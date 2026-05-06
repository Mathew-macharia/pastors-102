//! Buy/sell orchestration.
//!
//! BUY  -- 40 wallets across 8 atomic Jito bundles (5 wallets each), all
//!         targeting the same slot. Atomicity is a feature here: it
//!         protects against partial fills at different prices.
//!
//! SELL -- DUAL-PATH SIMULTANEOUS FIRE.
//!         For every selling wallet we build ONE signed VersionedTransaction
//!         that includes its own tip transfer, then submit that exact same
//!         signed tx through TWO independent paths in parallel:
//!           (a) packed into one of 8 Jito bundles (5 wallets each), each
//!               bundle fired concurrently to the Jito Block Engine;
//!           (b) standalone POST to Helius Sender's dual-route endpoint
//!               (which itself forwards to BOTH Jito's auction AND the
//!               validator-attached SWQoS staked-connection lane).
//!         A signature can only be processed once on Solana, so the
//!         network deduplicates: whichever path's copy reaches the leader
//!         first wins, the other returns "already processed".
//!         Why dual-path:
//!           - Jito bundles give same-slot atomic inclusion when they land,
//!             but if any single tx in a bundle reverts, the WHOLE bundle
//!             is dropped. The standalone Sender copy of each tx is then
//!             still in-flight independently and lands on the next slot.
//!           - Net effect: every signed sell tx has two simultaneous
//!             chances to land, with no double-spend risk.
//!         All paths fire from `tokio::spawn`'d tasks concurrently, so
//!         submission spread across all 48 outbound POSTs (40 + 8) is
//!         bounded by tokio task-scheduling latency + HTTP/2 stream
//!         multiplexing -- typically sub-millisecond.
//!         Inclusion latency, however, is bounded by Solana's slot time
//!         (~400 ms): you cannot land a transaction faster than the next
//!         slot's leader produces a block. That is a protocol limit, not
//!         a software limit.
//!         Stragglers (wallets whose first-wave sig hasn't confirmed
//!         within ~1.5 s) are rebuilt with a fresh blockhash + bumped
//!         priority fee + bumped tip and re-fired through both paths.
//!         Up to 2 retry waves; each retry wave fires the full dual-path
//!         again for only the wallets that didn't confirm.

use anyhow::{Context, Result};
use base64::engine::general_purpose::STANDARD as B64;
use base64::Engine;
use futures::future::join_all;
use rand::Rng;
use solana_sdk::{
    compute_budget::ComputeBudgetInstruction,
    instruction::Instruction,
    message::{v0::Message as MessageV0, VersionedMessage},
    pubkey::Pubkey,
    signature::Signer,
    system_instruction,
    transaction::VersionedTransaction,
};
use spl_associated_token_account::get_associated_token_address;
use std::collections::{HashMap, HashSet};
use std::str::FromStr;
use std::sync::Arc;
use std::time::Duration;
use tracing::{info, warn};

use crate::{
    config::Config,
    jito::{random_tip_account, JitoClient},
    pumpfun::{
        self, apply_slippage_floor, estimate_tokens_out, BuyParams, SellParams,
        INITIAL_VIRTUAL_SOL_RESERVES, INITIAL_VIRTUAL_TOKEN_RESERVES,
    },
    rpc::RpcPool,
    wallets::LoadedWallet,
};

/// Number of retry WAVES after the initial first wave. Total = 1 + this.
/// Each wave only re-fires the wallets that didn't confirm.
const SELL_RETRY_WAVES: u32 = 2;
/// How long to wait after firing a wave before checking which wallets
/// actually landed. ~3-4 Solana slots @ 400 ms = enough headroom for
/// confirmed status to propagate via getSignatureStatuses.
const FIRST_WAVE_WAIT_MS: u64 = 1500;
/// Same as above for retry waves.
const RETRY_WAVE_WAIT_MS: u64 = 1500;
/// Per-poll timeout when batch-checking sig statuses. Short because we
/// want to fail fast and re-fire stragglers.
const STATUS_BATCH_LIMIT: usize = 256;
/// Helius Sender's "dual-route" minimum tip (forwards to BOTH Jito and
/// validator-attached SWQoS staked connections for max inclusion rate).
/// Source: https://www.helius.dev/docs/sending-transactions/sender
const SENDER_DUAL_MIN_TIP_LAMPORTS: u64 = 200_000;
/// Bundle size for the Jito sell path. Solana caps at 5 txs per bundle.
const SELL_BUNDLE_SIZE: usize = 5;

