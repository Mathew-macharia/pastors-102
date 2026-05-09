#!/usr/bin/env bash
# Usage: ./deploy-to-server.sh user@host [/remote/parent]
# Rsync repo to host:/remote/parent/pastors-102-v2 excluding build artifacts and editor noise.
set -euo pipefail
DEST="${1:?usage: $0 user@host [/remote/parent]}"
PARENT="${2:-/root}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
REMOTE="${DEST}:${PARENT}/"

rsync -avz --delete \
  --exclude 'target/' \
  --exclude '.venv/' \
  --exclude '__pycache__/' \
  --exclude '.cursor/' \
  --exclude 'node_modules/' \
  --exclude 'agent-tools/' \
  --exclude 'assets/' \
  --exclude '.idea/' \
  --exclude 'Thumbs.db' \
  "$ROOT/" "${REMOTE}pastors-102-v2/"

echo "Done: ${DEST}:${PARENT}/pastors-102-v2"
