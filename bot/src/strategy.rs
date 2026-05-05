//! Buy/sell orchestration: 40 wallets, 8 Jito bundles of 5 wallets each,
//! all targeting the same slot, with millisecond-level construction jitter
//! between bundles + per-tx randomized priority fees and SOL amounts.
//!
//! The flow:
//!   1. `prepare_buy(mint, creator)` builds 40 signed VersionedTransactions
//!      grouped into 8 bundles. Each bundle's last tx carries the Jito tip.
//!   2. `fire_buy()` submits all 8 bundles to Jito Block Engine in rapid
//!      sequence (with configured µs jitter). Returns 8 bundle IDs.
//!   3. After `sell.after_blocks` slots have elapsed past the buy's landing
//!      slot, `fire_sell()` queries each wallet's token balance and submits
//!      40 sell txs (bundled or parallel per config).

use anyhow::{Context, Result};
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
use std::str::FromStr;
use std::sync::Arc;
use std::time::{Duration, Instant};
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

    /// Build 8 signed bundles covering all 40 wallets. Does not submit yet.
    pub async fn prepare_buy(&self, mint: Pubkey, creator: Pubkey) -> Result<PreparedBuy> {
        let cfg = &self.cfg;
        let bundle_size = cfg.bundle.size;
        let num_bundles = (self.wallets.len() + bundle_size - 1) / bundle_size;
        info!(
            "preparing buy: mint={} creator={} wallets={} bundles={} (size {})",
            mint, creator, self.wallets.len(), num_bundles, bundle_size
        );

        // Shuffle wallet order so wallet-to-bundle mapping is non-deterministic.
        let mut order: Vec<usize> = (0..self.wallets.len()).collect();
        {
            let mut rng = rand::thread_rng();
            use rand::seq::SliceRandom;
            order.shuffle(&mut rng);
        }

        let (blockhash, _) = self.rpc.get_latest_blockhash().await?;

        // Use INITIAL reserves for block-0 sniping. If the curve has already
        // had buys, the caller could fetch live state and pass them in via a
        // future API extension; for snipe-time we want the block-0 quote.
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

                // Random amount in [min, max] SOL
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
                // Idempotent ATA creation -- payer is the wallet itself
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

                // Last tx in the bundle carries the Jito tip transfer.
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

                // Inter-tx jitter (within bundle preparation; doesn't affect
                // on-chain ordering -- just spreads CPU work).
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

    /// Submit all bundles to Jito as fast as possible, with configured
    /// µs jitter between submissions. Returns one BuyResult per bundle.
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

            // Inter-bundle jitter. Keeps total spread well below 400ms.
            let mut rng = rand::thread_rng();
            let us = rng.gen_range(
                cfg.bundle.inter_bundle_jitter_us_min
                    ..=cfg.bundle.inter_bundle_jitter_us_max.max(1),
            );
            if us > 0 {
                tokio::time::sleep(Duration::from_micros(us)).await;
            }
        }

        // Poll bundle statuses (best-effort; don't block forever).
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

    /// Wait until current slot >= reference_slot + N.
    pub async fn wait_blocks_after(&self, reference_slot: u64) -> Result<u64> {
        let target = reference_slot + self.cfg.sell.after_blocks;
        info!(
            "waiting for slot >= {} (current ref slot = {}, +N = {})",
            target, reference_slot, self.cfg.sell.after_blocks
        );
        loop {
            let slot = self.rpc.get_slot().await?;
            if slot >= target {
                return Ok(slot);
            }
            tokio::time::sleep(Duration::from_millis(200)).await;
        }
    }

    /// Build + submit sell txs for all wallets that hold tokens.
    pub async fn fire_sell(
        &self,
        mint: Pubkey,
        creator: Pubkey,
    ) -> Result<Vec<BuyResult>> {
        let cfg = &self.cfg;
        let (blockhash, _) = self.rpc.get_latest_blockhash().await?;

        // Fetch all token balances in parallel-ish.
        let mut balances: Vec<(usize, u64)> = Vec::with_capacity(self.wallets.len());
        for w in self.wallets.iter() {
            let ata = get_associated_token_address(&w.pubkey, &mint);
            let bal = self.rpc.get_token_balance_atoms(&ata).await.unwrap_or(0);
            if bal > 0 {
                balances.push((w.index - 1, bal));
            }
        }
        info!("sell-side: {} wallets hold tokens", balances.len());
        if balances.is_empty() {
            return Ok(Vec::new());
        }

        if cfg.sell.strategy == "bundled" {
            self.sell_bundled(mint, creator, &balances, blockhash).await
        } else {
            self.sell_parallel(mint, creator, &balances, blockhash).await
        }
    }

    async fn sell_bundled(
        &self,
        mint: Pubkey,
        creator: Pubkey,
        balances: &[(usize, u64)],
        blockhash: solana_sdk::hash::Hash,
    ) -> Result<Vec<BuyResult>> {
        let cfg = &self.cfg;
        let bundle_size = cfg.bundle.size;
        let mut results = Vec::new();
        let mut rng = rand::thread_rng();

        for (b_idx, chunk) in balances.chunks(bundle_size).enumerate() {
            let mut txs = Vec::with_capacity(chunk.len());
            for (i, (widx, amount)) in chunk.iter().enumerate() {
                let w = &self.wallets[*widx];
                let prio = rng.gen_range(
                    cfg.sell.priority_micro_lamports_min..=cfg.sell.priority_micro_lamports_max,
                );
                let min_sol_output = 1u64; // accept any non-zero output for exit (worst-case slippage)

                let mut ixs: Vec<Instruction> = Vec::with_capacity(4);
                ixs.push(ComputeBudgetInstruction::set_compute_unit_limit(
                    cfg.sell.compute_unit_limit,
                ));
                ixs.push(ComputeBudgetInstruction::set_compute_unit_price(prio));
                ixs.push(pumpfun::build_sell_ix(
                    &w.pubkey,
                    &mint,
                    &creator,
                    &self.fee_recipient,
                    SellParams {
                        amount: *amount,
                        min_sol_output,
                    },
                ));
                if i == chunk.len() - 1 {
                    let tip_lamports = rng.gen_range(
                        cfg.jito.tip_lamports_min..=cfg.jito.tip_lamports_max,
                    );
                    let tip_acct = random_tip_account();
                    ixs.push(system_instruction::transfer(
                        &w.pubkey,
                        &tip_acct,
                        tip_lamports,
                    ));
                }
                let msg = MessageV0::try_compile(&w.pubkey, &ixs, &[], blockhash)?;
                let tx = VersionedTransaction::try_new(VersionedMessage::V0(msg), &[&w.keypair])?;
                txs.push(tx);
            }
            let submitted_at = chrono::Utc::now();
            let send_res = self.jito.send_bundle(&txs).await;
            let mut br = BuyResult {
                bundle_id: String::new(),
                bundle_index: b_idx,
                submitted_at,
                landed_slot: None,
                confirmation_status: None,
                error: None,
            };
            match send_res {
                Ok(id) => {
                    info!("[sell bundle {}] sent bundle_id={}", b_idx, id);
                    br.bundle_id = id;
                }
                Err(e) => {
                    warn!("[sell bundle {}] FAILED: {}", b_idx, e);
                    br.error = Some(e.to_string());
                }
            }
            results.push(br);
        }
        Ok(results)
    }

    async fn sell_parallel(
        &self,
        mint: Pubkey,
        creator: Pubkey,
        balances: &[(usize, u64)],
        blockhash: solana_sdk::hash::Hash,
    ) -> Result<Vec<BuyResult>> {
        let cfg = &self.cfg;
        let mut results = Vec::with_capacity(balances.len());
        let mut handles = Vec::new();
        let sender_url = self.rpc.sender_swqos().to_string();
        let rpc = self.rpc.clone();

        for (i, (widx, amount)) in balances.iter().enumerate() {
            let w = &self.wallets[*widx];
            let mut rng = rand::thread_rng();
            let prio = rng.gen_range(
                cfg.sell.priority_micro_lamports_min..=cfg.sell.priority_micro_lamports_max,
            );
            let tip_lamports = rng.gen_range(
                cfg.jito.tip_lamports_min..=cfg.jito.tip_lamports_max,
            );
            let tip_acct = random_tip_account();

            let ixs: Vec<Instruction> = vec![
                ComputeBudgetInstruction::set_compute_unit_limit(cfg.sell.compute_unit_limit),
                ComputeBudgetInstruction::set_compute_unit_price(prio),
                pumpfun::build_sell_ix(
                    &w.pubkey,
                    &mint,
                    &creator,
                    &self.fee_recipient,
                    SellParams {
                        amount: *amount,
                        min_sol_output: 1,
                    },
                ),
                system_instruction::transfer(&w.pubkey, &tip_acct, tip_lamports),
            ];
            let msg = MessageV0::try_compile(&w.pubkey, &ixs, &[], blockhash)?;
            let tx = VersionedTransaction::try_new(VersionedMessage::V0(msg), &[&w.keypair])?;
            let bytes = bincode::serialize(&tx)?;
            use base64::engine::general_purpose::STANDARD as B64;
            use base64::Engine;
            let b64 = B64.encode(bytes);

            let sender_url = sender_url.clone();
            let rpc = rpc.clone();
            let h = tokio::spawn(async move {
                let r = rpc.send_transaction_b64(&sender_url, &b64, true).await;
                (i, r)
            });
            handles.push(h);
        }
        for h in handles {
            match h.await {
                Ok((i, Ok(sig))) => {
                    info!("[sell parallel {}] sent sig={}", i, sig);
                    results.push(BuyResult {
                        bundle_id: sig,
                        bundle_index: i,
                        submitted_at: chrono::Utc::now(),
                        landed_slot: None,
                        confirmation_status: Some("sent".into()),
                        error: None,
                    });
                }
                Ok((i, Err(e))) => {
                    warn!("[sell parallel {}] error: {}", i, e);
                    results.push(BuyResult {
                        bundle_id: String::new(),
                        bundle_index: i,
                        submitted_at: chrono::Utc::now(),
                        landed_slot: None,
                        confirmation_status: None,
                        error: Some(e.to_string()),
                    });
                }
                Err(e) => warn!("join error: {}", e),
            }
        }
        Ok(results)
    }
}

/// Convenience: parse a base58 pubkey from string.
pub fn parse_pubkey(s: &str) -> Result<Pubkey> {
    Pubkey::from_str(s).context("parsing pubkey")
}