/// Runtime overrides set by the UI / env, applied per-fire.
#[derive(Debug, Clone, Default)]
pub struct SellOverrides {
    /// Override `cfg.sell.after_blocks` for this fire only.
    pub after_blocks: Option<u64>,
}

#[derive(Debug, Clone)]
pub struct PreparedBuy {
    pub mint: Pubkey,
    pub creator: Pubkey,
    pub fee_recipient: Pubkey,
    pub bundles: Vec<Bundle>,
}

#[derive(Debug, Clone)]
pub struct Bundle {
    pub bundle_index: usize,
    pub txs: Vec<VersionedTransaction>,
    pub wallets_in_order: Vec<Pubkey>,
    pub buy_amounts: Vec<u64>,
}

#[derive(Debug, Clone, serde::Serialize)]
pub struct BuyResult {
    pub bundle_id: String,
    pub bundle_index: usize,
    pub submitted_at: chrono::DateTime<chrono::Utc>,
    pub landed_slot: Option<u64>,
    pub confirmation_status: Option<String>,
    pub error: Option<String>,
}

#[derive(Debug, Clone, serde::Serialize)]
pub struct SellResult {
    pub wallet: String,
    pub wallet_index: usize,
    pub amount_atoms: u64,
    pub signature: Option<String>,
    pub attempts: u32,
    pub status: String, // "confirmed" | "failed" | "timeout"
    pub error: Option<String>,
}

pub struct Strategy {
    cfg: Arc<Config>,
    rpc: RpcPool,
    jito: JitoClient,
    wallets: Arc<Vec<LoadedWallet>>,
    fee_recipient: Pubkey,
}

impl Strategy {
    pub fn new(
        cfg: Arc<Config>,
        rpc: RpcPool,
        jito: JitoClient,
        wallets: Arc<Vec<LoadedWallet>>,
        fee_recipient: Pubkey,
    ) -> Self {
        Self {
            cfg,
            rpc,
            jito,
            wallets,
            fee_recipient,
        }
    }

    // ===================================================================
    // BUY: 8 atomic Jito bundles, all wallets fire same-slot
    // ===================================================================

