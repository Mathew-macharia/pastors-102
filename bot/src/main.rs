//! pastors-bot
//!
//! Pump.fun sniper bot:
//!   * 40 wallets buy a target token in 8 atomic Jito bundles (5 wallets each)
//!   * Helius + Triton RPC rotation
//!   * Sell after N blocks via dual-path: every sell tx is fired through
//!     BOTH a Jito bundle AND Helius Sender's dual-route at the same
//!     instant. First-to-land per signature wins; the network
//!     deduplicates. Stragglers retry with bumped fees.
//!   * Triggered by either a UI button or a scheduled time

mod config;
mod jito;
mod pumpfun;
mod rpc;
mod strategy;
mod trigger;
mod ui;
mod wallets;

use anyhow::{Context, Result};
use clap::Parser;
use std::net::SocketAddr;
use std::path::PathBuf;
use std::sync::Arc;
use std::time::Duration;
use tokio::sync::RwLock;
use tracing::{error, info, warn};
use tracing_subscriber::EnvFilter;

use crate::config::Config;
use crate::jito::JitoClient;
use crate::rpc::RpcPool;
use crate::strategy::{SellOverrides, Strategy};
use crate::trigger::Trigger;
use crate::ui::{AppState, UiState};

#[derive(Parser, Debug)]
#[command(name = "pastors-bot", version, about)]
struct Cli {
    /// Path to config.toml
    #[arg(long, default_value = "config.toml")]
    config: PathBuf,

    /// Pump.fun fee_recipient pubkey. Mandatory; see README on how to get
    /// the current value from a recent Pump.fun buy on Solscan.
    #[arg(long, env = "PUMP_FEE_RECIPIENT")]
    fee_recipient: String,

    /// Bind address for the UI.
    #[arg(long, env = "UI_BIND", default_value = "127.0.0.1:7777")]
    ui_bind: String,

    /// Static directory for the UI.
    #[arg(long, default_value = "static")]
    ui_static: PathBuf,

    /// Skip UI and instead fire immediately on the given mint+creator.
    /// (Useful for testing without the browser.)
    #[arg(long)]
    headless_mint: Option<String>,
    #[arg(long)]
    headless_creator: Option<String>,
}

