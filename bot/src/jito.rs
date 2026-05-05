//! Jito Block Engine bundle submission client.
//!
//! Endpoint: https://mainnet.block-engine.jito.wtf/api/v1/bundles
//! Method:   sendBundle
//! Limits:   max 5 transactions per bundle, all execute atomically in 1 slot
//!
//! Required: a SOL transfer to one of the designated tip accounts must be
//! present in at least one tx of the bundle. We always include it in the
//! LAST tx of every bundle for predictability.

use anyhow::{Context, Result};
use base64::engine::general_purpose::STANDARD as B64;
use base64::Engine;
use rand::seq::SliceRandom;
use serde::{Deserialize, Serialize};
use solana_sdk::pubkey::Pubkey;
use solana_sdk::transaction::VersionedTransaction;
use std::str::FromStr;
use std::time::Duration;

/// Jito tip accounts (mainnet-beta). Identical to Helius Sender's accounts.
/// Source: docs.jito.wtf, https://www.helius.dev/docs/sending-transactions/sender
pub const TIP_ACCOUNTS: &[&str] = &[
    "4ACfpUFoaSD9bfPdeu6DBt89gB6ENTeHBXCAi87NhDEE",
    "D2L6yPZ2FmmmTKPgzaMKdhu6EWZcTpLy1Vhx8uvZe7NZ",
    "9bnz4RShgq1hAnLnZbP8kbgBg1kEmcJBYQq3gQbmnSta",
    "5VY91ws6B2hMmBFRsXkoAAdsPHBJwRfBht4DXox3xkwn",
    "2nyhqdwKcJZR2vcqCyrYsaPVdAnFoJjiksCXJ7hfEYgD",
    "2q5pghRs6arqVjRvT5gfgWfWcHWmw1ZuCzphgd5KfWGJ",
    "wyvPkWjVZz1M8fHQnMMCDTQDbkManefNNhweYk5WkcF",
    "3KCKozbAaF75qEU33jtzozcJ29yJuaLJTy2jFdzUY8bT",
    "4vieeGHPYPG2MmyPRcYjdiDmmhN3ww7hsFNap8pVN3Ey",
    "4TQLFNWK8AovT1gFvda5jfw2oJeRMKEmw7aH6MGBJ3or",
];

pub fn random_tip_account() -> Pubkey {
    let mut rng = rand::thread_rng();
    let s = TIP_ACCOUNTS.choose(&mut rng).expect("non-empty");
    Pubkey::from_str(s).expect("valid base58")
}

#[derive(Debug, Clone)]
pub struct JitoClient {
    block_engine_url: String,
    http: reqwest::Client,
}

#[derive(Debug, Serialize, Deserialize, Clone)]
pub struct BundleStatus {
    pub bundle_id: String,
    pub transactions: Vec<String>,
    pub slot: Option<u64>,
    pub confirmation_status: Option<String>,
    pub err: Option<serde_json::Value>,
}

impl JitoClient {
    pub fn new(block_engine_url: impl Into<String>, timeout_ms: u64) -> Result<Self> {
        let http = reqwest::Client::builder()
            .timeout(Duration::from_millis(timeout_ms))
            .pool_max_idle_per_host(8)
            .build()
            .context("building Jito HTTP client")?;
        Ok(Self {
            block_engine_url: block_engine_url.into(),
            http,
        })
    }

    /// Submit a bundle of up to 5 signed VersionedTransactions to the block
    /// engine. Returns the bundle ID (SHA-256 hash of the tx signatures).
    pub async fn send_bundle(&self, txs: &[VersionedTransaction]) -> Result<String> {
        if txs.is_empty() || txs.len() > 5 {
            anyhow::bail!("bundle size must be 1..=5, got {}", txs.len());
        }
        let encoded: Vec<String> = txs
            .iter()
            .map(|t| {
                let bytes = bincode::serialize(t).expect("serialize");
                B64.encode(bytes)
            })
            .collect();
        let body = serde_json::json!({
            "jsonrpc": "2.0",
            "id": chrono::Utc::now().timestamp_millis(),
            "method": "sendBundle",
            "params": [encoded, {"encoding": "base64"}],
        });
        let resp = self
            .http
            .post(&self.block_engine_url)
            .json(&body)
            .send()
            .await
            .with_context(|| format!("POST {} sendBundle", self.block_engine_url))?
            .error_for_status()
            .context("HTTP error from Jito")?;
        let v: serde_json::Value = resp.json().await?;
        if let Some(err) = v.get("error") {
            anyhow::bail!("jito sendBundle error: {}", err);
        }
        let bundle_id = v
            .get("result")
            .and_then(|x| x.as_str())
            .ok_or_else(|| anyhow::anyhow!("missing bundle_id in response: {}", v))?
            .to_string();
        Ok(bundle_id)
    }

    /// Poll bundle status. Returns when finalized or after timeout.
    pub async fn poll_status(
        &self,
        bundle_id: &str,
        timeout: Duration,
    ) -> Result<Option<BundleStatus>> {
        let deadline = std::time::Instant::now() + timeout;
        loop {
            let body = serde_json::json!({
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getBundleStatuses",
                "params": [[bundle_id]],
            });
            let resp = self
                .http
                .post(&self.block_engine_url)
                .json(&body)
                .send()
                .await?
                .error_for_status()?;
            let v: serde_json::Value = resp.json().await?;
            if let Some(arr) = v["result"]["value"].as_array() {
                if let Some(item) = arr.first() {
                    if !item.is_null() {
                        let cs = item["confirmation_status"].as_str().map(|s| s.to_string());
                        let slot = item["slot"].as_u64();
                        let err = item.get("err").cloned();
                        let status = BundleStatus {
                            bundle_id: bundle_id.into(),
                            transactions: item["transactions"]
                                .as_array()
                                .map(|a| {
                                    a.iter()
                                        .filter_map(|x| x.as_str().map(|s| s.to_string()))
                                        .collect()
                                })
                                .unwrap_or_default(),
                            slot,
                            confirmation_status: cs.clone(),
                            err,
                        };
                        if matches!(cs.as_deref(), Some("confirmed") | Some("finalized")) {
                            return Ok(Some(status));
                        }
                    }
                }
            }
            if std::time::Instant::now() >= deadline {
                return Ok(None);
            }
            tokio::time::sleep(Duration::from_millis(500)).await;
        }
    }
}