    pub async fn prepare_buy(&self, mint: Pubkey, creator: Pubkey) -> Result<PreparedBuy> {
        let cfg = &self.cfg;
        let bundle_size = cfg.bundle.size;
        let num_bundles = (self.wallets.len() + bundle_size - 1) / bundle_size;
        info!(
            "preparing buy: mint={} creator={} wallets={} bundles={} (size {})",
            mint, creator, self.wallets.len(), num_bundles, bundle_size
        );

        let mut order: Vec<usize> = (0..self.wallets.len()).collect();
        {
            let mut rng = rand::thread_rng();
            use rand::seq::SliceRandom;
            order.shuffle(&mut rng);
        }

        let (blockhash, _) = self.rpc.get_latest_blockhash().await?;
        let virtual_sol = INITIAL_VIRTUAL_SOL_RESERVES;
        let virtual_token = INITIAL_VIRTUAL_TOKEN_RESERVES;

        let mut bundles = Vec::with_capacity(num_bundles);
        let mut idx = 0;

        for b in 0..num_bundles {
            let mut bundle_txs = Vec::with_capacity(bundle_size);
            let mut wallets_in_bundle = Vec::with_capacity(bundle_size);
            let mut amounts = Vec::with_capacity(bundle_size);
            let take = bundle_size.min(self.wallets.len() - idx);

            for slot_in_bundle in 0..take {
                let wallet = &self.wallets[order[idx]];
                idx += 1;

                let mut rng = rand::thread_rng();
                let sol_amt = rng.gen_range(cfg.buy.min_sol..=cfg.buy.max_sol);
                let lamports = (sol_amt * 1_000_000_000.0) as u64;
                amounts.push(lamports);

                let est = estimate_tokens_out(lamports, virtual_sol, virtual_token);
                let min_out = apply_slippage_floor(est, cfg.buy.slippage_bps);
                let prio = rng.gen_range(
                    cfg.buy.priority_micro_lamports_min..=cfg.buy.priority_micro_lamports_max,
                );

                let mut ixs: Vec<Instruction> = Vec::with_capacity(5);
                ixs.push(ComputeBudgetInstruction::set_compute_unit_limit(
                    cfg.buy.compute_unit_limit,
                ));
                ixs.push(ComputeBudgetInstruction::set_compute_unit_price(prio));
                ixs.push(pumpfun::create_user_ata_idempotent_ix(
                    &wallet.pubkey,
                    &wallet.pubkey,
                    &mint,
                ));
                ixs.push(pumpfun::build_buy_exact_sol_in_ix(
                    &wallet.pubkey,
                    &mint,
                    &creator,
                    &self.fee_recipient,
                    BuyParams {
                        spendable_sol_in: lamports,
                        min_tokens_out: min_out,
                        track_volume: None,
                    },
                ));

                if slot_in_bundle == take - 1 {
                    let tip_lamports = rng.gen_range(
                        cfg.jito.tip_lamports_min..=cfg.jito.tip_lamports_max,
                    );
                    let tip_acct = random_tip_account();
                    ixs.push(system_instruction::transfer(
                        &wallet.pubkey,
                        &tip_acct,
                        tip_lamports,
                    ));
                }

                let msg = MessageV0::try_compile(&wallet.pubkey, &ixs, &[], blockhash)
                    .context("compiling MessageV0 for buy tx")?;
                let signed = VersionedTransaction::try_new(
                    VersionedMessage::V0(msg),
                    &[&wallet.keypair],
                )?;
                bundle_txs.push(signed);
                wallets_in_bundle.push(wallet.pubkey);

                let us = rng.gen_range(
                    cfg.bundle.jitter_us_min..=cfg.bundle.jitter_us_max.max(1),
                );
                if us > 0 {
                    tokio::time::sleep(Duration::from_micros(us)).await;
                }
            }

            bundles.push(Bundle {
                bundle_index: b,
                txs: bundle_txs,
                wallets_in_order: wallets_in_bundle,
                buy_amounts: amounts,
            });
        }

        Ok(PreparedBuy {
            mint,
            creator,
            fee_recipient: self.fee_recipient,
            bundles,
        })
    }

    pub async fn fire_buy(&self, prepared: &PreparedBuy) -> Result<Vec<BuyResult>> {
        let mut results = Vec::with_capacity(prepared.bundles.len());
        let cfg = &self.cfg;

        for bundle in &prepared.bundles {
            let submitted_at = chrono::Utc::now();
            let send_res = self.jito.send_bundle(&bundle.txs).await;
            let mut br = BuyResult {
                bundle_id: String::new(),
                bundle_index: bundle.bundle_index,
                submitted_at,
                landed_slot: None,
                confirmation_status: None,
                error: None,
            };
            match send_res {
                Ok(id) => {
                    info!(
                        "[buy bundle {}] sent bundle_id={} txs={} amounts={:?}",
                        bundle.bundle_index,
                        id,
                        bundle.txs.len(),
                        bundle.buy_amounts
                    );
                    br.bundle_id = id;
                }
                Err(e) => {
                    warn!("[buy bundle {}] FAILED: {}", bundle.bundle_index, e);
                    br.error = Some(e.to_string());
                }
            }
            results.push(br);

            let mut rng = rand::thread_rng();
            let us = rng.gen_range(
                cfg.bundle.inter_bundle_jitter_us_min
                    ..=cfg.bundle.inter_bundle_jitter_us_max.max(1),
            );
            if us > 0 {
                tokio::time::sleep(Duration::from_micros(us)).await;
            }
        }

        let poll_timeout = Duration::from_secs(15);
        for r in results.iter_mut() {
            if r.bundle_id.is_empty() {
                continue;
            }
            match self.jito.poll_status(&r.bundle_id, poll_timeout).await {
                Ok(Some(s)) => {
                    r.landed_slot = s.slot;
                    r.confirmation_status = s.confirmation_status.clone();
                    if let Some(err) = s.err {
                        r.error = Some(err.to_string());
                    }
                    info!(
                        "[buy bundle {}] status={:?} slot={:?}",
                        r.bundle_index, r.confirmation_status, r.landed_slot
                    );
                }
                Ok(None) => {
                    r.confirmation_status = Some("timeout".into());
                    warn!("[buy bundle {}] poll timeout", r.bundle_index);
                }
                Err(e) => {
                    r.error = Some(format!("poll: {e}"));
                    warn!("[buy bundle {}] poll error: {}", r.bundle_index, e);
                }
            }
        }

        Ok(results)
    }

