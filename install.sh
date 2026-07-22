#!/usr/bin/env bash
#
# nvcurve-gui install.sh
# ════════════════════════════════════════════════════════════════════════
# Standalone installer for the nvcurve GUI — a single-purpose PySide6
# front-end for the `nvcurve` NVIDIA V/F-curve CLI/HAL package. This
# installer does NOT touch anything related to cTGP/nvctgp, gaming
# tunables, or CPU isolation — it only wires up:
#
#   /usr/local/lib/nvcurve-gui/            app code (nvcurve_gui.py, root_helper.py, nvcurve/)
#   /usr/local/lib/nvcurve-gui/root_helper.py   (root:root, 0700)
#   /etc/nvcurve/profiles/                 saved V/F curve profiles
#   /usr/share/polkit-1/actions/           polkit action (com.nvcurve.gui.policy)
#   /usr/share/applications/               .desktop launcher (optional)
#
set -euo pipefail

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="/usr/local/lib/nvcurve-gui"
NVCURVE_PROFILES_DIR="/etc/nvcurve/profiles"
POLKIT_ACTIONS_DIR="/usr/share/polkit-1/actions"
BIN_LINK="/usr/local/bin/nvcurve-gui"

ok()   { echo -e "\033[1;32m[OK]\033[0m $*"; }
warn() { echo -e "\033[1;33m[WARN]\033[0m $*"; }
err()  { echo -e "\033[1;31m[ERR]\033[0m $*" >&2; }

if [[ $EUID -ne 0 ]]; then
    err "This installer must be run as root (sudo ./install.sh)."
    exit 1
fi

# ─── Dependency checks ────────────────────────────────────────────────
MISSING_DEPS=()
command -v pkexec >/dev/null 2>&1 || MISSING_DEPS+=("polkit (pkexec)")
python3 -c "import PySide6" >/dev/null 2>&1 || MISSING_DEPS+=("python3-pyside6 (PySide6 + QtCharts)")
python3 -c "import httpx" >/dev/null 2>&1 || MISSING_DEPS+=("python3-httpx")
python3 -c "import fastapi" >/dev/null 2>&1 || MISSING_DEPS+=("python3-fastapi")
python3 -c "import pydantic" >/dev/null 2>&1 || MISSING_DEPS+=("python3-pydantic")

