#!/usr/bin/env bash
#
# nvcurve-gui install.sh
# ════════════════════════════════════════════════════════════════════════
# Standalone build + installer for the NVIDIA V/F-curve optimizer: the Rust
# `nvcurve` core (CLI + root helpers) and the Qt6 GUI, extracted from Centurion.
#
#   sudo ./install.sh                       build + install to /usr/local
#   sudo ./install.sh --security-level=2    polkit level (see below)
#   DESTDIR=/tmp/stage ./install.sh         stage only (packaging), no root needed
#
# Options:
#   --no-build           install what is already built (target/release, gui/build)
#   --no-lto             no link-time optimization for the GUI (Rust always uses fat LTO)
#   --no-harden          drop stack protector / FORTIFY=3 / CET / full RELRO / PIE on the GUI
#   --security-level=N   polkit level for the root helper (NVCURVE_SECURITY_LEVEL works too):
#                          1  wheel/sudo + local + active session is password-free
#                          2  the helper always asks for the administrator password
#                             (polkit caches it briefly). Read-only sensors stay silent.
#                        Asked interactively when not given; DESTDIR and non-tty installs use 1.
#                        An existing 49-nvcurve-gui.rules keeps its level on upgrade.
#
# Layout ($PREFIX defaults to /usr/local):
#   $PREFIX/bin/nvcurve-gui                          GUI (Qt6)
#   $PREFIX/bin/nvcurve                              CLI (Rust)
#   $PREFIX/libexec/nvcurve-gui/nvcurve-root-helper  pkexec target (root:root, 0700)
#   $PREFIX/libexec/nvcurve-gui/nvcurve-sensors      read-only sensor stream (root:root, 0755)
#   $PREFIX/libexec/nvcurve-gui/nvcurve-boot-guard   pauses the boot-time profile after a crashed boot
#   /usr/share/polkit-1/actions/com.nvcurve.gui.policy
#   /etc/polkit-1/rules.d/49-nvcurve-gui.rules
#   /etc/nvcurve/profiles/                           saved V/F curve profiles
#   /etc/init.d/nvcurve-{autoload,boot-guard}        OpenRC   (installed, not enabled)
#   $UNITDIR/nvcurve-{autoload,boot-guard}.service   systemd  (installed, not enabled)
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

PREFIX=${PREFIX:-/usr/local}
DESTDIR=${DESTDIR:-}
LIBEXEC=${LIBEXEC:-$PREFIX/libexec/nvcurve-gui}
UNITDIR=${UNITDIR:-/usr/lib/systemd/system}
POLKIT_ACTIONS_DIR=${POLKIT_ACTIONS_DIR:-/usr/share/polkit-1/actions}
POLKIT_RULES_DIR=${POLKIT_RULES_DIR:-/etc/polkit-1/rules.d}
PROFILES_DIR=/etc/nvcurve/profiles
SECLEVEL=${NVCURVE_SECURITY_LEVEL:-}
BUILD=1 LTO=1 HARDEN=1

ok()   { echo -e "\033[1;32m[OK]\033[0m $*"; }
warn() { echo -e "\033[1;33m[WARN]\033[0m $*"; }
err()  { echo -e "\033[1;31m[ERR]\033[0m $*" >&2; }

for a in "$@"; do
    case "$a" in
        --no-build) BUILD=0 ;;
        --no-lto) LTO=0 ;;
        --no-harden) HARDEN=0 ;;
        --security-level=*) SECLEVEL=${a#*=} ;;
        *) err "unknown option: $a"; exit 2 ;;
    esac
done
if [[ -z "$DESTDIR" && $EUID -ne 0 ]]; then
    err "This installer must be run as root (sudo ./install.sh), or set DESTDIR to stage."
    exit 1
fi

# ─── Earlier install (Python nvcurve-gui) ───────────────────────────────
# Detected by its 0700 root_helper.py. Migrated below: same polkit action id, same profile directory.
LEGACY_APP="$DESTDIR/usr/local/lib/nvcurve-gui"
LEGACY=0; [[ -f $LEGACY_APP/root_helper.py ]] && LEGACY=1

