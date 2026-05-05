//! Pump.fun program client.
//!
//! Program: 6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P  (mainnet + devnet)
//!
//! IDL source (commit 1059c0d9):
//!   https://github.com/pump-fun/pump-public-docs/blob/main/idl/pump.json
//!
//! Account ordering and discriminators are taken DIRECTLY from the canonical
//! IDL. Any program upgrade that adds/removes accounts in buy/sell will break
//! this module -- regenerate from the latest IDL when that happens.

use anyhow::{anyhow, Context, Result};
use solana_sdk::{
    instruction::{AccountMeta, Instruction},
    pubkey,
    pubkey::Pubkey,
    system_program,
};
use spl_associated_token_account::get_associated_token_address;

pub const PUMP_PROGRAM_ID: Pubkey =
    pubkey!("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P");

// SPL Token (legacy). Pump.fun coins created via `create` use this program.
// Coins created via `create_v2` use Token-2022 -- not handled here.
pub const TOKEN_PROGRAM_ID: Pubkey =
    pubkey!("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA");

// Fee program (separate program holding fee_config PDAs). From the IDL.
pub const PUMP_FEE_PROGRAM_ID: Pubkey =
    pubkey!("pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ");

// Anchor instruction discriminators (8-byte prefix on instruction data).
// Source: pump-public-docs/idl/pump.ts at commit 1059c0d9.
pub const BUY_EXACT_SOL_IN_DISCRIMINATOR: [u8; 8] =
    [56, 252, 116, 8, 158, 223, 205, 95];
pub const SELL_DISCRIMINATOR: [u8; 8] =
    [51, 230, 133, 164, 1, 127, 131, 173];

// BondingCurve account discriminator.
pub const BONDING_CURVE_DISCRIMINATOR: [u8; 8] =
    [23, 183, 248, 55, 96, 216, 172, 96];

// 32-byte constant used as the second seed of fee_config PDA. Hard-coded
// in the IDL (the fee_config admin authority pubkey).
pub const FEE_CONFIG_SEED2: [u8; 32] = [
    1, 86, 224, 246, 147, 102, 90, 207, 68, 219, 21, 104, 191, 23, 91, 170,
    81, 137, 203, 151, 245, 210, 255, 59, 101, 93, 43, 182, 253, 109, 24, 176,
];

// Initial reserves for a freshly-minted Pump.fun coin (from Global config).
pub const INITIAL_VIRTUAL_TOKEN_RESERVES: u64 = 1_073_000_000_000_000;
pub const INITIAL_VIRTUAL_SOL_RESERVES: u64 = 30_000_000_000;
pub const INITIAL_REAL_TOKEN_RESERVES: u64 = 793_100_000_000_000;
pub const TOKEN_TOTAL_SUPPLY: u64 = 1_000_000_000_000_000;

// --- PDA derivations ------------------------------------------------------
pub fn find_global_pda() -> (Pubkey, u8) {
    Pubkey::find_program_address(&[b"global"], &PUMP_PROGRAM_ID)
}

pub fn find_bonding_curve_pda(mint: &Pubkey) -> (Pubkey, u8) {
    Pubkey::find_program_address(&[b"bonding-curve", mint.as_ref()], &PUMP_PROGRAM_ID)
}

pub fn find_creator_vault_pda(creator: &Pubkey) -> (Pubkey, u8) {
    Pubkey::find_program_address(&[b"creator-vault", creator.as_ref()], &PUMP_PROGRAM_ID)
}

pub fn find_event_authority_pda() -> (Pubkey, u8) {
    Pubkey::find_program_address(&[b"__event_authority"], &PUMP_PROGRAM_ID)
}

pub fn find_global_volume_accumulator_pda() -> (Pubkey, u8) {
    Pubkey::find_program_address(&[b"global_volume_accumulator"], &PUMP_PROGRAM_ID)
}

pub fn find_user_volume_accumulator_pda(user: &Pubkey) -> (Pubkey, u8) {
    Pubkey::find_program_address(
        &[b"user_volume_accumulator", user.as_ref()],
        &PUMP_PROGRAM_ID,
    )
}

