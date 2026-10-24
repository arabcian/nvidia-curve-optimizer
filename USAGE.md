# nvcurve-gui — Usage Guide

> **Before you start:** this tool relies on undocumented NVIDIA NvAPI
> functions and lets you change your GPU's voltage/frequency curve and
> related clocks. That carries real risk (instability, crashes, in rare
> cases hardware damage) and comes with no warranty from NVIDIA, this
> project, or Anthropic. See the disclaimer at the top of `README.md`
> before using it. You are responsible for what you apply.

This guide covers day-to-day use after installation. For architecture and
security, see `README.md` and `SECURITY-CHANGELOG.md`.

## Launching

```bash
nvcurve-gui
```

The first time the window is shown, the app reads the GPU's current V/F curve
and draws it. This may raise a `pkexec` password prompt (depending on the polkit
security level chosen at install time). Only one window can run at a time.

The **Theme** box in the header switches between 21 colour palettes; the new
palette is applied after a restart (the app offers to restart itself).

## The window, top to bottom

### Trial apply

Tick it to make every successful apply a trial: the GPU is put back to stock
after 30 seconds unless you press **Keep**. **Revert now** reverts at once.
Use it for testing a new curve — if the screen glitches or a game hangs, just
wait. The tick is remembered between runs. (A hard freeze or a reboot already
resets the curve by itself: the driver forgets it.)

### GPU Status

Live readout, refreshed while the window is visible: temperature, hotspot, VRAM
temperature, power draw, core clock, memory clock, and the current throttle
reason (`power`, `thermal`, or `—` when the GPU boosts freely). Values come from
NVML; hotspot and per-partition VRAM temperatures on Blackwell need root and are
read by `nvcurve-sensors` (no password for the active local session).

Below it, the profile row:

- **Save As…** — store the current graph, memory offset, VRAM lock and core cap
  as a named profile.
- **Profile dropdown** — selecting a profile loads its values into the window;
  it does **not** apply them. The default profile is marked with ★.
- **Apply Profile** — apply the selected saved profile to the GPU as it is.
- **★ Default** — toggles whether the selected profile is the boot-time
  auto-load profile (used by `nvcurve autoload` / the `nvcurve-autoload` service).
- **Delete** — delete the selected profile (and clear the default if it was one).

### V/F Curve

Voltage (mV) on the X axis, frequency (MHz) on the Y axis.

- Click a point to select it, drag a selected point to move it.
- `Ctrl`+click toggles a point, `Shift`+click selects a range, `Ctrl`/`Shift`+drag
  on empty space box-selects. `Ctrl+A` selects all; then dragging empty space
  shifts the whole curve.
- `←`/`→` extend the selection, `↑`/`↓` nudge by 1 MHz (`Shift`: 15 MHz),
  `Space` toggles the current point, `Esc` clears the selection.
- Mouse wheel zooms at the cursor, `Shift`+wheel pans, `+`/`−` zoom, `0` fits
  (also the **Fit** button; the **Zoom** and pan sliders do the same).
- **Selected / Voltage / Freq / Offset** show the active point; the MHz box next
  to them sets the total offset of the selected point(s).
- **Flatten after index** — every point above that index gets the same frequency
  as that point (press Enter). It is applied with **Apply Offsets** and saved with
  the profile.
- **Select All**, **Reset Graph** (discard unapplied edits and show the last curve
  read from the GPU; it does not touch the hardware).

### Controls