    // ===================================================================
    // SELL: dual-path simultaneous fire (40 Sender + 8 Jito) per wave,
    //       wave-level retry on stragglers only.
    // ===================================================================

    /// Wait until current slot >= reference_slot + N, where N is taken
    /// from the override if present, otherwise from `cfg.sell.after_blocks`.
    pub async fn wait_blocks_after(
        &self,
        reference_slot: u64,
        overrides: &SellOverrides,
    ) -> Result<u64> {
        let n = overrides.after_blocks.unwrap_or(self.cfg.sell.after_blocks);
        let target = reference_slot + n;
        info!(
            "waiting for slot >= {} (ref={} N={})",
            target, reference_slot, n
        );
        loop {
            let slot = self.rpc.get_slot().await?;
            if slot >= target {
                return Ok(slot);
            }
            tokio::time::sleep(Duration::from_millis(200)).await;
        }
    }

    /// Query token balances in parallel, build all sell txs once, then
    /// fire DUAL-PATH (Sender + Jito bundles) in parallel for sub-ms
    /// submission spread. Stragglers retry on bumped fees up to
    /// SELL_RETRY_WAVES additional waves.
    pub async fn fire_sell(&self, mint: Pubkey, creator: Pubkey) -> Result<Vec<SellResult>> {
        // 1. Parallel balance fetch.
        let balance_futures = self.wallets.iter().map(|w| {
            let rpc = self.rpc.clone();
            let ata = get_associated_token_address(&w.pubkey, &mint);
            async move { rpc.get_token_balance_atoms(&ata).await.unwrap_or(0) }
        });
        let balances = join_all(balance_futures).await;

        let mut targets: Vec<(usize, u64)> = Vec::new();
        for (i, bal) in balances.into_iter().enumerate() {
            if bal > 0 {
                targets.push((i, bal));
            }
        }
        info!(
            "sell: {}/{} wallets hold tokens, dual-path firing",
            targets.len(),
            self.wallets.len()
        );
        if targets.is_empty() {
            return Ok(Vec::new());
        }

        // Per-wallet bookkeeping. KEY = target_index (position in
        // self.wallets), NOT the human-friendly CSV index.
        // confirmed[w]     = (sig, slot, attempts) once any sig lands
        // all_sigs[w]      = every signature we've broadcast for w
        // last_error[w]    = most recent error (for failed wallets)
        // total_attempts[w]= number of waves w was fired in
        let mut confirmed: HashMap<usize, (String, u64, u32)> = HashMap::new();
        let mut all_sigs: HashMap<usize, Vec<String>> = HashMap::new();
        let mut last_error: HashMap<usize, String> = HashMap::new();
        let mut total_attempts: HashMap<usize, u32> = HashMap::new();

        let mut to_fire: Vec<(usize, u64)> = targets.clone();

        for wave in 0..=SELL_RETRY_WAVES {
            if to_fire.is_empty() {
                break;
            }
            let bump = 1.0_f64 + 0.5_f64 * wave as f64; // 1.0, 1.5, 2.0
            let wave_label = if wave == 0 { "FIRST WAVE" } else { "RETRY WAVE" };
            info!(
                "[sell {} {}] firing dual-path for {} wallets (fee bump x{:.1})",
                wave_label,
                wave,
                to_fire.len(),
                bump
            );

            // 2. Build all sell txs for this wave with one fresh blockhash.
            let prepared = match self.build_sell_wave(mint, creator, &to_fire, bump).await {
                Ok(p) => p,
                Err(e) => {
                    warn!("[sell wave {}] build error: {}", wave, e);
                    for (w, _) in &to_fire {
                        last_error.insert(*w, format!("build: {e}"));
                    }
                    break;
                }
            };

            for p in &prepared {
                all_sigs
                    .entry(p.target_index)
                    .or_default()
                    .push(p.signature.clone());
                *total_attempts.entry(p.target_index).or_insert(0) += 1;
            }

            // 3. Fire dual-path: 40 Sender submits + 8 Jito bundles fire
            //    concurrently from independently spawned tokio tasks.
            self.fire_sell_wave_dual_path(&prepared).await;

            // 4. Wait the wave window, then sweep.
            let wait_ms = if wave == 0 {
                FIRST_WAVE_WAIT_MS
            } else {
                RETRY_WAVE_WAIT_MS
            };
            tokio::time::sleep(Duration::from_millis(wait_ms)).await;

            // 5. Batch-query every sig we've ever broadcast for any wallet
            //    that's not yet confirmed. Anything that landed (across
            //    any path, any wave) marks the wallet done.
            let still_pending: Vec<usize> = to_fire
                .iter()
                .map(|(w, _)| *w)
                .filter(|w| !confirmed.contains_key(w))
                .collect();

            let mut sweep_sigs: Vec<(usize, String)> = Vec::new();
            for w in &still_pending {
                if let Some(sigs) = all_sigs.get(w) {
                    for s in sigs {
                        sweep_sigs.push((*w, s.clone()));
                    }
                }
            }
            let sweep_results =
                batch_signature_statuses(&self.rpc, &sweep_sigs).await;

            for (w, sig, outcome) in sweep_results {
                match outcome {
                    SigOutcome::Confirmed(slot) => {
                        let attempts = *total_attempts.get(&w).unwrap_or(&1);
                        confirmed
                            .entry(w)
                            .or_insert_with(|| (sig.clone(), slot, attempts));
                    }
                    SigOutcome::Reverted(err) => {
                        last_error.insert(w, format!("reverted: {err}"));
                    }
                    SigOutcome::Pending => {}
                }
            }

            let conf_count = confirmed.len();
            info!(
                "[sell wave {}] sweep: {}/{} wallets confirmed",
                wave,
                conf_count,
                targets.len()
            );

            // 6. Decide who needs another wave.
            to_fire = to_fire
                .into_iter()
                .filter(|(w, _)| !confirmed.contains_key(w))
                .collect();
        }

        // 7. Final assembly.
        let mut results = Vec::with_capacity(targets.len());
        for (widx, amount) in &targets {
            let attempts = *total_attempts.get(widx).unwrap_or(&0);
            let wallet_pk = self.wallets[*widx].pubkey.to_string();
            if let Some((sig, _slot, _conf_attempt)) = confirmed.get(widx) {
                results.push(SellResult {
                    wallet: wallet_pk,
                    wallet_index: self.wallets[*widx].index,
                    amount_atoms: *amount,
                    signature: Some(sig.clone()),
                    attempts,
                    status: "confirmed".into(),
                    error: None,
                });
            } else {
                let err = last_error
                    .remove(widx)
                    .unwrap_or_else(|| "no confirmation in any wave".to_string());
                let last_sig = all_sigs.get(widx).and_then(|v| v.last().cloned());
                results.push(SellResult {
                    wallet: wallet_pk,
                    wallet_index: self.wallets[*widx].index,
                    amount_atoms: *amount,
                    signature: last_sig,
                    attempts,
                    status: "failed".into(),
                    error: Some(err),
                });
            }
        }

        let confirmed_n = results.iter().filter(|r| r.status == "confirmed").count();
        info!(
            "sell complete: {}/{} confirmed",
            confirmed_n,
            results.len()
        );
        Ok(results)
    }

