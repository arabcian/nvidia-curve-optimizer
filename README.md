# nvcurve-gui

<img width="1575" height="885" alt="Screenshot" src="https://github.com/user-attachments/assets/cb3262ca-94d2-448d-99ba-58ebdd0a6255" />

> ## ⚠️ Disclaimer — read before using
>
> This tool works by calling **undocumented, private NVIDIA NvAPI
> functions** (`GetVFPCurve`, `SetClockBoostTable`, and related calls
> reverse-engineered from the closed-source Windows NvAPI headers —
> there is no public, supported API for reading or writing a GPU's
> voltage/frequency curve on Linux). These functions have no official
> specification, are not covered by any NVIDIA support channel, and can
> change behavior, change layout, or disappear entirely in any future
> driver release without notice.
>
> Concretely, this means:
> - There is **no guarantee of correctness**. The offsets, voltage
>   points, and clock domains this tool reads and writes are inferred
>   from reverse-engineering, not from documentation NVIDIA has
>   published.
> - **Overclocking/undervolting always carries risk**: instability,
>   crashes, driver resets, artifacts, data loss from an unexpected
>   crash, and — in rare or sustained-abuse cases — hardware damage or
>   reduced hardware lifespan are all possible outcomes of GPU
>   frequency/voltage/power tuning in general, independent of this
>   specific tool.
> - This project, its authors, and Anthropic (which assisted in
>   preparing this codebase) provide it **as-is, with no warranty of
>   any kind**, and accept **no responsibility or liability** for any
>   damage, data loss, instability, or hardware failure resulting from
>   its use.
> - **You are solely responsible** for what you do with this tool,
>   including deciding whether to use it at all, validating any curve
>   before applying it, and monitoring your hardware (temperatures,
>   stability) while doing so. If you are not comfortable accepting
>   that risk, do not use this software.
>
> Use at your own risk.

