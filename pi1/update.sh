#!/usr/bin/env bash
###############################################################################
# Pi 1 (Camera Pi) — Update Script
# Timelapse Art Installation
#
# Run as root:  sudo bash update.sh
#
# Pulls the latest code from the git remote, re-deploys services and configs,
# and restarts affected systemd units.  Safe to run multiple times.
###############################################################################
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# ─── colours ────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

[[ $EUID -eq 0 ]] || error "This script must be run as root (sudo bash update.sh)."

###############################################################################
# 1. Fix git directory permissions
###############################################################################
info "Fixing git repository permissions…"

if [[ ! -d "$REPO_DIR/.git" ]]; then
    error "No .git directory found in $REPO_DIR — is this a git clone?"
fi

# Determine the owner of the repo working tree (usually uid 1000 / "pi").
REPO_OWNER=$(stat -c '%U' "$REPO_DIR")
REPO_OWNER_UID=$(stat -c '%u' "$REPO_DIR")
REPO_OWNER_GID=$(stat -c '%g' "$REPO_DIR")

# Ensure the entire .git directory is owned by the repo owner so that
# both root (via this script) and the normal user can run git commands.
chown -R "${REPO_OWNER_UID}:${REPO_OWNER_GID}" "$REPO_DIR/.git"
info "Repository owner: $REPO_OWNER (uid=$REPO_OWNER_UID)"

###############################################################################
# 2. Pull latest changes
###############################################################################
info "Pulling latest changes from remote…"

# Mark the repo as safe for root (git ≥ 2.35.2 ownership check).
git config --global --get-all safe.directory 2>/dev/null | grep -qxF "$REPO_DIR" \
    || git config --global --add safe.directory "$REPO_DIR"

cd "$REPO_DIR"
BEFORE=$(git rev-parse HEAD 2>/dev/null || echo "unknown")
git pull --ff-only || error "git pull failed. Resolve any conflicts manually."
AFTER=$(git rev-parse HEAD 2>/dev/null || echo "unknown")

if [[ "$BEFORE" == "$AFTER" ]]; then
    info "Already up to date ($BEFORE)."
else
    info "Updated $BEFORE → $AFTER"
fi

# Restore ownership after pull (git operations run as root may create
# root-owned files inside .git).
chown -R "${REPO_OWNER_UID}:${REPO_OWNER_GID}" "$REPO_DIR/.git"

###############################################################################
# 3. Re-deploy services and scripts
###############################################################################
info "Re-deploying Pi 1 services…"

cp "$SCRIPT_DIR/services/capture.py"      /opt/capture.py
cp "$SCRIPT_DIR/services/portal.py"       /opt/portal.py
chmod +x /opt/capture.py /opt/portal.py

# Templates
mkdir -p /opt/templates
if [[ -d "$SCRIPT_DIR/services/templates" ]]; then
    cp -r "$SCRIPT_DIR/services/templates/"* /opt/templates/ 2>/dev/null || true
fi

# Cron scripts
cp "$SCRIPT_DIR/services/backup.sh"       /opt/backup.sh
cp "$SCRIPT_DIR/services/disk_monitor.sh" /opt/disk_monitor.sh
chmod +x /opt/backup.sh /opt/disk_monitor.sh

# Systemd units
cp "$SCRIPT_DIR/services/capture.service" /etc/systemd/system/
cp "$SCRIPT_DIR/services/portal.service"  /etc/systemd/system/
systemctl daemon-reload

###############################################################################
# 4. Restart services
###############################################################################
info "Restarting services…"
systemctl restart capture.service portal.service

info "═══════════════════════════════════════════════════════"
info " Pi 1 update complete!  ($AFTER)"
info "═══════════════════════════════════════════════════════"