#[tokio::main]
async fn main() -> Result<()> {
    // Load .env if present
    let _ = dotenvy::dotenv();
    tracing_subscriber::fmt()
        .with_env_filter(EnvFilter::try_from_default_env().unwrap_or_else(|_| EnvFilter::new("info")))
        .with_target(false)
        .init();

    let cli = Cli::parse();
    let cfg = Config::load(&cli.config)
        .with_context(|| format!("loading {}", cli.config.display()))?;
    let cfg = Arc::new(cfg);
    info!(
        "config loaded. wallets_csv={} private_dir={}",
        cfg.paths.wallets_csv.display(),
        cfg.paths.wallets_private_dir.display()
    );

    // Load wallets
    let loaded = wallets::load_all(&cfg.paths.wallets_csv, &cfg.paths.wallets_private_dir)
        .context("loading wallets from phase 1 outputs")?;
    info!("loaded {} wallets", loaded.len());
    let wallets_arc = Arc::new(loaded);

    // Build RPC pool + Jito client
    let rpc = RpcPool::new(&cfg)?;
    let jito = JitoClient::new(cfg.jito.block_engine_url.clone(), cfg.rpc.timeout_ms)?;

    // Pre-warm HTTP/2 connections to the sell-time hot endpoints so the
    // first FIRE doesn't pay TCP+TLS handshake cost (~30-100 ms).
    rpc.warm_up(jito.block_engine_url()).await;
    info!("HTTP connections to Sender + Jito BE pre-warmed");

    // Resolve fee recipient
    let fee_recipient = strategy::parse_pubkey(&cli.fee_recipient)
        .context("--fee-recipient must be a valid base58 pubkey")?;
    info!("pump_fee_recipient = {}", fee_recipient);

    // UI state + trigger
    let trigger = Trigger::new();
    let ui_state = Arc::new(RwLock::new(UiState {
        status: "idle".into(),
        ..Default::default()
    }));
    let app_state = AppState {
        trigger: trigger.clone(),
        ui: ui_state.clone(),
    };

    let strategy = Arc::new(Strategy::new(
        cfg.clone(),
        rpc.clone(),
        jito.clone(),
        wallets_arc.clone(),
        fee_recipient,
    ));

    // Background firing task: waits for trigger, then runs the buy/sell flow.
    let firing_state = ui_state.clone();
    let strat_for_task = strategy.clone();
    tokio::spawn(async move {
        loop {
            trigger.wait().await;
            let snap = firing_state.read().await.clone();
            let mint_str = match snap.mint {
                Some(m) => m,
                None => {
                    warn!("trigger fired but no mint armed -- ignoring");
                    continue;
                }
            };
            let creator_str = match snap.creator {
                Some(c) => c,
                None => {
                    warn!("trigger fired but no creator armed -- ignoring");
                    continue;
                }
            };
            let mint = match strategy::parse_pubkey(&mint_str) {
                Ok(p) => p,
                Err(e) => {
                    error!("bad mint pubkey: {}", e);
                    continue;
                }
            };
            let creator = match strategy::parse_pubkey(&creator_str) {
                Ok(p) => p,
                Err(e) => {
                    error!("bad creator pubkey: {}", e);
                    continue;
                }
            };

            {
                let mut ui = firing_state.write().await;
                ui.status = "firing_buy".into();
            }
            let prepared = match strat_for_task.prepare_buy(mint, creator).await {
                Ok(p) => p,
                Err(e) => {
                    error!("prepare_buy failed: {}", e);
                    let mut ui = firing_state.write().await;
                    ui.status = format!("error: {e}");
                    continue;
                }
            };
            let buy_results = match strat_for_task.fire_buy(&prepared).await {
                Ok(r) => r,
                Err(e) => {
                    error!("fire_buy failed: {}", e);
                    let mut ui = firing_state.write().await;
                    ui.status = format!("error: {e}");
                    continue;
                }
            };

            // Determine reference slot (earliest landed slot among buy bundles)
            let mut ref_slot = None;
            for r in &buy_results {
                if let Some(s) = r.landed_slot {
                    ref_slot = Some(ref_slot.map_or(s, |x: u64| x.min(s)));
                }
            }
            {
                let mut ui = firing_state.write().await;
                ui.last_buy_results = Some(serde_json::to_value(&buy_results).unwrap_or_default());
                ui.status = match ref_slot {
                    Some(s) => format!("buy landed slot={s}, holding for sell"),
                    None => "buy submitted (no slot info), holding for sell".into(),
                };
            }

            // Snapshot UI overrides for this fire (the user can have set
            // a custom N via the UI's "Sell after N blocks" input).
            let overrides = SellOverrides {
                after_blocks: snap.sell_after_blocks,
            };

            // Wait N blocks then sell
            if let Some(s) = ref_slot {
                if let Err(e) = strat_for_task.wait_blocks_after(s, &overrides).await {
                    warn!("wait_blocks_after error: {}", e);
                }
            } else {
                tokio::time::sleep(Duration::from_secs(3)).await;
            }

            {
                let mut ui = firing_state.write().await;
                ui.status = "firing_sell".into();
            }
            match strat_for_task.fire_sell(mint, creator).await {
                Ok(sr) => {
                    let confirmed = sr.iter().filter(|r| r.status == "confirmed").count();
                    let mut ui = firing_state.write().await;
                    ui.last_sell_results = Some(serde_json::to_value(&sr).unwrap_or_default());
                    ui.status = format!("done ({}/{} sold)", confirmed, sr.len());
                    info!("sell done: {}/{} confirmed", confirmed, sr.len());
                }
                Err(e) => {
                    error!("fire_sell failed: {}", e);
                    let mut ui = firing_state.write().await;
                    ui.status = format!("sell_error: {e}");
                }
            }
        }
    });

    // Headless mode bypasses the UI for CLI-driven testing.
    if let (Some(m), Some(c)) = (cli.headless_mint.as_ref(), cli.headless_creator.as_ref()) {
        info!("headless mode: arming and firing immediately on mint={} creator={}", m, c);
        {
            let mut ui = ui_state.write().await;
            ui.mint = Some(m.clone());
            ui.creator = Some(c.clone());
            ui.status = "armed_headless".into();
        }
        app_state.trigger.fire_now();
        // keep main alive forever (firing task handles the rest)
        loop {
            tokio::time::sleep(Duration::from_secs(60)).await;
        }
    }

    // Otherwise, run the web UI.
    let ui_addr: SocketAddr = cli.ui_bind.parse().context("--ui-bind")?;
    let ui_static = cli
        .ui_static
        .canonicalize()
        .unwrap_or_else(|_| cli.ui_static.clone());
    ui::run(ui_addr, app_state, ui_static.to_str().unwrap_or("static")).await?;
    Ok(())
}

