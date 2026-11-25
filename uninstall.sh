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
    systemctl disable --now nvcurve-gui-autoload.service nvcurve-gui-boot-guard.service >/dev/null 2>&1 || true
elif command -v rc-update >/dev/null 2>&1; then
    for s in nvcurve-gui-autoload nvcurve-gui-boot-guard; do
        rc-service "$s" --ifstarted stop >/dev/null 2>&1 || true
        for rl in /etc/runlevels/*/; do rc-update del "$s" "$(basename "$rl")" >/dev/null 2>&1 || true; done
    done
fi
rm -f /etc/init.d/nvcurve-gui-autoload /etc/init.d/nvcurve-gui-boot-guard \
      "$UNITDIR/nvcurve-gui-autoload.service" "$UNITDIR/nvcurve-gui-boot-guard.service" && ok "Removed boot services (if present)"
# Services installed under the old names (nvcurve-autoload / nvcurve-boot-guard) are removed only when they
# point into this project's LIBEXEC: the same names belong to Centurion, which this must not touch.
for old in nvcurve-autoload nvcurve-boot-guard; do
    for f in "$UNITDIR/$old.service" "/etc/init.d/$old"; do
        if [[ -f $f ]] && grep -qF "$LIBEXEC/" "$f"; then
            [[ -d /run/systemd/system ]] && systemctl disable --now "$old.service" >/dev/null 2>&1 || true
            rm -f "$f" && ok "Removed old-named service $f"
        fi
    done
done

rm -rf "$LIBEXEC" && ok "Removed $LIBEXEC"
rm -f "$PREFIX/bin/nvcurve-gui" && ok "Removed $PREFIX/bin/nvcurve-gui"
# Remove the CLI symlink when it points to this installation.
if [[ -L $PREFIX/bin/nvcurve && $(readlink "$PREFIX/bin/nvcurve") == "$LIBEXEC/nvcurve" ]]; then
    rm -f "$PREFIX/bin/nvcurve" && ok "Removed $PREFIX/bin/nvcurve"
else
    echo "Left $PREFIX/bin/nvcurve alone (not this project's)."
fi
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
# /etc/nvcurve (profiles + config.json) is shared with Centurion, which reads the same files: never
# offered for removal while Centurion is installed.
REPLY=""
if [[ -d /usr/libexec/centurion || -d /usr/local/libexec/centurion || -d /etc/centurion ]]; then
    echo "Centurion is installed: /etc/nvcurve (shared profiles + config) is kept."
else
    read -r -p "Also remove saved V/F profiles, config and guard state (/etc/nvcurve, /var/lib/nvcurve, /var/cache/nvcurve)? [y/N] " REPLY || REPLY=""
fi
if [[ "$REPLY" =~ ^[Yy]$ ]]; then
    rm -rf /etc/nvcurve /var/lib/nvcurve /var/cache/nvcurve
    ok "Removed /etc/nvcurve, /var/lib/nvcurve, /var/cache/nvcurve"
else
    echo "Kept /etc/nvcurve (profiles + config) and the guard state."
fi

ok "Uninstall complete."
