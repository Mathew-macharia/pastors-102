//! Configuration loader. Reads .env and config.toml.
//!
//! Precedence (highest first):
//!   1. CLI flags (handled in main.rs via clap)
//!   2. Environment variables
//!   3. config.toml
//!   4. Compiled-in defaults

use anyhow::{Context, Result};
use serde::Deserialize;
use std::path::{Path, PathBuf};

const DEFAULT_HELIUS_URL: &str = "https://mainnet.helius-rpc.com/?api-key=";
const DEFAULT_HELIUS_SENDER_SWQOS: &str = "https://sender.helius-rpc.com/fast?swqos_only=true";
const DEFAULT_HELIUS_SENDER_DUAL: &str = "https://sender.helius-rpc.com/fast";
const DEFAULT_JITO_BLOCK_ENGINE: &str = "https://mainnet.block-engine.jito.wtf/api/v1/bundles";

#[derive(Debug, Clone, Deserialize)]
pub struct Config {
    pub rpc: RpcConfig,
    pub jito: JitoConfig,
    pub buy: BuyConfig,
    pub sell: SellConfig,
    pub bundle: BundleConfig,
    pub trigger: TriggerConfig,
    pub ui: UiConfig,
    pub paths: PathsConfig,
}

#[derive(Debug, Clone, Deserialize)]
pub struct RpcConfig {
    /// Helius API key. Reads from HELIUS_API_KEY env var if not set here.
    #[serde(default)]
    pub helius_api_key: String,

    /// Triton One full RPC URL (already includes the secret token).
    /// E.g. https://your-app.mainnet.rpcpool.com/your-secret-token
    /// Reads from TRITON_RPC_URL env var if not set here.
    #[serde(default)]
    pub triton_url: String,

    /// Per-wallet RPC rotation strategy: "round_robin" or "random".
    #[serde(default = "default_rotation")]
    pub rotation_strategy: String,

    /// Per-RPC HTTP timeout (ms).
    #[serde(default = "default_rpc_timeout_ms")]
    pub timeout_ms: u64,
}

#[derive(Debug, Clone, Deserialize)]
pub struct JitoConfig {
    /// Block engine endpoint. Default = global; can pin to regional for lower latency.
    #[serde(default = "default_jito_url")]
    pub block_engine_url: String,

    /// Tip in lamports for each bundle. Default 0.001 SOL = 1_000_000 lamports.
    /// Recommended: 0.001-0.05 SOL during peak Pump.fun activity.
    #[serde(default = "default_jito_tip")]
    pub tip_lamports_min: u64,
    #[serde(default = "default_jito_tip_max")]
    pub tip_lamports_max: u64,
}

#[derive(Debug, Clone, Deserialize)]
pub struct BuyConfig {
    /// Per-wallet random buy SOL range.
    #[serde(default = "default_buy_min")]
    pub min_sol: f64,
    #[serde(default = "default_buy_max")]
    pub max_sol: f64,

    /// Slippage tolerance for buy in basis points (100 = 1.00%).
    /// Used to compute min_tokens_out for the buy_exact_sol_in instruction.
    #[serde(default = "default_buy_slippage_bps")]
    pub slippage_bps: u32,

    /// Compute unit price range (microLamports / CU) for buy txs.
    #[serde(default = "default_prio_min")]
    pub priority_micro_lamports_min: u64,
    #[serde(default = "default_prio_max")]
    pub priority_micro_lamports_max: u64,

    /// Compute unit limit for buy txs (Pump.fun buy uses ~50k-80k CU).
    #[serde(default = "default_buy_cu_limit")]
    pub compute_unit_limit: u32,
}

#[derive(Debug, Clone, Deserialize)]
pub struct SellConfig {
    /// Slippage for sell in basis points. Note: the strategy uses
    /// `min_sol_output = 1` lamport regardless, so this is informational
    /// for now -- kept here for future use if you ever want to refuse
    /// to dump at sub-cost.
    #[serde(default = "default_sell_slippage_bps")]
    pub slippage_bps: u32,

    /// Number of slots to wait after the buy lands before triggering
    /// sell. Overridable at runtime via the SELL_AFTER_BLOCKS env var
    /// or the UI's "Sell after N blocks" input.
    #[serde(default = "default_sell_after_blocks")]
    pub after_blocks: u64,

    /// Compute unit price range for sell txs.
    #[serde(default = "default_prio_min")]
    pub priority_micro_lamports_min: u64,
    #[serde(default = "default_prio_max")]
    pub priority_micro_lamports_max: u64,

    /// CU limit for sell tx.
    #[serde(default = "default_sell_cu_limit")]
    pub compute_unit_limit: u32,
}

#[derive(Debug, Clone, Deserialize)]
pub struct BundleConfig {
    /// Bundle size (Jito hard limit = 5).
    #[serde(default = "default_bundle_size")]
    pub size: usize,

    /// Inter-tx jitter in microseconds within a bundle's signing/preparation
    /// step. NOTE: This does NOT delay on-chain inclusion (txs in a bundle
    /// land in the same slot atomically). It only randomizes the order of
    /// tx construction in the local signing pipeline so submitted timestamps
    /// vary slightly. Real bundle ordering is by priority fee + position.
    #[serde(default = "default_bundle_jitter_us_min")]
    pub jitter_us_min: u64,
    #[serde(default = "default_bundle_jitter_us_max")]
    pub jitter_us_max: u64,