if [[ ${#MISSING_DEPS[@]} -gt 0 ]]; then
    err "Missing dependencies:"
    for d in "${MISSING_DEPS[@]}"; do echo "   - $d"; done
    echo
    echo "  Install suggestions:"
    echo "  Debian/Ubuntu: sudo apt install policykit-1 python3-pyside6.qtcharts python3-httpx python3-fastapi python3-pydantic"
    echo "  Arch/Manjaro : sudo pacman -S polkit pyside6 python-httpx python-fastapi python-pydantic"
    echo "  Fedora       : sudo dnf install polkit python3-pyside6 python3-httpx python3-fastapi python3-pydantic"
    echo "  Gentoo       : sudo emerge polkit dev-python/pyside6 dev-python/httpx dev-python/fastapi dev-python/pydantic"
    echo
    echo "  Or via pip (any distro): pip install --break-system-packages PySide6 httpx fastapi pydantic"
    exit 1
fi

python3 -c "import PySide6.QtCharts" >/dev/null 2>&1 || {
    warn "PySide6 is present but the QtCharts module could not be imported."
    warn "The V/F curve graph will not render without it (package name is"
    warn "often split out, e.g. python3-pyside6.qtcharts on Debian/Ubuntu)."
}

echo "── Installing nvcurve-gui to $APP_DIR ──────────────────────────"

# ─── App directory + files ────────────────────────────────────────────
install -d -o root -g root -m 0755 "$APP_DIR"
install -d -o root -g root -m 0755 "$APP_DIR/nvcurve"

# nvcurve/ package copied whole (minus __pycache__)
rm -rf "$APP_DIR/nvcurve"
cp -a "$SOURCE_DIR/nvcurve" "$APP_DIR/nvcurve"
find "$APP_DIR/nvcurve" -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
chown -R root:root "$APP_DIR/nvcurve"
find "$APP_DIR/nvcurve" -type d -exec chmod 0755 {} \;
find "$APP_DIR/nvcurve" -type f -exec chmod 0644 {} \;

# GUI: world-readable, not writable by non-root
install -o root -g root -m 0644 "$SOURCE_DIR/nvcurve_gui.py" "$APP_DIR/nvcurve_gui.py"

# root_helper.py: root:root, 0700 — unreadable/unwritable by the invoking user.
# com.nvcurve.gui.policy pins pkexec's org.freedesktop.policykit.exec.path
# to this exact file, so its content can never be swapped by a non-root user.
install -o root -g root -m 0700 "$SOURCE_DIR/root_helper.py" "$APP_DIR/root_helper.py"

ok "Application files installed under $APP_DIR"

# ─── Profiles directory ────────────────────────────────────────────────
install -d -o root -g root -m 0755 "$NVCURVE_PROFILES_DIR"
ok "Profile directory ready: $NVCURVE_PROFILES_DIR"

# ─── Migrate legacy profiles ───────────────────────────────────────────
# Older profiles were saved before ProfileData grew a `gpu_name` field.
# The loader now backfills gpu_name="" so they still apply, but we rewrite
# them here so the on-disk files carry the field explicitly (keeps
# list_profiles and future migrations clean).
if compgen -G "$NVCURVE_PROFILES_DIR/*.json" >/dev/null 2>&1; then
    PYTHONPATH="$APP_DIR" python3 - "$NVCURVE_PROFILES_DIR" <<'PYEOF' && \
        ok "Legacy profiles migrated." || \
        warn "Profile migration skipped (non-fatal; profiles still load at runtime)."
import sys, glob, os
from nvcurve.profiles.native import load_profile, save_profile
prof_dir = sys.argv[1]
migrated = 0
for fp in glob.glob(os.path.join(prof_dir, "*.json")):
    try:
        p = load_profile(fp)            # backfills gpu_name="" if absent
        save_profile(prof_dir, p)       # rewrites file with the full schema
        migrated += 1
    except Exception as e:
        print(f"   - skipped {os.path.basename(fp)}: {e}", file=sys.stderr)
print(f"   migrated {migrated} profile(s)")
PYEOF
fi

# ─── Polkit policy ──────────────────────────────────────────────────────
install -d -o root -g root -m 0755 "$POLKIT_ACTIONS_DIR"
sed "s|/usr/local/lib/nvcurve-gui|$APP_DIR|g" "$SOURCE_DIR/com.nvcurve.gui.policy" \
    > "$POLKIT_ACTIONS_DIR/com.nvcurve.gui.policy"
chown root:root "$POLKIT_ACTIONS_DIR/com.nvcurve.gui.policy"
chmod 0644 "$POLKIT_ACTIONS_DIR/com.nvcurve.gui.policy"
ok "Polkit action installed: $POLKIT_ACTIONS_DIR/com.nvcurve.gui.policy"

# Restart polkit so it picks up the new action immediately.
if command -v rc-service >/dev/null 2>&1 && [[ -d /run/openrc ]]; then
    rc-service polkit restart >/dev/null 2>&1 || warn "Could not restart the OpenRC polkit service (try: rc-service polkit restart)"
elif command -v systemctl >/dev/null 2>&1; then
    systemctl restart polkit >/dev/null 2>&1 || warn "Could not restart the systemd polkit service (try: systemctl restart polkit)"
else
    warn "Could not restart polkit automatically; restart polkitd manually if the app can't authenticate."
fi

# ─── Optional: passwordless polkit rule ────────────────────────────────
read -r -p "Install a passwordless-authorization polkit rule for this action? [y/N] " REPLY
if [[ "$REPLY" =~ ^[Yy]$ ]]; then
    install -d -o root -g root -m 0755 /etc/polkit-1/rules.d
    install -o root -g root -m 0644 "$SOURCE_DIR/49-nvcurve-gui.rules" \
        /etc/polkit-1/rules.d/49-nvcurve-gui.rules
    ok "Passwordless authorization rule installed: /etc/polkit-1/rules.d/49-nvcurve-gui.rules"
    if command -v rc-service >/dev/null 2>&1 && [[ -d /run/openrc ]]; then
        rc-service polkit restart >/dev/null 2>&1 || true
    elif command -v systemctl >/dev/null 2>&1; then
        systemctl restart polkit >/dev/null 2>&1 || true
    fi
else
    echo "Skipped — pkexec will prompt for the admin password on each new"
    echo "polkit auth cache window (typically a few minutes, auth_admin_keep)."
fi

# ─── Launcher ───────────────────────────────────────────────────────────
cat > "$BIN_LINK" << EOF
#!/usr/bin/env bash
exec python3 "$APP_DIR/nvcurve_gui.py" "\$@"
EOF
chmod 0755 "$BIN_LINK"
ok "Launcher installed: $BIN_LINK"

if [[ -f "$SOURCE_DIR/nvcurve-gui.desktop" ]]; then
    install -d -o root -g root -m 0755 /usr/share/applications
    sed "s|/usr/local/bin/nvcurve-gui|$BIN_LINK|g" "$SOURCE_DIR/nvcurve-gui.desktop" \
        > /usr/share/applications/nvcurve-gui.desktop
    chmod 0644 /usr/share/applications/nvcurve-gui.desktop
    ok "Desktop launcher installed."
fi

echo
ok "Installation complete."
echo "  Run with:            nvcurve-gui"
echo "  App directory:       $APP_DIR"
echo "  Profiles directory:  $NVCURVE_PROFILES_DIR"
echo "  Uninstall with:      sudo ./uninstall.sh"