pub fn find_fee_config_pda() -> (Pubkey, u8) {
    Pubkey::find_program_address(
        &[b"fee_config", &FEE_CONFIG_SEED2],
        &PUMP_FEE_PROGRAM_ID,
    )
}

pub fn associated_bonding_curve(bonding_curve: &Pubkey, mint: &Pubkey) -> Pubkey {
    get_associated_token_address(bonding_curve, mint)
}

// --- BondingCurve account decoder -----------------------------------------
#[derive(Debug, Clone)]
pub struct BondingCurveState {
    pub virtual_token_reserves: u64,
    pub virtual_sol_reserves: u64,
    pub real_token_reserves: u64,
    pub real_sol_reserves: u64,
    pub token_total_supply: u64,
    pub complete: bool,
    pub creator: Pubkey,
}

impl BondingCurveState {
    pub fn decode(raw: &[u8]) -> Result<Self> {
        if raw.len() < 8 + 8 * 5 + 1 + 32 {
            anyhow::bail!("bonding curve account too small: {} bytes", raw.len());
        }
        if raw[..8] != BONDING_CURVE_DISCRIMINATOR {
            anyhow::bail!("not a BondingCurve account (discriminator mismatch)");
        }
        let mut p = 8;
        let mut take = |n: usize| {
            let s = &raw[p..p + n];
            p += n;
            s
        };
        let virtual_token_reserves = u64::from_le_bytes(take(8).try_into()?);
        let virtual_sol_reserves = u64::from_le_bytes(take(8).try_into()?);
        let real_token_reserves = u64::from_le_bytes(take(8).try_into()?);
        let real_sol_reserves = u64::from_le_bytes(take(8).try_into()?);
        let token_total_supply = u64::from_le_bytes(take(8).try_into()?);
        let complete = take(1)[0] != 0;
        let creator_bytes: [u8; 32] = take(32).try_into()?;
        Ok(Self {
            virtual_token_reserves,
            virtual_sol_reserves,
            real_token_reserves,
            real_sol_reserves,
            token_total_supply,
            complete,
            creator: Pubkey::new_from_array(creator_bytes),
        })
    }
}

// --- Bonding curve quote --------------------------------------------------
//
// Per the IDL docs for buyExactSolIn:
//   net_sol = floor(spendable_sol_in * 10000 / (10000 + total_fee_bps))
//   tokens_out = floor((net_sol - 1) * virtual_token_reserves
//                       / (virtual_sol_reserves + net_sol - 1))
//
// We don't know total_fee_bps without reading global state. For block-0
// snipes a typical Pump.fun fee is 100 bps protocol + 50 bps creator = 150 bps.
// Use that as the safe default; a bot operator can edit if launches change.
pub const DEFAULT_PROTOCOL_FEE_BPS: u64 = 100;
pub const DEFAULT_CREATOR_FEE_BPS: u64 = 50;

pub fn estimate_tokens_out(
    spendable_sol_in: u64,
    virtual_sol_reserves: u64,
    virtual_token_reserves: u64,
) -> u64 {
    if spendable_sol_in == 0 || virtual_sol_reserves == 0 || virtual_token_reserves == 0 {
        return 0;
    }
    let total_fee_bps = DEFAULT_PROTOCOL_FEE_BPS + DEFAULT_CREATOR_FEE_BPS;
    let net_sol = (spendable_sol_in as u128) * 10_000 / (10_000 + total_fee_bps as u128);
    if net_sol <= 1 {
        return 0;
    }
    let dx = net_sol - 1;
    let x = virtual_sol_reserves as u128;
    let y = virtual_token_reserves as u128;
    let tokens_out = y.checked_mul(dx).unwrap_or(0) / (x + dx);
    tokens_out.min(u64::MAX as u128) as u64
}

pub fn apply_slippage_floor(quoted_out: u64, slippage_bps: u32) -> u64 {
    let bps = slippage_bps.min(10_000) as u128;
    let q = quoted_out as u128;
    let floor = q * (10_000 - bps) / 10_000;
    floor.min(u64::MAX as u128) as u64
}