Standalone NVIDIA V/F-curve optimizer: the **NVIDIA Curve** tab of
[Centurion](https://github.com/arabcian/centurion), extracted together with its
security model (polkit actions and rules, hardened root helper, boot guard) and
its theme system into a single-purpose application. It does exactly one thing:
read, edit, apply and save NVIDIA GPU voltage/frequency curves (plus the core
clock cap, VRAM offset/lock and PowerMizer), through `nvcurve`.

No Legion-specific code, no firmware/EC access, no CPU tuning, no power
profiles, no tray, no scenes. It runs on any machine with the NVIDIA
proprietary driver.

## What's in here

```
nvidia-curve-optimizer/
├── gui/                       # Qt6 GUI (C++20): curve tab, graph widget, trial bar, theme system
│   ├── src/                   #   main.cpp  nvidiatab.*  vfcurvewidget.*  trialbar.*  nvregdialog.*  theme.*  privileged.*  icon.*
│   └── data/                  #   theme glyphs, app icon (SVG)
├── crates/nvcurve/            # Rust core: NvAPI/NVML (dlopen) HAL, safety limits, profiles, root helpers
│   └── src/bin/               #   nvcurve (CLI)  nvcurve-root-helper  nvcurve-sensors  nvcurve-boot-guard
├── packaging/                 # systemd units + OpenRC scripts (nvcurve-gui-autoload, nvcurve-gui-boot-guard)
├── com.nvcurve.gui.policy     # polkit actions (pinned to the helpers)
├── 49-nvcurve-gui.rules       # polkit rules: allowlist + security level 1/2
├── nvcurve-gui.desktop        # application launcher
├── install.sh / uninstall.sh
├── USAGE.md  SECURITY-CHANGELOG.md  LICENSE  NOTICE
└── Cargo.toml  Cargo.lock
```

## Features synced from Centurion (experimental)

- **Verified GPU identity**: every apply is bound to the GPU (UUID, PCI address, driver, VBIOS) the
  curve was read from; a changed or ambiguous (multi-GPU) topology refuses the write.
- **Verified writes**: apply / reset write a prepared table, read it back exactly and roll back on
  a mismatch; no blind retries after an Xid. Snapshots restore only checked per-point deltas.
- **Flatten guard** and **Xid 154 hardening**: see `docs/NVCURVE-FLATTEN-SAFETY.md` and
  `docs/NVCURVE-XID154-HARDENING.md`.
- **Curve editor**: undo / redo, reference curves (base / read-back), invalid-point markers, a draft
  validator and a transaction history of recent applies. *Record used region* samples the graphics clock and
  marks the estimated points exercised against the last hardware read-back.
- **Driver options**: the NVIDIA tab's *Driver options…* dialog edits a validated set of `nvidia` /
  `nvidia_drm` module options in `/etc/modprobe.d/zz-nvcurve-nvidia.conf` (applies at next boot).
- **GPU Test**: run Vulkan core and VRAM checks, choose the NVIDIA GPU, and review saved results.
- **Sensor safety**: Blackwell hotspot / VRAM register reads skip a sample while a GPU write holds
  the lock, and never re-probe absent memory partitions.

## Alongside Centurion

This app and [Centurion](https://github.com/arabcian/centurion) can be installed on the same machine.

- **Separate:** install prefix (`/usr/local` here, `/usr` for Centurion), helper directory
  (`libexec/nvcurve-gui`), polkit actions (`com.nvcurve.gui.*`), boot services
  (`nvcurve-gui-autoload`, `nvcurve-gui-boot-guard`) and boot-guard state (`/var/lib/nvcurve`).
  The installer never overwrites a Centurion unit or an existing `nvcurve` command; this project's CLI
  is then run as `<libexec>/nvcurve`.
- **Shared on purpose:** `/etc/nvcurve` (profiles and the ★ Default in `config.json`) and the GPU
  write lock `/run/nvcurve.lock`, so the two never write the GPU at the same moment. The uninstaller
  keeps `/etc/nvcurve` while Centurion is installed.
- **Enable only one boot service.** Both would apply the same ★ Default profile at boot; Centurion's
  boot guard does not see this project's and the reverse.

## Architecture

- `nvcurve-gui` draws the V/F curve and the controls. It never writes to the
  GPU itself.
- Every privileged action (`read_gpu_curve`, `apply_gpu_offsets`,
  `apply_named_profile`, `reset_gpu_curve`, `reset_all`, `set_vram_memlock`,
  `reset_vram_memlock`, `set_gpu_clocklock`, `reset_gpu_clocklock`,
  `set_powermizer`, `read_sensors`, `write_nvcurve_profile`,
  `delete_nvcurve_profile`, `set_default_gpu_profile`, `run_gpu_autoload`) is a
  small JSON payload on stdin of `nvcurve-root-helper`, started through
  `pkexec`. The helper is a strict allowlist dispatcher: profile names, profile
  schemas and ranges are validated before anything is written, and the limits
  are re-read live from the driver (see `SECURITY-CHANGELOG.md`).
- The Blackwell hotspot / GDDR7 partition sensors need root; they come from
  `nvcurve-sensors`, a read-only helper with no input (its polkit action is
  `allow_active=yes`).
- `nvcurve` (CLI) is the same core without the GUI: `nvcurve read`, `profile
  apply NAME`, `autoload`, … Run `nvcurve` without arguments for the list.
- Boot-time profile (optional): `nvcurve-gui-autoload` applies the profile marked
  ★ Default. `nvcurve-boot-guard` arms at boot and disarms after 3 minutes of
  stable uptime; a boot that dies in between pauses the autoload until
  `nvcurve-boot-guard reset`, so a too-aggressive curve cannot crash-loop the
  machine.
- Colour themes: 21 palettes, chosen in the header; the choice is stored in
  `~/.config/nvcurve-gui/gui.ini` and applied after a restart of the app.

## Supported GPUs

Any NVIDIA GeForce / RTX GPU the proprietary Linux driver supports, desktop or laptop, from any
board or laptop maker. There is no vendor, model or PCI subsystem check anywhere in the code;
the limits, P-states and offset ranges are read live from the driver for the GPU in the machine.

| Architecture | Examples | Status |
|---|---|---|
| Blackwell | RTX 50 series | Tested (GB202); hotspot / GDDR7 partition sensors via the root sensor helper |
| Ada Lovelace | RTX 40 series | Expected to work (same NvAPI structures) |
| Ampere | RTX 30 series | Expected to work |
| Turing | RTX 20 / GTX 16 series | Expected to work |
| Pascal | GTX 10 series | Expected to work while the driver still supports it (R580 is the last branch for Pascal) |

"Expected to work" means the V/F-curve structures (`GetVFPCurve` / `ClockBoostTable` v1) are the same
ones used since Pascal, but the tool has not been run on that generation. Every write is read back and
rolled back on a mismatch, so an incompatible driver fails the apply instead of writing a wrong curve.

Limits:
- **One NVIDIA GPU per machine for writes.** Hybrid laptops (Intel/AMD iGPU + NVIDIA dGPU) are fine;
  systems with two or more NVIDIA GPUs can read but refuse curve/profile writes, because NvAPI and
  NVML enumeration cannot yet be joined unambiguously.
- The GUI's simulation sandbox (seccomp) is x86-64 only.

## Requirements

- NVIDIA proprietary driver (NVML, `libnvidia-ml.so.1`; NvAPI, `libnvidia-api.so`)
- polkit (`pkexec`)
- NVIDIA Vulkan driver support for the optional GPU core and VRAM tests.
- Build: Rust (cargo with edition 2024 support), CMake ≥ 3.19, a C++20 compiler, Qt6 ≥ 6.4 (Widgets, Svg)

```bash
# Debian/Ubuntu
sudo apt install cargo cmake g++ qt6-base-dev qt6-svg-dev pkg-config policykit-1
# Arch
sudo pacman -S rust cmake qt6-base qt6-svg pkgconf polkit
# Fedora
sudo dnf install cargo cmake gcc-c++ qt6-qtbase-devel qt6-qtsvg-devel pkgconf polkit
```

## Install

```bash
sudo ./install.sh                      # asks for the polkit security level
sudo ./install.sh --security-level=2   # or choose it up front
nvcurve-gui
```

Security levels: **1** — a `wheel`/`sudo` member at the machine (local, active
session) is not asked for a password; everyone else is. **2** — the helper
always asks (polkit caches the password briefly); only the read-only sensors
stay silent. Re-run `install.sh` to change it.

The installer builds as your user (via `SUDO_USER`) and installs as root under
`/usr/local` (override with `PREFIX=`). If it finds the earlier Python version
(`/usr/local/lib/nvcurve-gui`) it migrates it: `/etc/nvcurve` (profiles, config) is backed up to
`/etc/nvcurve.pre-migrate-<date>` and then used as it is, the old program files, launcher and icon are
removed, and the old authorization behaviour is kept (opt-in passwordless rule → security level 1,
none → level 2) unless you pass `--security-level`.
Boot services are installed but not enabled — see the end of the installer output.

## Uninstall

```bash
sudo ./uninstall.sh
```

## Notes

- Profiles are JSON files under `/etc/nvcurve/profiles/`, the same format as
  before — profiles saved by the earlier Python version, or by Centurion, load
  here and vice versa.
- Config (`/etc/nvcurve/config.json`) is read only if root owns it and nobody
  else can write it. `max_delta_khz` (default +500 MHz per point, never above
  the driver's +1000 MHz) is the overclock cap the helper enforces.
- No code path here touches the power limit except `reset_all` (which restores
  it to stock) and a profile that carries `power_limit_w`.

## License

GPL-3.0-or-later (inherited from Centurion) — see `LICENSE` and `NOTICE`.