- **Core** — one offset for the whole curve, applied from 30 % of the curve up
  (point 38 of 127, like LACT's curve shift): idle / low-voltage points keep their
  stock clocks. It shows `⚠ curve` once individual points were edited, because a
  single number can no longer describe the curve; **Reset Graph** brings it back.
- **Memory** — VRAM clock offset in MHz, written with **Apply Offsets**.
- **VRAM lock** (min – max) — pins the memory clock now (**Lock**) or hands it
  back to the driver (**Unlock**). Independent of Apply/Reset. Min `0` uses Max for both.
- **Core cap** (+ compensation) — the highest core clock the GPU may use (NVML locked
  clocks). The driver keeps every curve point within about 1000 MHz of stock whatever
  offset is stored, so a flatten cannot pull the top points all the way down; the cap
  does. **Cap** / **Uncap** act immediately; the value is also saved with the profile and
  applied with Apply Offsets. Blackwell GPUs run ~90–115 MHz below a locked clock:
  measure the gap and enter it as the compensation so the GPU lands on your value.
- **PowerMizer** — Auto, Adaptive, Prefer maximum performance, Prefer consistent
  performance. Needs driver 580+. Not kept across reboots.
- **Read Curve** — re-read the curve from the GPU.
- **Apply Offsets** — write what is drawn (plus memory offset, VRAM lock, core cap) to
  the GPU. This is the button that changes hardware behavior.
- **Reset Curve** — curve back to stock (all deltas zero; core cap released).
- **Reset All** — everything back to stock: curve offsets, NVML offsets in every
  P-state, core and VRAM clock locks, power limit, PowerMizer → Auto (asks first).

### Log

Every operation's result — successes, errors, and warnings the core reports (for
example when the driver snaps a memory-clock request to a different value).

## Profiles

A profile is a named snapshot of: per-point curve deltas, memory offset, VRAM lock
range, core cap (and compensation), a pending flatten, and optionally a power limit.
They are JSON files under `/etc/nvcurve/profiles/`; names are letters, digits, space,
`_ - ( )`, up to 64 characters.

**Typical workflow:**

1. Shape the curve (drag points, set Core, Memory, VRAM lock as desired).
2. **Apply Offsets**, with **Trial apply** ticked while testing, and confirm it behaves.
3. **Save As…** with a name (`quiet`, `max-perf`, `undervolt`).
4. Optionally mark it **★ Default** and enable the boot service
   (`systemctl enable nvcurve-autoload.service`, or `rc-update add nvcurve-autoload default`).
   The boot guard pauses the autoload after a boot that died right after applying it;
   resume it with `sudo nvcurve-boot-guard reset` (installed under the libexec directory,
   `/usr/local/libexec/nvcurve-gui/` by default).

## Typical sessions

**A simple core undervolt/overclock:** open the app, set **Core** (e.g. `-100 MHz`),
tick **Trial apply**, **Apply Offsets**, stress-test, press **Keep**, **Save As…**.

**Cap clocks above a voltage point:** select the point where the cap should start, set
**Flatten after index** to its index, Enter, apply, test, save. Use **Core cap** if the
top points cannot be pulled down far enough.

**Fixed VRAM clock:** set **VRAM lock** Max (and optionally Min), press **Lock**;
**Unlock** hands control back to the driver.

## Command line

`nvcurve` is the same core without the GUI (`nvcurve` alone prints the command list):
`nvcurve read`, `nvcurve profile list`, `sudo nvcurve profile apply NAME`,
`sudo nvcurve reset-all`, `nvcurve sensors`, …

## Troubleshooting

- **No curve / "No NVIDIA GPU found"** — the NVIDIA proprietary driver (NVML and NvAPI
  libraries) must be loaded; check the log pane for the underlying error.
- **Password prompt on every action** — security level 2 (or no rules file) asks for the
  administrator password; polkit caches it for a few minutes. Re-run `sudo ./install.sh
  --security-level=1` for a password-free grant to an admin at the machine.
- **GPU Status shows `--`** — NVML (`libnvidia-ml.so.1`) is not available to your user.
- **Hotspot shows `--` on Blackwell** — the sensor helper is not installed or its
  authorization was declined.
- **"already running"** — another window holds the per-user instance lock
  (`$XDG_RUNTIME_DIR/nvcurve-gui-<uid>.lock`); close it first.
- **Core shows `⚠ curve`** — you edited individual points; **Reset Graph** returns to a
  uniform curve.