// --- buyExactSolIn instruction --------------------------------------------
//
// Per IDL (idl/pump.ts at 1059c0d9, lines 715-1083). 16 accounts in this
// EXACT order:
//   0  global                          (PDA, readonly)
//   1  fee_recipient                   (writable)
//   2  mint                            (readonly)
//   3  bonding_curve                   (PDA, writable)
//   4  associated_bonding_curve        (ATA of bonding_curve for mint, writable)
//   5  associated_user                 (ATA of user for mint, writable)
//   6  user                            (signer, writable)
//   7  system_program                  (readonly)
//   8  token_program                   (readonly)
//   9  creator_vault                   (PDA, writable)
//   10 event_authority                 (PDA, readonly)
//   11 program                         (Pump program ID, readonly)
//   12 global_volume_accumulator       (PDA, readonly)
//   13 user_volume_accumulator         (PDA, writable)
//   14 fee_config                      (PDA on fee_program, readonly)
//   15 fee_program                     (readonly)

#[derive(Debug, Clone, Copy)]
pub struct BuyParams {
    pub spendable_sol_in: u64,
    pub min_tokens_out: u64,
    pub track_volume: Option<bool>,
}

pub fn build_buy_exact_sol_in_ix(
    user: &Pubkey,
    mint: &Pubkey,
    creator: &Pubkey,
    fee_recipient: &Pubkey,
    params: BuyParams,
) -> Instruction {
    let (global, _) = find_global_pda();
    let (bonding_curve, _) = find_bonding_curve_pda(mint);
    let assoc_bonding_curve = associated_bonding_curve(&bonding_curve, mint);
    let assoc_user = get_associated_token_address(user, mint);
    let (creator_vault, _) = find_creator_vault_pda(creator);
    let (event_authority, _) = find_event_authority_pda();
    let (global_vol, _) = find_global_volume_accumulator_pda();
    let (user_vol, _) = find_user_volume_accumulator_pda(user);
    let (fee_config, _) = find_fee_config_pda();

    let accounts = vec![
        AccountMeta::new_readonly(global, false),
        AccountMeta::new(*fee_recipient, false),
        AccountMeta::new_readonly(*mint, false),
        AccountMeta::new(bonding_curve, false),
        AccountMeta::new(assoc_bonding_curve, false),
        AccountMeta::new(assoc_user, false),
        AccountMeta::new(*user, true),
        AccountMeta::new_readonly(system_program::id(), false),
        AccountMeta::new_readonly(TOKEN_PROGRAM_ID, false),
        AccountMeta::new(creator_vault, false),
        AccountMeta::new_readonly(event_authority, false),
        AccountMeta::new_readonly(PUMP_PROGRAM_ID, false),
        AccountMeta::new_readonly(global_vol, false),
        AccountMeta::new(user_vol, false),
        AccountMeta::new_readonly(fee_config, false),
        AccountMeta::new_readonly(PUMP_FEE_PROGRAM_ID, false),
    ];

    let mut data = Vec::with_capacity(8 + 8 + 8 + 2);
    data.extend_from_slice(&BUY_EXACT_SOL_IN_DISCRIMINATOR);
    data.extend_from_slice(&params.spendable_sol_in.to_le_bytes());
    data.extend_from_slice(&params.min_tokens_out.to_le_bytes());
    encode_option_bool(&mut data, params.track_volume);

    Instruction {
        program_id: PUMP_PROGRAM_ID,
        accounts,
        data,
    }
}

// --- sell instruction -----------------------------------------------------
//
// Per IDL (idl/pump.ts at 1059c0d9, lines 3393-3655). 14 required accounts
// in this EXACT order (note: order DIFFERS from buy -- creator_vault is
// before token_program in sell, NO volume accumulators):
//   0  global                       (PDA, readonly)
//   1  fee_recipient                (writable)
//   2  mint                         (readonly)
//   3  bonding_curve                (PDA, writable)
//   4  associated_bonding_curve     (writable)
//   5  associated_user              (writable)
//   6  user                         (signer, writable)
//   7  system_program               (readonly)
//   8  creator_vault                (PDA, writable)
//   9  token_program                (readonly)
//   10 event_authority              (readonly)
//   11 program                      (readonly)
//   12 fee_config                   (readonly)
//   13 fee_program                  (readonly)
//
// Args: amount (u64), min_sol_output (u64) -- NO track_volume in sell.
//
// For cashback coins, user_volume_accumulator can optionally be passed as
// remaining_accounts[0] (we don't pass it).

