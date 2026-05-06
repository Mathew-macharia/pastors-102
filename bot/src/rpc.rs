//! RPC client pool with Helius + Triton One rotation.
//!
//! Each wallet is permanently mapped to one RPC for the duration of a session
//! (round-robin or random based on config). This serves two goals:
//!   1. Op-sec: separate wallets' tx submissions ride different infrastructure
//!      paths so no single provider sees all 40 wallets' activity in one log.
//!   2. Redundancy: if one RPC degrades, only half the wallets are affected.
//!
//! Read-side calls (getLatestBlockhash, getBalance, etc.) use the primary
//! Helius URL since it has Sender + the priority-fee estimate API.

use anyhow::{Context, Result};
use rand::seq::SliceRandom;
use solana_sdk::pubkey::Pubkey;
use std::str::FromStr;
use std::sync::Arc;
use std::time::Duration;

use crate::config::Config;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RpcProvider {
    Helius,
    Triton,
}

#[derive(Debug, Clone)]
pub struct RpcEndpoint {
    pub provider: RpcProvider,
    pub url: String,
    pub label: &'static str,
}

#[derive(Debug, Clone)]
pub struct RpcPool {
    inner: Arc<RpcPoolInner>,
}

#[derive(Debug)]
struct RpcPoolInner {
    endpoints: Vec<RpcEndpoint>,
    primary: RpcEndpoint,           // for read-side calls + Sender
    sender_url_swqos: String,
    sender_url_dual: String,
    http: reqwest::Client,
}

impl RpcPool {
    pub fn new(cfg: &Config) -> Result<Self> {
        let helius = RpcEndpoint {
            provider: RpcProvider::Helius,
            url: cfg.helius_rpc_url(),
            label: "helius",
        };
        let triton = RpcEndpoint {
            provider: RpcProvider::Triton,
            url: cfg.rpc.triton_url.clone(),
            label: "triton",
        };
        let endpoints = vec![helius.clone(), triton];
        // Tuned for the sell-time hot path: 40 parallel POSTs to Sender +
        // 8 parallel POSTs to Jito BE. With HTTP/2 multiplexing, one TCP
        // conn per host can carry all 40 streams; we keep 64 idle in
        // reserve for slow-start scenarios. http2_prior_knowledge=false
        // because both Helius and Jito advertise h2 via ALPN.
        let http = reqwest::Client::builder()
            .timeout(Duration::from_millis(cfg.rpc.timeout_ms))
            .pool_max_idle_per_host(64)
            .pool_idle_timeout(Duration::from_secs(60))
            .tcp_nodelay(true)
            .http2_adaptive_window(true)
            .build()
            .context("building HTTP client")?;
        Ok(Self {
            inner: Arc::new(RpcPoolInner {
                endpoints,
                primary: helius,
                sender_url_swqos: cfg.helius_sender_url(false).to_string(),
                sender_url_dual: cfg.helius_sender_url(true).to_string(),
                http,
            }),
        })
    }

    pub fn primary(&self) -> &RpcEndpoint {
        &self.inner.primary
    }

    pub fn http(&self) -> &reqwest::Client {
        &self.inner.http
    }

    pub fn sender_swqos(&self) -> &str {
        &self.inner.sender_url_swqos
    }

    pub fn sender_dual(&self) -> &str {
        &self.inner.sender_url_dual
    }

    /// Pre-establish HTTP/2 connections to the sell-time hot endpoints
    /// (Helius Sender dual-route + the configured Jito block engine),
    /// so the first sell fire doesn't pay TCP + TLS handshake cost
    /// (~30-100 ms otherwise). Called once at bot startup.
    pub async fn warm_up(&self, jito_bundles_url: &str) {
        let urls = [
            self.inner.sender_url_dual.as_str(),
            self.inner.sender_url_swqos.as_str(),
            self.inner.primary.url.as_str(),
            jito_bundles_url,
        ];
        let body = serde_json::json!({
            "jsonrpc": "2.0",
            "id": 0,
            "method": "getHealth",
            "params": [],
        });
        let pings: Vec<_> = urls
            .iter()
            .map(|u| {
                let body = body.clone();
                async move {
                    // We don't care about the response -- just want the
                    // TCP+TLS handshake to be done and the connection
                    // to be in the pool.
                    let _ = self
                        .inner
                        .http
                        .post(*u)
                        .json(&body)
                        .timeout(Duration::from_millis(2000))
                        .send()
                        .await;
                }
            })
            .collect();
        ::futures::future::join_all(pings).await;
    }

