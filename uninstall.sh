#!/usr/bin/env bash
#
# nvcurve-gui uninstall.sh
# ════════════════════════════════════════════════════════════════════════
set -euo pipefail

APP_DIR="/usr/local/lib/nvcurve-gui"
POLKIT_ACTIONS_DIR="/usr/share/polkit-1/actions"
BIN_LINK="/usr/local/bin/nvcurve-gui"

ok()   { echo -e "\033[1;32m[OK]\033[0m $*"; }
warn() { echo -e "\033[1;33m[WARN]\033[0m $*"; }
err()  { echo -e "\033[1;31m[ERR]\033[0m $*" >&2; }

if [[ $EUID -ne 0 ]]; then
    err "This uninstaller must be run as root (sudo ./uninstall.sh)."
    exit 1
fi

rm -rf "$APP_DIR" && ok "Removed $APP_DIR"
rm -f "$BIN_LINK" && ok "Removed $BIN_LINK"
rm -f "$POLKIT_ACTIONS_DIR/com.nvcurve.gui.policy" && ok "Removed polkit action"
rm -f /etc/polkit-1/rules.d/49-nvcurve-gui.rules && ok "Removed polkit passwordless rule (if present)"
rm -f /usr/share/applications/nvcurve-gui.desktop && ok "Removed desktop launcher (if present)"

if command -v rc-service >/dev/null 2>&1 && [[ -d /run/openrc ]]; then
    rc-service polkit restart >/dev/null 2>&1 || true
elif command -v systemctl >/dev/null 2>&1; then
    systemctl restart polkit >/dev/null 2>&1 || true
fi

echo
read -r -p "Also remove saved V/F profiles at /etc/nvcurve? [y/N] " REPLY
if [[ "$REPLY" =~ ^[Yy]$ ]]; then
    rm -rf /etc/nvcurve
    ok "Removed /etc/nvcurve"
else
    echo "Kept /etc/nvcurve (profiles + config)."
fi

ok "Uninstall complete."