    /// Build one signed sell tx per (wallet, amount) target, using a
    /// SINGLE fresh blockhash for the whole wave. Tip transfer is
    /// included in every tx so each is independently submittable via
    /// either Sender or Jito.
    async fn build_sell_wave(
        &self,
        mint: Pubkey,
        creator: Pubkey,
        targets: &[(usize, u64)],
        bump_factor: f64,
    ) -> Result<Vec<PreparedSellTx>> {
        let cfg = &self.cfg;
        let (blockhash, _) = self
            .rpc
            .get_latest_blockhash()
            .await
            .context("getLatestBlockhash")?;

        let mut prepared: Vec<PreparedSellTx> = Vec::with_capacity(targets.len());
        for (widx, amount) in targets {
            let wallet = &self.wallets[*widx];
            let mut rng = rand::thread_rng();

            let prio_base = rng.gen_range(
                cfg.sell.priority_micro_lamports_min..=cfg.sell.priority_micro_lamports_max,
            );
            let prio = ((prio_base as f64) * bump_factor) as u64;

            let tip_min_cfg = cfg.jito.tip_lamports_min.max(SENDER_DUAL_MIN_TIP_LAMPORTS);
            let tip_max_cfg = cfg.jito.tip_lamports_max.max(SENDER_DUAL_MIN_TIP_LAMPORTS);
            let tip_base = rng.gen_range(tip_min_cfg..=tip_max_cfg);
            let tip = ((tip_base as f64) * bump_factor) as u64;
            let tip_acct = random_tip_account();

            let ixs: Vec<Instruction> = vec![
                ComputeBudgetInstruction::set_compute_unit_limit(cfg.sell.compute_unit_limit),
                ComputeBudgetInstruction::set_compute_unit_price(prio),
                pumpfun::build_sell_ix(
                    &wallet.pubkey,
                    &mint,
                    &creator,
                    &self.fee_recipient,
                    SellParams {
                        amount: *amount,
                        min_sol_output: 1, // accept any output to guarantee exit
                    },
                ),
                system_instruction::transfer(&wallet.pubkey, &tip_acct, tip),
            ];
            let msg = MessageV0::try_compile(&wallet.pubkey, &ixs, &[], blockhash)
                .context("compiling sell MessageV0")?;
            let tx = VersionedTransaction::try_new(
                VersionedMessage::V0(msg),
                &[&wallet.keypair],
            )?;
            let bytes = bincode::serialize(&tx).context("serialize sell tx")?;
            let b64 = B64.encode(&bytes);
            let signature = tx.signatures[0].to_string();

            prepared.push(PreparedSellTx {
                wallet_index: wallet.index,
                target_index: *widx,
                signature,
                tx_b64: b64,
                tx,
                prio,
                tip,
            });
        }
        Ok(prepared)
    }