# ─── Security level ─────────────────────────────────────────────────────
# Upgrade: keep the level of the rules file being replaced (no prompt, no silent reset to 1).
OLDRULES="$DESTDIR$POLKIT_RULES_DIR/49-nvcurve-gui.rules"
if [[ -z $SECLEVEL && -f $OLDRULES ]]; then
    SECLEVEL=$(sed -n 's/^var SECURITY_LEVEL = \([12]\);.*/\1/p' "$OLDRULES" | head -n1)
    [[ -n $SECLEVEL ]] && echo "Keeping polkit security level $SECLEVEL from the installed rules."
fi
# Migration: carry the old install's authorization behaviour over. The old package had no level; its
# opt-in passwordless rule (49-nvcurve-gui.rules, no SECURITY_LEVEL) is level 1, no rule at all
# (auth_admin_keep from the old policy) is level 2.
if [[ -z $SECLEVEL ]] && (( LEGACY )); then
    if [[ -f $OLDRULES ]]; then SECLEVEL=1; else SECLEVEL=2; fi
    echo "Earlier install found: keeping its authorization behaviour (polkit security level $SECLEVEL)."
fi
if [[ -z $SECLEVEL ]]; then
    SECLEVEL=1
    if [[ -z $DESTDIR && -t 0 ]]; then
        echo "Polkit security level for the root helper:"
        echo "  1) password-free for an administrator (wheel or sudo) at the machine  [default]"
        echo "  2) the helper always asks for the administrator password (polkit caches it briefly)"
        ans=""; read -r -p "Level [1]: " ans || true
        [[ -n $ans ]] && SECLEVEL=$ans
    fi
fi
[[ $SECLEVEL == [12] ]] || { err "invalid security level '$SECLEVEL' (use 1 or 2)"; exit 2; }

as_user() { if [[ $EUID -eq 0 && -n "${SUDO_USER:-}" ]]; then sudo -u "$SUDO_USER" "$@"; else "$@"; fi; }
onoff() { (( $1 )) && echo ON || echo OFF; }

