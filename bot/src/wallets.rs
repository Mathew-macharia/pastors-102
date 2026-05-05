//! Wallet loader: reads the 40 keypairs produced by phase 1.
//!
//! Phase 1 stores each wallet as `private/<pubkey>.json` containing the
//! standard 64-byte `solana-keygen` Vec<u8> array. The `wallets.csv` lists
//! all 40 pubkeys. We load them in CSV order and verify the on-disk
//! keypair matches.

use anyhow::{anyhow, Context, Result};
use solana_sdk::{pubkey::Pubkey, signature::Keypair, signer::Signer};
use std::path::{Path, PathBuf};
use std::str::FromStr;

#[derive(Debug)]
pub struct LoadedWallet {
    pub index: usize,
    pub pubkey: Pubkey,
    pub keypair: Keypair,
    pub keypair_path: PathBuf,
}

/// Load all wallets listed in `wallets_csv`, looking up each keypair file
/// in `private_dir/<pubkey>.json`.
pub fn load_all(wallets_csv: &Path, private_dir: &Path) -> Result<Vec<LoadedWallet>> {
    let csv_text = std::fs::read_to_string(wallets_csv)
        .with_context(|| format!("reading {}", wallets_csv.display()))?;
    let mut lines = csv_text.lines();
    let header = lines.next().ok_or_else(|| anyhow!("empty CSV"))?;
    let cols: Vec<&str> = header.split(',').collect();
    let pubkey_idx = cols
        .iter()
        .position(|c| c.trim() == "pubkey")
        .ok_or_else(|| anyhow!("CSV missing 'pubkey' column"))?;

    let mut out = Vec::new();
    let mut seen = std::collections::HashSet::new();
    for (i, line) in lines.enumerate() {
        if line.trim().is_empty() {
            continue;
        }
        let fields: Vec<&str> = line.split(',').collect();
        let pubkey_str = fields
            .get(pubkey_idx)
            .ok_or_else(|| anyhow!("row {} missing pubkey field", i + 1))?
            .trim();
        if pubkey_str.is_empty() || !seen.insert(pubkey_str.to_string()) {
            continue;
        }
        let pubkey = Pubkey::from_str(pubkey_str)
            .with_context(|| format!("invalid pubkey at row {}: {}", i + 1, pubkey_str))?;

        let kp_path = private_dir.join(format!("{}.json", pubkey_str));
        let raw = std::fs::read_to_string(&kp_path)
            .with_context(|| format!("reading {}", kp_path.display()))?;
        let bytes: Vec<u8> = serde_json::from_str(&raw)
            .with_context(|| format!("parsing {} as JSON byte array", kp_path.display()))?;
        if bytes.len() != 64 {
            anyhow::bail!(
                "{}: expected 64-byte keypair, got {}",
                kp_path.display(),
                bytes.len()
            );
        }
        let kp = Keypair::from_bytes(&bytes)
            .map_err(|e| anyhow!("{}: invalid keypair: {}", kp_path.display(), e))?;
        if kp.pubkey() != pubkey {
            anyhow::bail!(
                "{}: keypair pubkey {} does not match CSV pubkey {}",
                kp_path.display(),
                kp.pubkey(),
                pubkey
            );
        }
        out.push(LoadedWallet {
            index: out.len() + 1,
            pubkey,
            keypair: kp,
            keypair_path: kp_path,
        });
    }
    if out.is_empty() {
        anyhow::bail!("no wallets loaded from {}", wallets_csv.display());
    }
    Ok(out)
}