#[derive(Debug, Clone, Copy)]
pub struct SellParams {
    pub amount: u64,
    pub min_sol_output: u64,
}

pub fn build_sell_ix(
    user: &Pubkey,
    mint: &Pubkey,
    creator: &Pubkey,
    fee_recipient: &Pubkey,
    params: SellParams,
) -> Instruction {
    let (global, _) = find_global_pda();
    let (bonding_curve, _) = find_bonding_curve_pda(mint);
    let assoc_bonding_curve = associated_bonding_curve(&bonding_curve, mint);
    let assoc_user = get_associated_token_address(user, mint);
    let (creator_vault, _) = find_creator_vault_pda(creator);
    let (event_authority, _) = find_event_authority_pda();
    let (fee_config, _) = find_fee_config_pda();

    let accounts = vec![
        AccountMeta::new_readonly(global, false),
        AccountMeta::new(*fee_recipient, false),
        AccountMeta::new_readonly(*mint, false),
        AccountMeta::new(bonding_curve, false),
        AccountMeta::new(assoc_bonding_curve, false),
        AccountMeta::new(assoc_user, false),
        AccountMeta::new(*user, true),
        AccountMeta::new_readonly(system_program::id(), false),
        AccountMeta::new(creator_vault, false),
        AccountMeta::new_readonly(TOKEN_PROGRAM_ID, false),
        AccountMeta::new_readonly(event_authority, false),
        AccountMeta::new_readonly(PUMP_PROGRAM_ID, false),
        AccountMeta::new_readonly(fee_config, false),
        AccountMeta::new_readonly(PUMP_FEE_PROGRAM_ID, false),
    ];

    let mut data = Vec::with_capacity(8 + 8 + 8);
    data.extend_from_slice(&SELL_DISCRIMINATOR);
    data.extend_from_slice(&params.amount.to_le_bytes());
    data.extend_from_slice(&params.min_sol_output.to_le_bytes());

    Instruction {
        program_id: PUMP_PROGRAM_ID,
        accounts,
        data,
    }
}

fn encode_option_bool(out: &mut Vec<u8>, v: Option<bool>) {
    match v {
        None => out.push(0),
        Some(b) => {
            out.push(1);
            out.push(if b { 1 } else { 0 });
        }
    }
}

/// Helper: idempotent ATA creation for the user's mint ATA.
pub fn create_user_ata_idempotent_ix(
    payer: &Pubkey,
    user: &Pubkey,
    mint: &Pubkey,
) -> Instruction {
    spl_associated_token_account::instruction::create_associated_token_account_idempotent(
        payer,
        user,
        mint,
        &TOKEN_PROGRAM_ID,
    )
}

/// Fetch + decode the BondingCurve state for a given mint.
pub async fn fetch_bonding_curve_state(
    rpc: &crate::rpc::RpcPool,
    mint: &Pubkey,
) -> Result<Option<BondingCurveState>> {
    let (bc_pda, _) = find_bonding_curve_pda(mint);
    let res = rpc
        .post(
            &rpc.primary().url,
            "getAccountInfo",
            serde_json::json!([
                bc_pda.to_string(),
                {"encoding": "base64", "commitment": "confirmed"}
            ]),
        )
        .await?;
    let value = res.get("value").cloned().unwrap_or(serde_json::Value::Null);
    if value.is_null() {
        return Ok(None);
    }
    let data_arr = value
        .get("data")
        .and_then(|d| d.as_array())
        .ok_or_else(|| anyhow!("missing data array in account info"))?;
    let b64 = data_arr
        .first()
        .and_then(|x| x.as_str())
        .ok_or_else(|| anyhow!("missing base64 data"))?;
    use base64::engine::general_purpose::STANDARD as B64;
    use base64::Engine;
    let raw = B64.decode(b64).context("base64 decoding account data")?;
    Ok(Some(BondingCurveState::decode(&raw)?))
}