    /// Inter-bundle jitter in microseconds between submitting consecutive
    /// bundles to Jito. Total wall-clock spread of all 8 bundle submissions
    /// must be << 400ms (Solana slot time) so they all target the same slot.
    /// Default 0-10000 us = 0-10 ms per bundle gap.
    #[serde(default = "default_inter_bundle_jitter_us_min")]
    pub inter_bundle_jitter_us_min: u64,
    #[serde(default = "default_inter_bundle_jitter_us_max")]
    pub inter_bundle_jitter_us_max: u64,
}

#[derive(Debug, Clone, Deserialize)]
pub struct TriggerConfig {
    /// "manual" | "scheduled" | "watch_creator" -- "manual" is the default
    /// (UI button). The other modes are placeholders for future wiring.
    #[serde(default = "default_trigger_mode")]
    pub mode: String,
}

#[derive(Debug, Clone, Deserialize)]
pub struct UiConfig {
    #[serde(default = "default_ui_host")]
    pub host: String,
    #[serde(default = "default_ui_port")]
    pub port: u16,
}

#[derive(Debug, Clone, Deserialize)]
pub struct PathsConfig {
    /// Path to wallets.csv from phase 1.
    #[serde(default = "default_wallets_csv")]
    pub wallets_csv: PathBuf,
    /// Directory containing the per-wallet keypair JSONs (phase 1 private/).
    #[serde(default = "default_wallets_private_dir")]
    pub wallets_private_dir: PathBuf,
}

// --- Defaults --------------------------------------------------------------
fn default_rotation() -> String { "round_robin".into() }
fn default_rpc_timeout_ms() -> u64 { 5000 }
fn default_jito_url() -> String { DEFAULT_JITO_BLOCK_ENGINE.into() }
fn default_jito_tip() -> u64 { 1_000_000 }       // 0.001 SOL
fn default_jito_tip_max() -> u64 { 3_000_000 }   // 0.003 SOL
fn default_buy_min() -> f64 { 0.05 }
fn default_buy_max() -> f64 { 0.08 }
fn default_buy_slippage_bps() -> u32 { 1500 }    // 15% (snipers eat slippage)
fn default_sell_slippage_bps() -> u32 { 2000 }   // 20% (slippage worse on exit)
fn default_prio_min() -> u64 { 100_000 }
fn default_prio_max() -> u64 { 500_000 }
fn default_buy_cu_limit() -> u32 { 100_000 }
fn default_sell_cu_limit() -> u32 { 100_000 }
fn default_sell_after_blocks() -> u64 { 5 }
fn default_bundle_size() -> usize { 5 }
fn default_bundle_jitter_us_min() -> u64 { 0 }
fn default_bundle_jitter_us_max() -> u64 { 1_000 }
fn default_inter_bundle_jitter_us_min() -> u64 { 0 }
fn default_inter_bundle_jitter_us_max() -> u64 { 10_000 }
fn default_trigger_mode() -> String { "manual".into() }
fn default_ui_host() -> String { "127.0.0.1".into() }
fn default_ui_port() -> u16 { 7777 }
fn default_wallets_csv() -> PathBuf { "../wallets/wallets.csv".into() }
fn default_wallets_private_dir() -> PathBuf { "../wallets/private".into() }

impl Config {
    /// Load config from a TOML file path (creating defaults if file missing),
    /// then overlay environment variables.
    pub fn load(path: &Path) -> Result<Self> {
        let mut cfg: Config = if path.exists() {
            let raw = std::fs::read_to_string(path)
                .with_context(|| format!("reading {}", path.display()))?;
            toml::from_str(&raw).with_context(|| format!("parsing {}", path.display()))?
        } else {
            // synthesize defaults via empty TOML
            toml::from_str("[rpc]\n[jito]\n[buy]\n[sell]\n[bundle]\n[trigger]\n[ui]\n[paths]\n")?
        };

        // Overlay env vars (env wins over TOML).
        if let Ok(v) = std::env::var("HELIUS_API_KEY") {
            if !v.trim().is_empty() {
                cfg.rpc.helius_api_key = v;
            }
        }
        if let Ok(v) = std::env::var("TRITON_RPC_URL") {
            if !v.trim().is_empty() {
                cfg.rpc.triton_url = v;
            }
        }
        if let Ok(v) = std::env::var("SELL_AFTER_BLOCKS") {
            if let Ok(n) = v.trim().parse::<u64>() {
                cfg.sell.after_blocks = n;
            }
        }

        cfg.validate()?;
        Ok(cfg)
    }

    pub fn validate(&self) -> Result<()> {
        if self.rpc.helius_api_key.trim().is_empty() {
            anyhow::bail!("HELIUS_API_KEY is empty (set via .env or config.toml)");
        }
        if self.rpc.triton_url.trim().is_empty() {
            anyhow::bail!("TRITON_RPC_URL is empty (set via .env or config.toml)");
        }
        if self.buy.min_sol <= 0.0 || self.buy.max_sol < self.buy.min_sol {
            anyhow::bail!("invalid buy SOL range");
        }
        if self.bundle.size == 0 || self.bundle.size > 5 {
            anyhow::bail!("bundle size must be 1..=5 (Jito hard limit)");
        }
        Ok(())
    }

    /// Full Helius mainnet RPC URL with API key embedded.
    pub fn helius_rpc_url(&self) -> String {
        format!("{}{}", DEFAULT_HELIUS_URL, self.rpc.helius_api_key)
    }

    pub fn helius_sender_url(&self, dual: bool) -> &'static str {
        if dual { DEFAULT_HELIUS_SENDER_DUAL } else { DEFAULT_HELIUS_SENDER_SWQOS }
    }
}