    /// Fire a built wave through BOTH paths simultaneously.
    /// 40 individual Sender POSTs + 8 Jito bundle POSTs all dispatched
    /// from concurrently spawned tokio tasks. Submission spread is
    /// bounded by tokio scheduler latency + HTTP/2 multiplexing.
    async fn fire_sell_wave_dual_path(&self, prepared: &[PreparedSellTx]) {
        let sender_url = self.rpc.sender_dual().to_string();

        let mut handles: Vec<tokio::task::JoinHandle<()>> = Vec::new();

        // Path A: 40 standalone tx submits to Helius Sender dual-route.
        for p in prepared {
            let rpc = self.rpc.clone();
            let url = sender_url.clone();
            let b64 = p.tx_b64.clone();
            let widx = p.wallet_index;
            let sig = p.signature.clone();
            let prio = p.prio;
            let tip = p.tip;
            handles.push(tokio::spawn(async move {
                match rpc.send_transaction_b64(&url, &b64, true).await {
                    Ok(returned_sig) => {
                        info!(
                            "[sell-sender w{}] sent sig={} prio={} tip={}",
                            widx, returned_sig, prio, tip
                        );
                    }
                    Err(e) => {
                        // Tx may have already landed via Jito path -- this
                        // is normal and not actually an error.
                        let msg = e.to_string();
                        if msg.contains("already been processed")
                            || msg.contains("AlreadyProcessed")
                        {
                            info!("[sell-sender w{}] sig={} already processed (Jito won)", widx, sig);
                        } else {
                            warn!("[sell-sender w{}] submit error: {}", widx, e);
                        }
                    }
                }
            }));
        }

        // Path B: 8 Jito bundles of 5 txs each fired in parallel.
        let bundle_chunks: Vec<Vec<VersionedTransaction>> = prepared
            .chunks(SELL_BUNDLE_SIZE)
            .map(|c| c.iter().map(|p| p.tx.clone()).collect())
            .collect();
        for (bidx, txs) in bundle_chunks.into_iter().enumerate() {
            let jito = self.jito.clone();
            let n = txs.len();
            handles.push(tokio::spawn(async move {
                match jito.send_bundle(&txs).await {
                    Ok(id) => {
                        info!("[sell-jito bundle {}] sent bundle_id={} txs={}", bidx, id, n);
                    }
                    Err(e) => {
                        // Bundle may revert atomically if any tx in it
                        // already landed via the Sender path -- the
                        // standalone Sender copies are still in-flight.
                        warn!("[sell-jito bundle {}] send error: {}", bidx, e);
                    }
                }
            }));
        }

        // We don't await results here -- spawn-and-go for the tightest
        // possible submission spread. Confirmation is detected via the
        // batched getSignatureStatuses sweep after the wave window.
        for h in handles {
            let _ = h.await;
        }
    }
}

