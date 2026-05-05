# pastors-bot — Pump.fun sniper

A Rust bot that fires 40 wallets at a freshly-launched Pump.fun token via
**8 atomic Jito bundles** in the same slot, with **millisecond construction
jitter** + randomized amounts + randomized priority fees, then **sells N
blocks later** in either bundled or parallel mode. Triggered by a small
**web UI** with a manual fire button or a scheduled time.

---

## Architecture

```
                             ┌──────────────────────┐
                             │   web UI (axum)      │
                             │  127.0.0.1:7777      │
                             │   button | schedule  │
                             └──────────┬───────────┘
                                        │ trigger.fire_now()
                                        ▼
        ┌──────────────────────────────────────────────────┐
        │  strategy::prepare_buy(mint, creator)            │
        │    - 40 wallets, shuffled                        │
        │    - 8 Bundles × 5 wallets                       │
        │    - per-wallet: rand(0.05-0.08) SOL             │
        │    - per-tx: rand(100k-500k) µLam/CU             │
        │    - last tx of each bundle: Jito tip            │
        │    - sign 40 VersionedTransactions               │
        └──────────────────────────────────────────────────┘
                                        │
                          ┌─────────────┴─────────────┐
                          │ strategy::fire_buy()      │
                          │ submit 8 bundles in       │
                          │ rapid sequence (µs jitter)│
                          └─────────────┬─────────────┘
                                        ▼
              ┌───────────────────────────────────────────────┐
              │  Jito Block Engine                            │
              │  https://mainnet.block-engine.jito.wtf/...    │
              │   (atomic same-slot inclusion, max 5 tx/bundle)│
              └───────────────────────────────────────────────┘

           [wait sell.after_blocks slots, default 5 = ~2 sec]

        ┌──────────────────────────────────────────────────┐
        │  strategy::fire_sell(mint, creator)              │
        │    bundled: 8 sell bundles via Jito              │
        │    OR                                            │
        │    parallel: 40 parallel sends via Sender/RPC    │
        └──────────────────────────────────────────────────┘
```

### RPC layout

| RPC                                    | Used for                                                |
| -------------------------------------- | ------------------------------------------------------- |
| Helius mainnet (`mainnet.helius-rpc…`) | Read-side: `getLatestBlockhash`, `getBalance`, `getSlot`, `getTokenAccountBalance`, fee estimate |
| Helius **Sender** (SWQoS-only)         | Sell-side `parallel` strategy fallback                  |
| **Jito Block Engine**                  | All bundle submissions (buy + sell-bundled)             |
| **Triton One**                         | Per-wallet rotation pool (alternates with Helius)       |

The 40 wallets are split round-robin between Helius and Triton for any
non-bundled traffic. Bundled traffic always goes through Jito.

---

## Layout

```
bot/
├── Cargo.toml
├── README.md
├── setup.sh                 # idempotent installer (Rust + apt deps + build)
├── config.example.toml      # template -> config.toml
├── .env.example             # template -> .env
├── .gitignore               # excludes target/, .env, config.toml, *.log
│
├── src/
│   ├── main.rs              # entry point, CLI, firing task, UI launcher
│   ├── config.rs            # config + env loader
│   ├── wallets.rs           # phase-1 wallet loader (reads ../wallets/...)
│   ├── rpc.rs               # Helius + Triton client pool
│   ├── jito.rs              # Jito Block Engine client (sendBundle + status)
│   ├── pumpfun.rs           # Pump.fun program: PDA derivations + ix builders
│   ├── strategy.rs          # buy/sell orchestration (8 bundles × 5)
│   ├── trigger.rs           # manual + scheduled triggers
│   └── ui.rs                # axum web server (POST /api/arm /fire /cancel ; GET /api/status)
│
└── static/
    └── index.html           # the UI page
```

---

## Prerequisites

1. Phase 1 done: `../wallets/wallets.csv` and `../wallets/private/<pubkey>.json`
   files exist.
2. Each of the 40 wallets is funded with at least `buy.max_sol + ~0.005`
   SOL (covers buy + tip + fees + ATA rent on first buy).
