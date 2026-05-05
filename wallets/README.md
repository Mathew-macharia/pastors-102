# Solana Batch Wallet Generator

Production-grade tool that generates N Solana wallets using the official
[`solana-keygen`](https://github.com/anza-xyz/agave/tree/master/keygen) binary,
tags each wallet with a unique sticky-session residential proxy, and stores
public keys, encrypted-at-rest seed phrases, and a master CSV — with random
human-paced delays between creations.

---

## Layout

```
wallets/
├── generate_wallets.py        # main script
├── setup.sh                   # one-shot Ubuntu installer (Solana CLI + Python deps)
├── requirements.txt           # Python deps: requests, base58, PySocks
├── proxies.txt.example        # template; copy to proxies.txt and edit
├── .env.example               # optional env overrides
├── .gitignore                 # excludes private/, wallets.csv, proxies.txt, logs/
├── README.md
│
├── private/                   # chmod 700 on Linux. NEVER commit.
│   ├── <pubkey>.json          # solana-keygen Vec<u8> 64-byte keypair (chmod 600)
│   └── <pubkey>.seed.txt      # 12-word BIP39 mnemonic (chmod 600)
│
├── public/
│   └── master_pubkeys.txt     # one public key per line
│
├── logs/
│   └── creation.log           # UTC per-wallet creation log
│
└── wallets.csv                # full master CSV (see schema below)
```

### `wallets.csv` schema

| column              | description                                                   |
| ------------------- | ------------------------------------------------------------- |
| `index`             | 1-based creation index                                        |
| `created_at_utc`    | ISO-8601 UTC timestamp                                        |
| `pubkey`            | base58 Solana public key (the wallet address)                 |
| `secret_key_b58`    | base58 of the full 64-byte secret key (Phantom-import-ready)  |
| `seed_phrase_12w`   | 12-word BIP39 mnemonic (Phantom/Backpack/Solflare-restorable) |
| `keypair_json_path` | absolute path to the `solana-keygen` JSON keypair             |
| `seed_phrase_path`  | absolute path to the `.seed.txt` file                         |
| `proxy_endpoint`    | base proxy endpoint (no session id) from `proxies.txt`        |
| `proxy_session_id`  | unique sticky-session id assigned to this wallet              |
| `proxy_exit_ip`     | actual residential IP seen via `api.ipify.org`, or `UNVERIFIED` |

---

## Honest scope note (read this)

`solana-keygen new` is a **purely local, offline cryptographic operation**.
No network packets leave the machine during key generation. The proxy
system in this tool is therefore a **tagging layer**:

- Each wallet is permanently bound to one residential exit IP via a unique
  sticky-session id.
- All future on-chain activity from that wallet (CEX-to-wallet funding,
  RPC calls, dApp logins, transaction broadcasts) should be routed through
  the wallet's recorded `proxy_session_id` so it never cross-contaminates
  with another wallet's IP.
- The randomized creation delay is preserved because it still mimics human
  pacing for any future wrapper service or filesystem observer, and costs
  nothing.

If you only generate keys and never use them, no fingerprint exists to
hide. The fingerprint risk is in the *use* phase.

---

## Install — Ubuntu 22.04+ / Debian / WSL Ubuntu (production)

```bash
cd wallets
bash setup.sh
```

`setup.sh` is idempotent and:

1. installs `curl`, `build-essential`, `pkg-config`, `libssl-dev`, `python3-venv`, `python3-pip`
2. installs the official Solana CLI (Agave) via `https://release.anza.xyz/stable/install`
3. creates a `.venv/` and installs `requirements.txt`
4. creates `private/` (chmod 700), `public/`, `logs/`

Pin a specific version with `SOLANA_VERSION=v3.1.9 bash setup.sh`.

After install, open a new shell (or run the export the installer prints) so
that `solana-keygen` is on `PATH`.

## Install — Windows (development only)

1. Open **Command Prompt as Administrator** and run:

   ```
   cmd /c "curl https://release.anza.xyz/stable/agave-install-init-x86_64-pc-windows-msvc.exe --output %TEMP%\agave-install-init.exe --create-dirs"
   %TEMP%\agave-install-init.exe stable
   ```

2. Close and reopen PowerShell. Verify:

   ```powershell
   solana-keygen --version
   ```

3. Install Python deps:

   ```powershell
   python -m venv .venv
   .venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   ```

> Note: `chmod` is a no-op on Windows; private key files will not have
> POSIX permissions until you migrate to Linux/WSL.

---

## Configure proxies

```bash
cp proxies.txt.example proxies.txt
$EDITOR proxies.txt   # add your real residential proxy endpoint(s)
```

**Recommended provider**: Smartproxy/Decodo residential — sticky sessions up
to 30 min, ~50M IP pool, ~$2.20/GB. Bright Data, Oxylabs and IPRoyal are
also supported; the script auto-injects `-session-XXXX` into the username,
which is the convention all four providers use.

A single base endpoint is enough — every wallet gets its own sticky
session id. Multiple endpoints (different geos) round-robin automatically.

---

## Run

```bash
# default: 40 wallets, 5-60s delays, proxies verified via ipify
python generate_wallets.py

# explicit options
python generate_wallets.py -n 40 --min-delay 5 --max-delay 60

# faster (skip per-proxy ipify check)
python generate_wallets.py --no-proxy-verify
```

The script is **resumable**: re-running appends to `wallets.csv` and
`public/master_pubkeys.txt` so a crash mid-run is non-fatal. Just re-run
with `-n` set to however many additional wallets you still need.

---

## Migrate this project from Windows to WSL Ubuntu

You're currently editing on Windows but plan to deploy on Linux. To move
the project into WSL where `chmod 600` actually takes effect:

```bash
# inside WSL Ubuntu (traveler@DESKTOP-AC8TAF8:~$)
cp -r "/mnt/c/Users/Traveler/Desktop/Personal-projects/pastors-102-v2" ~/
cd ~/pastors-102-v2/wallets
bash setup.sh
```

After this, `private/*.json` and `private/*.seed.txt` will be `chmod 600`
(owner read/write only) and `private/` itself will be `chmod 700`.

> Do **not** run the script from `/mnt/c/...` in WSL for production: the
> Windows DrvFs mount silently ignores POSIX permission bits, so private
> key files end up world-readable.

---

## Importing a generated wallet into Phantom / Backpack / Solflare

Three equally valid paths from `wallets.csv`:

1. **Seed phrase** — paste the 12 words from `seed_phrase_12w` into the
   wallet's "Import seed phrase" flow. (Works in every Solana wallet.)
2. **Private key** — paste `secret_key_b58` into "Import private key".
   (Phantom and Backpack accept this.)
3. **JSON keypair** — pass the file at `keypair_json_path` to the Solana
   CLI: `solana config set --keypair <path>`.

---

## Security checklist before going to production

- [ ] `proxies.txt` filled with **residential** (not datacenter) proxies
- [ ] Project lives at `~/...` inside WSL/Linux, not `/mnt/c/...`
- [ ] `private/` is `chmod 700`, files inside are `chmod 600` (run `ls -la private/` to verify)
- [ ] `wallets.csv` is backed up to encrypted storage (it contains every secret key)
- [ ] `.gitignore` is in effect — verify with `git check-ignore -v private/foo.json`
- [ ] Any future wrapper that *uses* these wallets routes traffic through
      `proxy_endpoint` + the recorded `proxy_session_id` for each wallet
