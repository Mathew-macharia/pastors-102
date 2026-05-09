"""
Upload this repo to a VPS over SSH using the root password (Hetzner email).

  pip install paramiko scp
  set DEPLOY_SSH_PASSWORD=your_root_password
  python scripts/deploy_paramiko.py

Env (optional):
  DEPLOY_SSH_HOST      default 178.105.92.47
  DEPLOY_SSH_USER      default root
  DEPLOY_REMOTE_DIR    default /root/pastors-102-v2
"""

from __future__ import annotations

import os
import shlex
import shutil
import sys
import tempfile
from pathlib import Path

try:
    import paramiko
    from scp import SCPClient
except ImportError:
    print("Install: pip install paramiko scp", file=sys.stderr)
    raise SystemExit(1)

ROOT = Path(__file__).resolve().parent.parent
EXCLUDE_DIRS = frozenset(
    {
        "target",
        ".venv",
        "__pycache__",
        ".cursor",
        "node_modules",
        "agent-tools",
        "assets",
        ".idea",
    }
)


def should_skip(_src: Path, names: list[str]) -> set[str]:
    return {n for n in names if n in EXCLUDE_DIRS}


def main() -> None:
    host = os.environ.get("DEPLOY_SSH_HOST", "178.105.92.47")
    user = os.environ.get("DEPLOY_SSH_USER", "root")
    password = os.environ.get("DEPLOY_SSH_PASSWORD", "")
    remote_dir = os.environ.get("DEPLOY_REMOTE_DIR", "/root/pastors-102-v2")

    if not password:
        print(
            "ERROR: set DEPLOY_SSH_PASSWORD to your VPS root password (from Hetzner email).",
            file=sys.stderr,
        )
        raise SystemExit(1)

    parent = str(Path(remote_dir).parent)
    staging = Path(tempfile.mkdtemp(prefix="pastors-deploy-"))
    try:
        dest = staging / "pastors-102-v2"
        print(f"Staging -> {dest}")
        shutil.copytree(ROOT, dest, ignore=should_skip, dirs_exist_ok=True)

        print(f"SSH {user}@{host} ...")
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(hostname=host, username=user, password=password, timeout=30)

        cmd = f"mkdir -p {shlex.quote(parent)} && rm -rf {shlex.quote(remote_dir)}"
        _, stdout, stderr = client.exec_command(cmd)
        code = stdout.channel.recv_exit_status()
        if code != 0:
            err = stderr.read().decode(errors="replace")
            print(f"Remote setup failed ({code}): {err}", file=sys.stderr)
            raise SystemExit(1)

        print(f"Uploading to {remote_dir} ...")
        with SCPClient(client.get_transport()) as scp:
            scp.put(str(dest), remote_path=parent, recursive=True)

        client.close()
        print("Done.")
        print(f"  ssh {user}@{host}")
        print(f"  cd {remote_dir}")
    finally:
        shutil.rmtree(staging, ignore_errors=True)


if __name__ == "__main__":
    main()