# ─── Build ──────────────────────────────────────────────────────────────
if (( BUILD )); then
    MISSING=()
    command -v cargo >/dev/null 2>&1 || MISSING+=("cargo (Rust toolchain)")
    command -v cmake >/dev/null 2>&1 || MISSING+=("cmake")
    command -v g++ >/dev/null 2>&1 || command -v clang++ >/dev/null 2>&1 || MISSING+=("a C++20 compiler (g++ or clang++)")
    pkg-config --exists Qt6Widgets Qt6Svg 2>/dev/null || MISSING+=("Qt6 Widgets + Svg development files (>= 6.4)")
    if (( ${#MISSING[@]} )); then
        err "Missing build dependencies:"
        for d in "${MISSING[@]}"; do echo "   - $d"; done
        echo
        echo "  Debian/Ubuntu: sudo apt install cargo cmake g++ qt6-base-dev qt6-svg-dev pkg-config"
        echo "  Arch/Manjaro : sudo pacman -S rust cmake qt6-base qt6-svg pkgconf"
        echo "  Fedora       : sudo dnf install cargo cmake gcc-c++ qt6-qtbase-devel qt6-qtsvg-devel pkgconf"
        echo "  Gentoo       : sudo emerge dev-lang/rust dev-build/cmake dev-qt/qtbase dev-qt/qtsvg"
        exit 1
    fi
    # Rust: fat LTO + codegen-units=1 + panic=abort come from Cargo.toml.
    as_user cargo build --release --locked
    as_user cmake -S gui -B gui/build -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX="$PREFIX" \
        -DNVCURVE_HELPER_DIR="$LIBEXEC" -DNVCURVE_LTO="$(onoff $LTO)" -DNVCURVE_HARDEN="$(onoff $HARDEN)"
    as_user cmake --build gui/build -j"$(nproc)"
fi

T=target/release
for f in nvcurve nvcurve-root-helper nvcurve-sensors nvcurve-boot-guard; do
    [[ -x $T/$f ]] || { err "$T/$f not found — run without --no-build"; exit 1; }
done
[[ -x gui/build/nvcurve-gui ]] || { err "gui/build/nvcurve-gui not found — run without --no-build"; exit 1; }

# ─── Every polkit action id must be known to the rules allowlist ────────
for id in $(grep -o 'action id="com\.nvcurve\.gui\.[^"]*"' com.nvcurve.gui.policy | cut -d'"' -f2); do
    grep -qF "\"$id\"" <(grep -v '^ *\*' 49-nvcurve-gui.rules) \
        || { err "polkit action $id is in com.nvcurve.gui.policy but not in the rules allowlist (49-nvcurve-gui.rules)"; exit 2; }
done

# ─── Migrate the earlier Python install ────────────────────────────────
# Profiles (/etc/nvcurve/profiles) and config.json use the same format and location: they stay where
# they are, backed up once first. Only the old program files are removed; the polkit action, rules,
# launcher and desktop entry are replaced by the steps below (same ids / names).
if (( LEGACY )); then
    echo "── Migrating the earlier Python nvcurve-gui install ───────────────"
    if compgen -G "$DESTDIR$PROFILES_DIR/*.json" >/dev/null 2>&1 || [[ -f $DESTDIR/etc/nvcurve/config.json ]]; then
        BAK="$DESTDIR/etc/nvcurve.pre-migrate-$(date +%Y%m%d-%H%M%S)"
        cp -a "$DESTDIR/etc/nvcurve" "$BAK" && ok "Backed up /etc/nvcurve to ${BAK#"$DESTDIR"}"
    fi
    rm -rf "$LEGACY_APP"
    # The old launcher was a shell script that exec'd python; a PREFIX other than /usr/local would leave it behind.
    if [[ -f $DESTDIR/usr/local/bin/nvcurve-gui ]] && grep -q nvcurve_gui.py "$DESTDIR/usr/local/bin/nvcurve-gui" 2>/dev/null; then
        rm -f "$DESTDIR/usr/local/bin/nvcurve-gui"
    fi
    rm -f "$DESTDIR/usr/share/icons/hicolor/256x256/apps/nvcurve-gui.png"
    [[ $PREFIX != /usr ]] && rm -f "$DESTDIR/usr/share/applications/nvcurve-gui.desktop"
    ok "Removed the old program files (/usr/local/lib/nvcurve-gui, launcher, icon)"
fi

echo "── Installing nvcurve-gui to $PREFIX ──────────────────────────────"
own=(-o root -g root); [[ $EUID -eq 0 ]] || own=()

# ─── Binaries ───────────────────────────────────────────────────────────
install -d "${own[@]}" -m 0755 "$DESTDIR$LIBEXEC" "$DESTDIR$PREFIX/bin"
# nvcurve-root-helper: root:root, 0700 — unreadable/unwritable by the invoking user. The polkit
# action pins pkexec's exec.path to this exact file, so its content can never be swapped by a
# non-root user.
install "${own[@]}" -m 0700 "$T/nvcurve-root-helper" "$DESTDIR$LIBEXEC/"
install "${own[@]}" -m 0755 "$T/nvcurve-sensors" "$T/nvcurve-boot-guard" "$DESTDIR$LIBEXEC/"
install "${own[@]}" -m 0755 "$T/nvcurve" "$DESTDIR$PREFIX/bin/"
DESTDIR="$DESTDIR" cmake --install gui/build --strip >/dev/null
ok "Binaries installed: $PREFIX/bin/nvcurve-gui, $PREFIX/bin/nvcurve, $LIBEXEC"

# ─── Profiles directory ─────────────────────────────────────────────────
install -d "${own[@]}" -m 0755 "$DESTDIR$PROFILES_DIR"
ok "Profile directory ready: $PROFILES_DIR"

# ─── Polkit policy + rules ──────────────────────────────────────────────
install -d "${own[@]}" -m 0755 "$DESTDIR$POLKIT_ACTIONS_DIR" "$DESTDIR$POLKIT_RULES_DIR"
sed "s|@LIBEXEC@|$LIBEXEC|g" com.nvcurve.gui.policy > "$DESTDIR$POLKIT_ACTIONS_DIR/com.nvcurve.gui.policy"
sed -e "s|@LIBEXEC@|$LIBEXEC|g" -e "s|@SECLEVEL@|$SECLEVEL|g" 49-nvcurve-gui.rules > "$DESTDIR$POLKIT_RULES_DIR/49-nvcurve-gui.rules"
chmod 0644 "$DESTDIR$POLKIT_ACTIONS_DIR/com.nvcurve.gui.policy" "$DESTDIR$POLKIT_RULES_DIR/49-nvcurve-gui.rules"
if [[ $EUID -eq 0 ]]; then
    chown root:root "$DESTDIR$POLKIT_ACTIONS_DIR/com.nvcurve.gui.policy" "$DESTDIR$POLKIT_RULES_DIR/49-nvcurve-gui.rules"
fi
ok "Polkit action + rules installed (security level $SECLEVEL)"

# ─── Boot services (installed, never enabled) ───────────────────────────
subst() { sed -e "s|@BINDIR@|$PREFIX/bin|g" -e "s|@LIBEXEC@|$LIBEXEC|g" "$1"; }
install -d "${own[@]}" -m 0755 "$DESTDIR/etc/init.d" "$DESTDIR$UNITDIR"
for s in nvcurve-autoload nvcurve-boot-guard; do
    subst "packaging/openrc/$s" > "$DESTDIR/etc/init.d/$s"
    subst "packaging/systemd/$s.service" > "$DESTDIR$UNITDIR/$s.service"
    chmod 0755 "$DESTDIR/etc/init.d/$s"
    chmod 0644 "$DESTDIR$UNITDIR/$s.service"
    if [[ $EUID -eq 0 ]]; then chown root:root "$DESTDIR/etc/init.d/$s" "$DESTDIR$UNITDIR/$s.service"; fi
done

# ─── Launcher ───────────────────────────────────────────────────────────
install -d "${own[@]}" -m 0755 "$DESTDIR$PREFIX/share/applications"
sed "s|/usr/local/bin/nvcurve-gui|$PREFIX/bin/nvcurve-gui|g" nvcurve-gui.desktop > "$DESTDIR$PREFIX/share/applications/nvcurve-gui.desktop"
chmod 0644 "$DESTDIR$PREFIX/share/applications/nvcurve-gui.desktop"
ok "Desktop launcher installed."

# ─── Activate (real installs only) ──────────────────────────────────────
if [[ -z "$DESTDIR" ]]; then
    # Restart polkit so it picks up the new action immediately.
    if command -v rc-service >/dev/null 2>&1 && [[ -d /run/openrc ]]; then
        rc-service polkit restart >/dev/null 2>&1 || warn "Could not restart the OpenRC polkit service (try: rc-service polkit restart)"
    elif command -v systemctl >/dev/null 2>&1; then
        systemctl restart polkit >/dev/null 2>&1 || warn "Could not restart the systemd polkit service (try: systemctl restart polkit)"
    else
        warn "Could not restart polkit automatically; restart polkitd manually if the app can't authenticate."
    fi
    command -v gtk-update-icon-cache >/dev/null 2>&1 && gtk-update-icon-cache -q "$PREFIX/share/icons/hicolor" 2>/dev/null || true
    [[ -d /run/systemd/system ]] && systemctl daemon-reload 2>/dev/null || true

    if (( LEGACY )); then
        n=$(compgen -G "$PROFILES_DIR/*.json" 2>/dev/null | wc -l)
        if (( n )); then
            "$PREFIX/bin/nvcurve" profile list >/dev/null 2>&1 \
                && ok "Migrated: $n saved profile(s) kept in $PROFILES_DIR and readable by the new core" \
                || warn "$n profile(s) kept in $PROFILES_DIR, but 'nvcurve profile list' failed — check them (backup: /etc/nvcurve.pre-migrate-*)"
        else
            ok "Migrated (no saved profiles found)"
        fi
    fi
    command -v pkexec >/dev/null 2>&1 || warn "pkexec not found — install polkit, every root action needs it."
    ldconfig -p 2>/dev/null | grep -c libnvidia-ml.so >/dev/null || warn "libnvidia-ml.so not found — the NVIDIA proprietary driver is required."
fi

echo
ok "Installation complete."
echo "  Run with:            nvcurve-gui"
echo "  Profiles directory:  $PROFILES_DIR"
echo "  Boot-time profile (optional; the guard pauses it after a boot that crashed right after applying it):"
if [[ -d /run/systemd/system ]]; then
    echo "    systemctl enable nvcurve-autoload.service    # applies the GUI's ★ Default profile at boot"
else
    echo "    rc-update add nvcurve-autoload default       # applies the GUI's ★ Default profile at boot"
fi
echo "  Resume after a trip: sudo $LIBEXEC/nvcurve-boot-guard reset"
echo "  Uninstall with:      sudo ./uninstall.sh"