    /// Assign a per-wallet RPC endpoint. Round-robin over the configured pool.
    pub fn assign(&self, wallet_index: usize) -> &RpcEndpoint {
        let n = self.inner.endpoints.len();
        &self.inner.endpoints[wallet_index % n]
    }

    /// Random pick (for "random" rotation strategy).
    pub fn random(&self) -> &RpcEndpoint {
        let mut rng = rand::thread_rng();
        self.inner.endpoints.choose(&mut rng).expect("non-empty")
    }

    /// Fire a JSON-RPC POST call against `url`.
    pub async fn post(
        &self,
        url: &str,
        method: &str,
        params: serde_json::Value,
    ) -> Result<serde_json::Value> {
        let body = serde_json::json!({
            "jsonrpc": "2.0",
            "id": chrono::Utc::now().timestamp_millis(),
            "method": method,
            "params": params,
        });
        let resp = self
            .inner
            .http
            .post(url)
            .json(&body)
            .send()
            .await
            .with_context(|| format!("POST {} {}", url, method))?
            .error_for_status()
            .with_context(|| format!("HTTP error from {} {}", url, method))?;
        let v: serde_json::Value = resp.json().await.context("parsing JSON-RPC response")?;
        if let Some(err) = v.get("error") {
            anyhow::bail!("{} -> {}: {}", url, method, err);
        }
        Ok(v.get("result").cloned().unwrap_or(serde_json::Value::Null))
    }

    /// Get the latest blockhash from the primary RPC.
    pub async fn get_latest_blockhash(&self) -> Result<(solana_sdk::hash::Hash, u64)> {
        let res = self
            .post(
                &self.inner.primary.url,
                "getLatestBlockhash",
                serde_json::json!([{"commitment": "confirmed"}]),
            )
            .await?;
        let blockhash_b58 = res["value"]["blockhash"]
            .as_str()
            .ok_or_else(|| anyhow::anyhow!("missing blockhash in response"))?;
        let last_valid_height = res["value"]["lastValidBlockHeight"]
            .as_u64()
            .unwrap_or_default();
        let bh = solana_sdk::hash::Hash::from_str(blockhash_b58)?;
        Ok((bh, last_valid_height))
    }

    pub async fn get_balance_lamports(&self, pubkey: &Pubkey) -> Result<u64> {
        let res = self
            .post(
                &self.inner.primary.url,
                "getBalance",
                serde_json::json!([pubkey.to_string(), {"commitment": "confirmed"}]),
            )
            .await?;
        Ok(res["value"].as_u64().unwrap_or_default())
    }

    pub async fn get_slot(&self) -> Result<u64> {
        let res = self
            .post(
                &self.inner.primary.url,
                "getSlot",
                serde_json::json!([{"commitment": "confirmed"}]),
            )
            .await?;
        Ok(res.as_u64().unwrap_or_default())
    }

    pub async fn get_token_balance_atoms(&self, ata: &Pubkey) -> Result<u64> {
        let res = self
            .post(
                &self.inner.primary.url,
                "getTokenAccountBalance",
                serde_json::json!([ata.to_string(), {"commitment": "confirmed"}]),
            )
            .await;
        match res {
            Ok(v) => Ok(v["value"]["amount"]
                .as_str()
                .and_then(|s| s.parse::<u64>().ok())
                .unwrap_or(0)),
            Err(_) => Ok(0),
        }
    }

    /// Submit a base64-encoded signed tx via standard sendTransaction on the
    /// given URL (for swap or fallback paths -- NOT for Sender/Jito).
    pub async fn send_transaction_b64(
        &self,
        url: &str,
        raw_tx_b64: &str,
        skip_preflight: bool,
    ) -> Result<String> {
        let res = self
            .post(
                url,
                "sendTransaction",
                serde_json::json!([
                    raw_tx_b64,
                    {
                        "encoding": "base64",
                        "skipPreflight": skip_preflight,
                        "maxRetries": 0
                    }
                ]),
            )
            .await?;
        Ok(res
            .as_str()
            .ok_or_else(|| anyhow::anyhow!("missing signature in sendTransaction response"))?
            .to_string())
    }
}

