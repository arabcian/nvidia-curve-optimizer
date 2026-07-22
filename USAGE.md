# nvcurve-gui — Usage Guide

> **Before you start:** this tool relies on undocumented NVIDIA NvAPI
> functions and lets you change your GPU's voltage/frequency curve and
> power-related clocks. That carries real risk (instability, crashes,
> in rare cases hardware damage) and comes with no warranty from
> NVIDIA, this project, or Anthropic. See the disclaimer at the top of
> `README.md` before using it. You are responsible for what you apply.

This guide covers day-to-day use of the app after it's installed. For
architecture/internals, see `README.md`.

## Launching

```bash
nvcurve-gui
```

On first launch (and every launch after that) the app automatically
reads your GPU's current V/F curve and draws it — you don't need to
press anything to see real values. This may trigger a `pkexec`
password prompt the first time (or every few minutes, depending on
whether you opted into the passwordless polkit rule during install).

## The window, top to bottom

### GPU Status bar

Live readout, refreshed every 2 seconds: temperature, memory
temperature, hotspot temperature, power draw, core clock, memory
clock. Uses NVML (`pynvml`) if available, falls back to `nvidia-smi`
otherwise.

Next to it: **💾 Save As** and a profile dropdown — see
[Profiles](#profiles) below.

### Profile toolbar

- **⭐ Default** — toggles whether the profile currently selected in
  the dropdown is auto-applied at boot (via `nvcurve autoload`). Click
  once to mark it, click again to unmark it. The default profile shows
  a `★` next to its name in the dropdown.
- **🗑 Delete** — deletes the selected profile file (asks for
  confirmation first).

### Point Info & Controls

Shows details for whatever point(s) are currently selected on the
graph:

- **Selected / Voltage / Freq / Total Offset** — read-only info for
  the active point.
- The **MHz spinbox** next to them lets you nudge the *single* active
  point's total offset directly (core offset + per-point offset
  combined).
- **Flatten After Index** — enter a point index and every point after
  it gets flattened to the same frequency as that index. Useful for
  capping the curve's high end without dragging two dozen points by
  hand.

### V/F Curve graph

The main chart: voltage (mV) on the X axis, frequency (MHz) on the Y
axis.

- **Click** a point to select it (info panel updates).
- **Drag** a selected point up/down to change its frequency.
- **Select multiple points**: click one, then `Space` on others to
  add/remove them from the selection, or use **✅ Select All**.
- **Arrow keys**: `←`/`→` move the active selection to the
  previous/next point; `↑`/`↓` nudge the selected point(s) by 1 MHz.
- **Escape** clears the selection.
- If *all* points are selected, dragging anywhere on the chart moves
  the whole curve up/down together (a quick way to apply a uniform
  core offset by hand instead of the spinbox below).
- **↺ Reset Graph** discards any unsaved edits and redraws the curve
  from the last successful read/apply/reset — it does *not* touch the
  hardware, only the on-screen graph.

### Controls (bottom bar)

- **Core Offset** — a single global MHz offset applied to every point
  at once. This is what most people want for a simple "+100 MHz on the
  core" style overclock. Note: if you've edited individual points
  (via dragging or Flatten), this control disables itself and shows
  `⚠ CURVE` — the curve is no longer uniform, so a single global number
  can't represent it. Use **↺ Reset Graph** to get back to a uniform
  curve if you want the simple slider back.
- **Memory Offset** — VRAM clock offset in MHz, applied via NVML on
  Apply.
- **VRAM Lock (Min–Max)** — pins the memory clock to a fixed window
  instead of nudging it with an offset. Leave Min at 0 to just lock to
  Max. Takes effect immediately via the 🔒/🔓 buttons below, independent
  of the curve Apply/Reset buttons.
  - 🔒 locks VRAM to the entered range right now.
  - 🔓 removes the lock, returning clock control to the driver.
- **📥 Read Current Curve** — re-reads the GPU's actual current V/F
  curve and redraws the graph from it (same thing that happens
  automatically on startup).
- **🚀 Apply Offsets** — writes whatever is currently drawn on the
  graph (plus Memory Offset / VRAM Lock, if set) to the GPU. This is
  the button that actually changes hardware behavior.
- **↺ Reset Curve** — resets the GPU's V/F curve to stock (all deltas
  to zero) and reloads the graph from that state. Also clears Memory
  Offset and VRAM Lock in the UI.

### Log

Scrolling log of every operation's result — successes, errors, and
any warnings `nvcurve` itself reports (e.g. the driver silently
snapping a memory-clock request to a different value than requested).

## Profiles

A profile is a named snapshot of: per-point curve deltas, memory
offset, and VRAM lock range. Profiles are stored as JSON files under
`/etc/nvcurve/profiles/`.

**Typical workflow:**

1. Shape the curve on the graph (drag points, set Core Offset, set
   Memory Offset / VRAM Lock as desired).
2. Click **🚀 Apply Offsets** to push it to the GPU and confirm it
   behaves the way you want.
3. Click **💾 Save As**, give it a name (e.g. `quiet`, `max-perf`,
   `undervolt`).
4. It now appears in the profile dropdown. Selecting it in the
   dropdown loads its values into the UI (this does **not**
   automatically apply them to hardware — press **🚀 Apply Offsets**
   afterwards if you want it live).
5. Optionally mark it **⭐ Default** so it's applied automatically the
   next time `nvcurve autoload` runs at boot/login (this project
   doesn't run that itself — wire it into your own startup sequence,
   e.g. a login script or systemd/OpenRC unit, if you want autoload).

Profiles saved by the parent `m16R1-power-manager` project's GPU
tuning tab are fully compatible — same directory, same JSON shape.

## Typical sessions

**"I just want a simple core overclock":**
1. Open the app (curve auto-reads).
2. Set **Core Offset** to e.g. `+100 MHz`.
3. Click **🚀 Apply Offsets**.
4. Stress-test; if stable, **💾 Save As** so you don't have to redo it.

**"I want to cap clocks above a certain voltage point"** (common for
thermal/efficiency tuning):
1. Read the curve.
2. Click the point where you want the cap to start.
3. Set **Flatten After Index** to that point's index.
4. Everything after it now holds flat at that frequency.
5. Apply, test, save.

**"I want a fixed VRAM clock instead of the driver's boost behavior":**
1. Set **VRAM Lock Max** (and optionally Min).
2. Click 🔒 — this takes effect immediately, independent of the curve
   Apply button.
3. Click 🔓 any time to hand control back to the driver.

## Troubleshooting

- **Graph never populates / "nvcurve read failed"** — check the log
  pane for the underlying `nvcurve` error. The most common cause is
  the NVIDIA proprietary driver/NVAPI not being loaded.
- **Password prompt every time instead of caching** — you likely
  skipped the passwordless polkit rule during install; either accept
  the `auth_admin_keep` short cache (a few minutes) or re-run
  `install.sh` and opt into `49-nvcurve-gui.rules` when asked.
- **GPU Status shows `--` for everything** — `pynvml` isn't installed;
  the app falls back to `nvidia-smi`, and if that's also unavailable
  or slow, the fields just stay blank. `pip install --break-system-packages pynvml`
  fixes this.
- **Core Offset spinbox is stuck on `⚠ CURVE`** — you've made
  per-point edits (drag or Flatten), so the curve is no longer
  uniform and can't be represented by a single offset. Click
  **↺ Reset Graph** to discard those edits and get a uniform curve
  back, if that's what you want.
