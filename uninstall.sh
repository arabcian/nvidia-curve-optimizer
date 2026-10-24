#!/usr/bin/env bash
#
# nvcurve-gui uninstall.sh
# ════════════════════════════════════════════════════════════════════════
set -euo pipefail

PREFIX=${PREFIX:-/usr/local}
LIBEXEC=${LIBEXEC:-$PREFIX/libexec/nvcurve-gui}
UNITDIR=${UNITDIR:-/usr/lib/systemd/system}
POLKIT_ACTIONS_DIR=${POLKIT_ACTIONS_DIR:-/usr/share/polkit-1/actions}
POLKIT_RULES_DIR=${POLKIT_RULES_DIR:-/etc/polkit-1/rules.d}

ok()   { echo -e "\033[1;32m[OK]\033[0m $*"; }
warn() { echo -e "\033[1;33m[WARN]\033[0m $*"; }
err()  { echo -e "\033[1;31m[ERR]\033[0m $*" >&2; }

if [[ $EUID -ne 0 ]]; then
    err "This uninstaller must be run as root (sudo ./uninstall.sh)."
    exit 1
fi

# Boot services first: a still-enabled unit would point at binaries that are gone.
if [[ -d /run/systemd/system ]]; then
    systemctl disable --now nvcurve-autoload.service nvcurve-boot-guard.service >/dev/null 2>&1 || true
elif command -v rc-update >/dev/null 2>&1; then
    for s in nvcurve-autoload nvcurve-boot-guard; do
        rc-service "$s" --ifstarted stop >/dev/null 2>&1 || true
        for rl in /etc/runlevels/*/; do rc-update del "$s" "$(basename "$rl")" >/dev/null 2>&1 || true; done
    done
fi
rm -f /etc/init.d/nvcurve-autoload /etc/init.d/nvcurve-boot-guard \
      "$UNITDIR/nvcurve-autoload.service" "$UNITDIR/nvcurve-boot-guard.service" && ok "Removed boot services (if present)"

rm -rf "$LIBEXEC" && ok "Removed $LIBEXEC"
rm -f "$PREFIX/bin/nvcurve-gui" "$PREFIX/bin/nvcurve" && ok "Removed $PREFIX/bin/nvcurve-gui and nvcurve"
rm -f "$POLKIT_ACTIONS_DIR/com.nvcurve.gui.policy" && ok "Removed polkit action"
rm -f "$POLKIT_RULES_DIR/49-nvcurve-gui.rules" && ok "Removed polkit rules (if present)"
rm -f "$PREFIX/share/applications/nvcurve-gui.desktop" "$PREFIX/share/icons/hicolor/scalable/apps/nvcurve-gui.svg" \
    && ok "Removed desktop launcher and icon (if present)"
command -v gtk-update-icon-cache >/dev/null 2>&1 && gtk-update-icon-cache -q "$PREFIX/share/icons/hicolor" 2>/dev/null || true

if [[ -d /run/systemd/system ]]; then
    systemctl daemon-reload >/dev/null 2>&1 || true
    systemctl restart polkit >/dev/null 2>&1 || true
elif command -v rc-service >/dev/null 2>&1 && [[ -d /run/openrc ]]; then
    rc-service polkit restart >/dev/null 2>&1 || true
fi

echo
read -r -p "Also remove saved V/F profiles, config and guard state (/etc/nvcurve, /var/lib/nvcurve, /var/cache/nvcurve)? [y/N] " REPLY || REPLY=""
if [[ "$REPLY" =~ ^[Yy]$ ]]; then
    rm -rf /etc/nvcurve /var/lib/nvcurve /var/cache/nvcurve
    ok "Removed /etc/nvcurve, /var/lib/nvcurve, /var/cache/nvcurve"
else
    echo "Kept /etc/nvcurve (profiles + config) and the guard state."
fi

ok "Uninstall complete."
