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

### If SSH keys are not on the server yet (password upload)

Hetzner emails you a **root password**. Use it **once** locally (do not commit the password):

```powershell
cd C:\Users\Traveler\Desktop\Personal-projects\pastors-102-v2
pip install -r scripts\requirements-deploy.txt
$env:DEPLOY_SSH_PASSWORD = "PASTE_ROOT_PASSWORD_HERE"
$env:DEPLOY_SSH_HOST = "178.105.92.47"
python scripts\deploy_paramiko.py
Remove-Item Env:DEPLOY_SSH_PASSWORD
```

Then install your **public** key on the server so the next deploy uses keys:

```powershell
type $env:USERPROFILE\.ssh\id_ed25519.pub | ssh root@178.105.92.47 "mkdir -p .ssh && chmod 700 .ssh && cat >> .ssh/authorized_keys && chmod 600 .ssh/authorized_keys"
```

(Enter the same root password when prompted.) After that, `.\scripts\deploy-to-server.ps1` works without the password env.

## Bash (WSL / Linux / macOS)

```bash
chmod +x scripts/deploy-to-server.sh
./scripts/deploy-to-server.sh root@YOUR_SERVER_IP /root
```

Needs `rsync` and SSH key auth.
