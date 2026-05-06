# pastors-bot — Pump.fun sniper

A Rust bot that fires 40 wallets at a freshly-launched Pump.fun token via
**8 atomic Jito bundles** in the same slot, with **millisecond construction
jitter** + randomized amounts + randomized priority fees, then **sells N
blocks later** via a **DUAL-PATH simultaneous fire** — every signed sell
tx is broadcast through BOTH a Jito bundle AND Helius Sender's dual-route
at the same instant. First-to-land per signature wins, the network
deduplicates, stragglers retry on bumped fees. Triggered by a small **web
UI** with a manual fire button or a scheduled time. `N` is configurable
via either the `SELL_AFTER_BLOCKS` env var or the UI input — your choice.

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

           [wait N slots after the earliest landed buy bundle.
            N comes from (in order of precedence):
              1. UI "Sell after N blocks" input
              2. SELL_AFTER_BLOCKS env var
              3. config.toml -> sell.after_blocks
              4. compiled-in default = 5 slots = ~2 s]

        ┌──────────────────────────────────────────────────────────┐
        │  strategy::fire_sell(mint, creator)                      │
        │   FIRST WAVE:                                            │
        │     1. parallel balance fetch (40 RPCs concurrently)     │
        │     2. ONE fresh blockhash, build + sign 40 sell txs     │
        │        (each tx contains its own tip transfer so it's    │
        │         independently submittable via either path)       │
        │     3. spawn 48 concurrent tokio tasks:                  │
        │          ─ 40 × POST /sender/fast  (dual-route)          │
        │          ─  8 × POST /jito sendBundle (5 txs each)       │
        │        all 48 fires within bounded scheduler latency,    │
        │        HTTP/2 multiplexed; submission spread is sub-ms   │
        │     4. wait ~1.5 s, batch-query 40 sigs at once          │
        │   RETRY WAVES (up to 2):                                 │
        │     5. for each wallet still pending:                    │
        │          fresh blockhash + bumped prio + bumped tip      │
        │          re-fire dual-path                               │
        │     6. batch sweep again                                 │
        │   FINAL: each wallet lands via WHICHEVER path's copy     │
        │   reached the leader first (network dedupes by sig).     │
        └──────────────────────────────────────────────────────────┘
```

### RPC layout

| RPC                                    | Used for                                                |
| -------------------------------------- | ------------------------------------------------------- |
| Helius mainnet (`mainnet.helius-rpc…`) | Read-side: `getLatestBlockhash`, `getBalance`, `getSlot`, `getTokenAccountBalance`, signature status |
| Helius **Sender** (dual-route)         | One of the two sell-side paths (40 standalone POSTs per wave) |
| **Jito Block Engine**                  | Buy-side bundle submissions (8 bundles × 5 wallets) AND second sell path (8 parallel bundles per wave) |
| **Triton One**                         | Per-wallet rotation pool (alternates with Helius for read traffic) |

The 40 wallets are split round-robin between Helius and Triton for read
traffic. Buys go through Jito only. Sells go through Jito AND Sender
**simultaneously** — both paths fire at the same instant, network
deduplication ensures at most one copy lands per wallet.

### Why dual-path simultaneous sells

`Bundled-only`, `parallel-only` and `dual-path` were all prototyped. The
dual-path variant is shipped because it strictly dominates either single
path on every metric that matters at exit:

| Metric                              | Jito-only          | Sender-only        | **Dual-path**            |
| ----------------------------------- | ------------------ | ------------------ | ------------------------ |
| Submission spread (40 wallets)      | ~50 ms (8 parallel POSTs) | ~50 ms (40 parallel POSTs) | **~5 ms (48 parallel POSTs)** |
| Per-wallet inclusion latency        | 1 slot if bundle survives, never otherwise | 1–2 slots          | **min(both)**            |
| Single-tx revert in same bundle     | **Whole bundle of 5 dies** | Other 39 unaffected | Sender backup catches the 4 innocents |
| Retry granularity                   | 5-tx bundle        | One wallet         | One wallet               |
| Fees + tip per wallet (avg)         | ~0.0007 SOL        | ~0.0008 SOL        | ~0.0008 SOL (one tip per tx, used by either path) |
| Same-slot land rate (40 wallets)    | 60–95% (depends on bundle survival) | 50–85% | **80–98%**          |

Mechanism: every sell tx has the **same signature** regardless of which
path forwards it (the leader processes a signature exactly once per
slot). When a bundle reverts atomically because one of its 5 txs has no
tokens to sell, the OTHER 4 txs in that bundle are simultaneously
in-flight via Sender as standalone subs and land independently.
Conversely if Sender's copy hits the leader first, the Jito bundle's
copy of that sig reports "already processed" and the bundle's
remaining 4 sigs decide their own fate. No double-spend risk.

Submission jitter across all 48 outbound POSTs is bounded by tokio task
scheduling latency + HTTP/2 stream multiplexing on persistent
keep-alive connections — typically sub-millisecond. We pre-warm the
connections to Sender + Jito BE at bot startup so the first FIRE
doesn't pay TCP+TLS handshake cost.

### Honest physical limit

Submission spread can be sub-millisecond. **Inclusion latency cannot.**
Solana's slot time is ~400 ms — a signed tx cannot land sooner than the
next leader produces a block, regardless of how many milliseconds you
shave off submission. The dual-path design optimises for the things
software *can* control:

1. Maximise the chance every signature reaches the next leader (two
   independent paths, each with fast-lane forwarding).
2. Minimise the window between first and last submission so all 40 sigs
   target the same slot.
3. Detect stragglers in 1.5 s (3-4 slots) and re-fire only those with
   bumped fees, never blocking the fast-path wallets.

What is not achievable on Solana, and the bot does not pretend
otherwise: 40 transactions confirmed in microseconds. The protocol
floor is one slot ≈ 400 ms.

The dual-route Sender (`https://sender.helius-rpc.com/fast` without
`?swqos_only=true`) forwards each tx to BOTH the Jito block engine and
validator-attached SWQoS staked connections in parallel. Combined with
our own direct Jito bundle path, every sell tx ends up on **three**
independent forwarding routes (Sender→Jito-auction, Sender→SWQoS, our
own Jito-bundle). Min tip per tx 0.0002 SOL.

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
4. Optionally set **Sell after N blocks** (overrides config + env). Leave
   blank to use whatever's in `.env` (`SELL_AFTER_BLOCKS`) or `config.toml`
   (`sell.after_blocks`).
