# Deploy to your VPS

## SSH keys (first time)

Generate a key pair on your PC (safe to re-run; it will **not** overwrite existing keys):

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\generate-ssh-key-for-hetzner.ps1
```

Copy the printed **`ssh-ed25519 ...` line** into **Hetzner Cloud → Security → SSH keys**, then either attach that key when **creating** a new server or add the same line to `/root/.ssh/authorized_keys` on an existing server (after logging in with the root password from email).

---

Hetzner (or any Linux host) does **not** receive a `git push` from your laptop unless you set up a bare repo there. The usual flow is **clone from GitHub** on the server, or **copy** the tree with `scp` / `rsync`.

## PowerShell (Windows)

From the **repo root** (or anywhere):

```powershell
.\scripts\deploy-to-server.ps1 -ServerHost YOUR_SERVER_IP
```

If your SSH key is not the default:

```powershell
.\scripts\deploy-to-server.ps1 -ServerHost YOUR_SERVER_IP -IdentityFile "$env:USERPROFILE\.ssh\id_ed25519"
```

Different user or parent path on the server:

```powershell
.\scripts\deploy-to-server.ps1 -ServerHost YOUR_SERVER_IP -User ubuntu -RemotePath /home/ubuntu
```

**Requirements**

- OpenSSH (`scp` in `C:\Windows\System32\OpenSSH\`).
- The same **public key** you added in Hetzner must exist as the matching **private key** on the PC running the script (or use `-IdentityFile`).
- If you see `Permission denied (publickey)`, fix keys in the Hetzner console (add your pubkey) or pass `-IdentityFile`.

**After upload:** on the server, `cd ~/pastors-102-v2` (or `/root/pastors-102-v2`), install Rust/Python deps, copy any `.env` / `wallets/private` that were never in git, then `cargo build --release` in `bot/`.

## Bash (WSL / Linux / macOS)

```bash
chmod +x scripts/deploy-to-server.sh
./scripts/deploy-to-server.sh root@YOUR_SERVER_IP /root
```

Needs `rsync` and SSH key auth.