/// One built+signed sell transaction ready for dual-path dispatch.
#[derive(Clone)]
struct PreparedSellTx {
    /// Human-friendly index from wallets.csv (only used for logging).
    wallet_index: usize,
    /// Index into `Strategy.wallets` vec; the bookkeeping key.
    target_index: usize,
    signature: String,
    tx_b64: String,
    tx: VersionedTransaction,
    prio: u64,
    tip: u64,
}

#[derive(Debug)]
enum SigOutcome {
    Confirmed(u64),
    Reverted(String),
    Pending,
}

/// Batch-check every (wallet, sig) tuple via getSignatureStatuses
/// (max 256 sigs per RPC call). Returns the outcome per (wallet, sig).
async fn batch_signature_statuses(
    rpc: &RpcPool,
    sweep: &[(usize, String)],
) -> Vec<(usize, String, SigOutcome)> {
    if sweep.is_empty() {
        return Vec::new();
    }
    let mut out: Vec<(usize, String, SigOutcome)> = Vec::with_capacity(sweep.len());
    let mut seen: HashSet<String> = HashSet::new();

    for chunk in sweep.chunks(STATUS_BATCH_LIMIT) {
        let sigs: Vec<String> = chunk
            .iter()
            .map(|(_, s)| s.clone())
            .filter(|s| seen.insert(s.clone()))
            .collect();
        if sigs.is_empty() {
            continue;
        }
        let res = match rpc
            .post(
                &rpc.primary().url,
                "getSignatureStatuses",
                serde_json::json!([sigs, {"searchTransactionHistory": false}]),
            )
            .await
        {
            Ok(r) => r,
            Err(e) => {
                warn!("batch sig sweep error: {}", e);
                continue;
            }
        };
        let arr = res
            .get("value")
            .and_then(|v| v.as_array())
            .cloned()
            .unwrap_or_default();

        let mut sig_to_outcome: HashMap<String, SigOutcome> = HashMap::new();
        for (i, item) in arr.iter().enumerate() {
            let sig = match sigs.get(i) {
                Some(s) => s.clone(),
                None => continue,
            };
            if item.is_null() {
                sig_to_outcome.insert(sig, SigOutcome::Pending);
                continue;
            }
            if let Some(err) = item.get("err") {
                if !err.is_null() {
                    sig_to_outcome
                        .insert(sig, SigOutcome::Reverted(err.to_string()));
                    continue;
                }
            }
            let cs = item.get("confirmationStatus").and_then(|x| x.as_str());
            if matches!(cs, Some("confirmed") | Some("finalized") | Some("processed")) {
                let slot = item.get("slot").and_then(|s| s.as_u64()).unwrap_or(0);
                sig_to_outcome.insert(sig, SigOutcome::Confirmed(slot));
            } else {
                sig_to_outcome.insert(sig, SigOutcome::Pending);
            }
        }

        for (w, sig) in chunk {
            let outcome = sig_to_outcome
                .remove(sig)
                .unwrap_or(SigOutcome::Pending);
            out.push((*w, sig.clone(), outcome));
        }
    }
    out
}

pub fn parse_pubkey(s: &str) -> Result<Pubkey> {
    Pubkey::from_str(s).context("parsing pubkey")
}