5. Click **arm**.
6. Either click **FIRE NOW** for instant trigger, or wait for the schedule.

The status box updates every 1.5s with bundle IDs, landed slots, the
override `N`, and the per-wallet sell results when they come in
(signature, attempt count, status, error if any).

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
after_blocks = 5          # wait 5 slots (~2s) before selling
                          #   overrides:  SELL_AFTER_BLOCKS env  >  UI input  > this value
slippage_bps = 2000       # informational; sell currently uses min_sol_output=1
priority_micro_lamports_min = 100000
priority_micro_lamports_max = 500000
compute_unit_limit = 100000

[jito]
tip_lamports_min = 1000000     # 0.001 SOL  -- buy bundles only
tip_lamports_max = 3000000     # 0.003 SOL  -- bump to 10-50M for hot launches
                               # sells use these values too but FLOORED at
                               # 0.0002 SOL (Helius Sender dual-route minimum)
```

### Override `N` (sell-after-blocks) at runtime

Three ways, highest precedence first:

```bash
# 1. UI input (per-fire override)
#    -> "Sell after N blocks" field on http://127.0.0.1:7777/

# 2. Environment variable (process-wide override)
SELL_AFTER_BLOCKS=25 ./target/release/pastors-bot --fee-recipient $FR

# 3. config.toml ([sell] after_blocks = ...)
```

`N=0` sells immediately after the buy lands. `N=750` (~5 min) lets you
catch the typical "first pump" of a Pump.fun launch.

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
  `min_sol_output=1` lamport on sells — accept any non-zero output to
  guarantee exit. Tighten by editing the `min_sol_output` constant in
  `strategy::build_sell_wave` if you'd rather hold than dump at sub-cost.
- **Sell will retry, but it will not retry forever.** Up to 3 waves per
  wallet (1 first wave + 2 retry waves), each with a fresh blockhash and
  bumped priority + tip. After the third wave, the wallet is marked
  `failed` in the status box. Common causes: wallet ran out of lamports,
  on-chain revert unrelated to slippage, network sustained outage. You
  can re-fire manually after fixing the cause — the bot reads token
  balances live each fire, so it will pick up tokens that didn't sell
  on the first run.
- **No anti-rug detection.** If the creator rugs (sells all tokens
  before your buys land), all 40 wallets will buy into a graveyard. This
  bot trusts the creator (you).

---

## Op-sec & Clustering Immunity

By itself, this bot just executes buys and sells. **Stealth is entirely dependent on how you fund and sweep the wallets.**

If you use the deprecated on-chain funders (`funder/` or `multihop/`), Pump.fun and 2026 sniper bots (via APIs like Sybil Shield) will instantly flag your 40 wallets as a single Wash Trade / Rug cluster. They trace the funding back to your single Treasury wallet.

**To achieve true stealth and "organic" bot metrics, you MUST use the full 4-phase pipeline outlined in the root README:**
1. Fund the 40 wallets *directly* from Binance using `binance_funder/` (breaks the inflow cluster).
2. Buy and sell with this bot.
3. Sweep the profits to 40 *unique* CEX deposit addresses using `sweeper/` (breaks the outflow cluster).
4. **Throw the wallets away.** If you reuse them for a second token launch, Behavioral Clustering algorithms will flag you. One launch = 40 fresh wallets.