3. Three credentials in `.env` (filled in by you):
   - `HELIUS_API_KEY` — free at https://dashboard.helius.dev/
   - `TRITON_RPC_URL` — sign up at https://forms.gle/rT6nPbUE4toyPfbb7
     (or paid via https://customers.triton.one/), URL format:
     `https://your-app.mainnet.rpcpool.com/your-secret-token`
   - `PUMP_FEE_RECIPIENT` — see [Finding the fee recipient](#finding-the-pumpfun-fee-recipient)

4. Rust toolchain (handled by `setup.sh`).

---

## Finding the Pump.fun fee recipient

The Pump.fun program rotates a `fee_recipient` account each tx via its
`global` state. The buy/sell instruction takes this account as input. The
**simplest reliable way to get the current value** is to copy it from a
recent Pump.fun buy transaction:

1. Open https://solscan.io/, paste any active Pump.fun token mint into
   search.
2. Click any recent **buy** transaction.
3. Scroll to "Account Inputs" — the **second writable account in the
   list** (after the global PDA) is the `fee_recipient`. Copy it.
4. Paste into `.env` as `PUMP_FEE_RECIPIENT=...`.

This value rotates infrequently (weeks-months scale). Check before each
launch if the bot is sitting cold for a long time.

> A future version of this bot can decode the Pump.fun `global` account
> automatically — see `pumpfun::find_global_pda()`. PRs welcome.

---

## Install — Ubuntu / WSL Ubuntu

```bash
cd bot
bash setup.sh
```

`setup.sh`:

1. Installs the Rust toolchain via rustup (if missing).
2. Installs `build-essential`, `pkg-config`, `libssl-dev`, `clang` (for
   native crates like `ring` and `solana-sdk`).
3. Verifies phase 1 outputs are present.
4. Copies `config.example.toml` → `config.toml` and `.env.example` → `.env`
   (only if they don't exist yet).
5. Runs `cargo build --release`.

First build takes 3-8 minutes (Solana SDK is large). Subsequent builds
are seconds.

After install, edit `config.toml` and `.env`.

---

## Run

```bash
# Web UI mode (default)
./target/release/pastors-bot --fee-recipient $PUMP_FEE_RECIPIENT

# Or with explicit config:
./target/release/pastors-bot \
  --config config.toml \
  --fee-recipient <PUBKEY> \
  --ui-bind 127.0.0.1:7777
```

Open http://127.0.0.1:7777/ in your browser.

### UI flow

1. Enter your **token mint** (the address of the coin you launched on Pump.fun).
2. Enter the **creator wallet** (the wallet that called `create` — usually you).
3. Optionally pick a **schedule** local time + timezone.
4. Click **arm**.
5. Either click **FIRE NOW** for instant trigger, or wait for the schedule.

The status box updates every 1.5s with bundle IDs, landed slots, and the
sell results when they come in.

### Headless mode (for testing without browser)

```bash
./target/release/pastors-bot \
  --fee-recipient $PUMP_FEE_RECIPIENT \
  --headless-mint <MINT> \
  --headless-creator <CREATOR>
```

Fires immediately on the given mint+creator and exits when the sell flow
finishes.

---

## Timing model

The bot's spec is "all 40 wallets buy in the same block, varying by
milliseconds, NOT in the same instant". On Solana that maps to:

- **Same block** = same slot (~400 ms wall-clock window per leader).
- **Multiple bundles to one slot** = 8 bundles submitted within a few ms
  of each other, all targeting the next leader. Jito's block engine
  forwards them to the same upcoming leader's banking stage.
- **Millisecond variation** = `bundle.inter_bundle_jitter_us_min/max`
  controls the gap between consecutive bundle submissions. Default
  0–5000 µs (0–5 ms), keeping total spread well under a slot.
- **Within a bundle**, the 5 transactions execute **sequentially in the
  same slot** with order determined by their position in the bundle.
  The "ms variation" inside a bundle is purely cosmetic (signing
  pipeline jitter) — on chain, all 5 txs land in the same slot.

What you cannot have, on Solana, given the user spec:

- "Same block" + "wall-clock delays of seconds between txs" are
  mutually exclusive. A slot is 400 ms. That constraint is a Solana
  protocol-level limit, not a tunable.

---

## Important config values

```toml
[buy]
min_sol = 0.05            # per-wallet random buy floor
max_sol = 0.08            # per-wallet random buy ceiling
slippage_bps = 1500       # 15% slippage tolerance for snipe-time entry
priority_micro_lamports_min = 100000
priority_micro_lamports_max = 500000

[sell]
strategy = "bundled"      # "bundled" | "parallel"
after_blocks = 5          # wait 5 slots (~2s) before selling

[jito]
tip_lamports_min = 1000000     # 0.001 SOL
tip_lamports_max = 3000000     # 0.003 SOL  -- bump to 10-50M for hot launches
```

---

## Honest limitations

- **Legacy SPL only, not Token-2022.** This bot targets coins created via
  Pump.fun's `create` instruction (legacy SPL Token). Coins created via
  `create_v2` (Token-2022, used by "mayhem mode" / cashback coins) will
  not work — the bonding curve uses a different token program. If you
  launched via the standard Pump.fun web UI, it's `create` and you're
  fine.
- **The Pump.fun program updates.** This bot uses the IDL at commit
  `1059c0d9` of `pump-public-docs`. If the program upgrades (new account
  layout, new discriminators, new fee logic), the buy/sell instructions
  will need to be regenerated. Check the IDL repo before each major launch.
- **Block-0 sniping uses initial reserves** (`INITIAL_VIRTUAL_*` constants
  in `pumpfun.rs`). If the bonding curve has had buys before you fire,
  your `min_tokens_out` will be set with stale reserves and txs may
  revert. Either fire at block 0 or extend the bot to fetch live curve
  state in `prepare_buy`.
- **Same-block guarantees aren't absolute.** Jito atomic execution is
  guaranteed *within one bundle* but **not across bundles**. With 8
  bundles to one slot, expect ~80-95% same-slot land rate when tip is
  competitive (≥0.001 SOL), and 60-80% on quiet slots with smaller tips.
  Failed bundles can be re-fired but lose the block-0 advantage.
- **Sell may revert with high slippage.** This bot uses
  `min_sol_output=1` lamport on sells — i.e., accept any non-zero output
  to ensure exit. Tighten by editing `strategy::sell_bundled` if you'd
  rather hold than dump at sub-cost.
- **No anti-rug detection.** If the creator rugs (sells all tokens
  before your buys land), all 40 wallets will buy into a graveyard. This
  bot trusts the creator (you).

---

## Op-sec note (what this bot DOES NOT do)

The 40 wallets are funded from a known cluster (treasury → multi-hop →
finals). Pump.fun + most explorers will see 40 buys from related wallets
on the same coin in the same slot — this is a **textbook sybil pattern**.
Pump.fun's anti-bot heuristics are documented (SolBundler, 2026 anti-sniper
guides). Volumes that look obviously synthetic to the platform may be
filtered from "trending", penalized in incentives, or rate-limited.

This bot does not attempt to make the buys look organic. The goal is
**maximum supply capture at block 0**, not stealth. Stealth and capture
are mutually exclusive at this scale.
