# nvcurve-gui

Standalone PySide6 GUI for the `nvcurve` NVIDIA V/F-curve CLI/HAL
package. This is the GPU-tuning tab of `m16R1-power-manager`, extracted
into its own single-purpose application — **no cTGP/nvctgp, no gaming
tunables, no CPU isolation, no power profiles (gmode/quiet/balanced)**.
It does exactly one thing: read, edit, apply, and save NVIDIA GPU
voltage/frequency curves (plus VRAM locked-clock control), through
`nvcurve`.

## What's in here

```
nvcurve-standalone/
├── nvcurve_gui.py           # the entire GUI (single file)
├── root_helper.py           # minimal privileged helper — only nvcurve ops
├── nvcurve/                 # the nvcurve CLI/HAL package, unmodified
├── install.sh / uninstall.sh
├── com.nvcurve.gui.policy   # polkit action
├── 49-nvcurve-gui.rules     # optional passwordless polkit rule
└── nvcurve-gui.desktop      # optional application launcher
```

## Architecture

- `nvcurve_gui.py` draws the V/F curve chart (QtCharts), profile combo,
  memory-offset/VRAM-lock controls, and a log pane. It never talks to
  the GPU directly.
- Every privileged action (`read_gpu_curve`, `apply_gpu_offsets`,
  `reset_gpu_curve`, `set_vram_memlock`, `reset_vram_memlock`,
  `write_nvcurve_profile`, `delete_nvcurve_profile`,
  `set_default_gpu_profile`, `run_gpu_autoload`) is sent as a small JSON
  payload over stdin to `root_helper.py`, invoked through `pkexec`. This
  triggers the `com.nvcurve.gui.set-params` polkit action, whose
  `auth_admin_keep` caches authorization for a few minutes (or is fully
  passwordless if you opt into `49-nvcurve-gui.rules` during install).
- `root_helper.py` is a strict allowlist dispatcher: it accepts exactly
  the ops above, validates every profile name against
  `^[A-Za-z0-9_-]{1,64}$`, resolves every profile path and checks it's
  still inside `/etc/nvcurve/profiles`, and shells out to
  `python3 -m nvcurve ...` with `PYTHONPATH`/`cwd` pinned to its own
  install directory (never anything client-supplied). Result JSON from
  `nvcurve read`/`apply`/`reset` is written atomically to
  `/run/nvcurve-gui/*.json` (root-owned tmpfs dir, O_EXCL|O_NOFOLLOW
  write + rename) so a local user can't race or symlink-hijack it.
- `nvcurve/` is copied over unmodified from the parent project — it's
  already a self-contained CLI/HAL layer over NVAPI/NVML with its own
  safety limits, snapshotting, and profile format.

## Requirements

- PySide6 with the QtCharts addon (`PySide6-Addons`)
- `httpx`, `fastapi`, `pydantic` (nvcurve's own dependencies)
- polkit (`pkexec`)
- NVIDIA proprietary driver + NVML (optional `pynvml` for the live
  temp/power/clock readout — falls back to `nvidia-smi` if absent)

```bash
pip install --break-system-packages PySide6 httpx fastapi pydantic pynvml
```

## Install

```bash
sudo ./install.sh
nvcurve-gui
```

## Uninstall

```bash
sudo ./uninstall.sh
```

## Notes

- Profiles are stored as JSON under `/etc/nvcurve/profiles/`, exactly
  as they were in the parent project — a profile saved by
  m16R1-power-manager's GPU tuning tab will load fine here, and vice
  versa.
- The "cTGP" slider, `run_nvctgp` op, and the C fast-path helper are
  gone entirely — this app has no code path that can touch power
  limits, only the V/F curve, memory offset, and VRAM clock lock.
