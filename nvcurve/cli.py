"""nvcurve CLI

Normal use:
    nvcurve                                        Launch web UI (escalates to root if needed)
    nvcurve read [--full|--json]                   Read V/F curve (escalates to root)
    nvcurve write [--point N|--range A-B|--global|--reset] --delta D [--dry-run]
    nvcurve verify --point N --delta D             Write-verify cycle (requires root)
    nvcurve snapshot [save|restore|list]           Manage snapshots
    nvcurve gpus                                   List detected NVIDIA GPUs
    nvcurve profile [save|apply|list|default]      Manage profiles
    nvcurve memlock status                         Show supported memory clock range
    nvcurve memlock set --to-max                   Lock VRAM to its max supported frequency
    nvcurve memlock set --max N [--min N]          Lock VRAM to a fixed MHz window
    nvcurve memlock reset                          Unlock VRAM (return to driver/P-state control)

Web server (on-demand, for the GUI):
    nvcurve serve start [--detach]                 Start web server (escalates to root)
    nvcurve serve stop                             Stop running web server
    nvcurve serve status                           Check web server status

Daemon (systemd service for auto-load profiles):
    nvcurve daemon                                 Run the daemon (requires root)
    nvcurve autoload                               Apply auto-load profiles from config (requires root)
    nvcurve service install [--serve]              Register systemd daemon (escalates to root)
    nvcurve service configure                      Update config + restart daemon (escalates to root)
    nvcurve service uninstall                      Remove systemd service (escalates to root)
    nvcurve service start/stop/restart/status      Manage systemd service

First-time / diagnostic commands (bypass server, escalate to root):
    nvcurve setup                                  Hardware compatibility check (diag + verify + restore)
    nvcurve read --diag                            Probe all NvAPI functions
    nvcurve read --raw                             Raw hex dumps of hardware buffers
    nvcurve inspect [--point N|--range A-B]        Raw ClockBoostTable field detail
"""

import argparse
import json
import struct
import sys
import time
import os

from .config import Config, default_config
from .client import NvCurveClient, ServerNotRunning, ApiError
from .nvapi.constants import (
    VFP_SIZE, VFP_BASE, VFP_STRIDE,
    CT_SIZE, CT_BASE, CT_STRIDE,
    CT_POINTS,
)
from .nvapi.errors import NvApiUnavailableError, NoGpuFoundError, GpuIndexError


# ── Utilities ─────────────────────────────────────────────────────────────────

def hexdump(data: bytes, start: int, length: int, cols: int = 16) -> str:
    lines = []
    end = min(start + length, len(data))
    for off in range(start, end, cols):
        chunk = data[off:off + cols]
        hx = " ".join(f"{b:02x}" for b in chunk)
        asc = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        lines.append(f"  {off:04x}: {hx:<{cols * 3}}  {asc}")
    return "\n".join(lines)


def parse_range(s: str):
    """Parse 'A-B' into (A, B) tuple."""
    parts = s.split("-")
    if len(parts) != 2:
        raise argparse.ArgumentTypeError(f"Expected A-B format, got '{s}'")
    try:
        a, b = int(parts[0]), int(parts[1])
    except ValueError:
        raise argparse.ArgumentTypeError(f"Non-integer in range: '{s}'")
    if a > b:
        raise argparse.ArgumentTypeError(f"Start > end in range: {a}-{b}")
    if a < 0 or b >= CT_POINTS:
        raise argparse.ArgumentTypeError(f"Range {a}-{b} outside 0–{CT_POINTS - 1}")
    return (a, b)


# ── Output formatters ─────────────────────────────────────────────────────────

def _select_display_indices(points, domains, full):
    """Return the list of point indices to display for the condensed or full view."""
    if full:
        return list(range(len(points)))
    show = []
    prev_freq = -1
    for i, (f, v) in enumerate(points):
        if f == 0 and v == 0:
            continue
        if domains and i < len(domains) and domains[i] == "memory":
            show.append(i)
        elif f != prev_freq or i == len(points) - 1:
            show.append(i)
        prev_freq = f
    return show


def _print_curve_table(points, offsets, domains, show, current_idx):
    """Print the per-point V/F table for the given display indices."""
    if domains:
        print(f"{'#':>3s}  {'Freq':>8s}  {'Voltage':>8s}  {'Offset':>8s}  {'Domain'}")
        print("-" * 56)
    else:
        print(f"{'#':>3s}  {'Freq':>8s}  {'Voltage':>8s}  {'Offset':>8s}")
        print("-" * 42)

    for i in show:
        f, v = points[i]
        if f == 0 and v == 0:
            continue
        freq_s = f"{f / 1000:.0f} MHz"
        volt_s = f"{v / 1000:.0f} mV"
        offset_s = ""
        if offsets and offsets[i] != 0:
            offset_s = f"{offsets[i] / 1000:+.0f} MHz"
        domain = domains[i] if domains and i < len(domains) else ""
        marker = ""
        if current_idx is not None and i == current_idx:
            marker = "  <-- current"
        elif f < 1_000_000 and v > 0 and not domain:
            marker = "  (low-power)"
        if domains:
            print(f"{i:3d}  {freq_s:>8s}  {volt_s:>8s}  {offset_s:>8s}  {domain}{marker}")
        else:
            print(f"{i:3d}  {freq_s:>8s}  {volt_s:>8s}  {offset_s:>8s}{marker}")


def _print_curve_summary(points, offsets, domains):
    """Print the frequency/voltage/offset summary lines after the table."""
    print()
    if domains:
        gpu_idxs = [i for i, d in enumerate(domains) if d == "gpu"]
        mem_idxs = [i for i, d in enumerate(domains) if d == "memory"]

        gpu_active = [(points[i][0], points[i][1]) for i in gpu_idxs
                      if i < len(points) and points[i][0] > 0]
        if gpu_active:
            freqs = [f for f, v in gpu_active]
            volts = [v for f, v in gpu_active]
            print(f"GPU core: {min(freqs)/1000:.0f} – {max(freqs)/1000:.0f} MHz, "
                  f"{min(volts)/1000:.0f} – {max(volts)/1000:.0f} mV "
                  f"({len(gpu_active)} points)")

        mem_active = [(points[i][0], points[i][1]) for i in mem_idxs
                      if i < len(points) and points[i][0] > 0]
        if mem_active:
            freqs = [f for f, v in mem_active]
            volts = [v for f, v in mem_active]
            print(f"Memory:   {min(freqs)/1000:.0f} – {max(freqs)/1000:.0f} MHz, "
                  f"{min(volts)/1000:.0f} – {max(volts)/1000:.0f} mV "
                  f"({len(mem_active)} points)")

        if offsets:
            gpu_offsets = [offsets[i] for i in gpu_idxs
                           if i < len(offsets) and offsets[i] != 0]
            if gpu_offsets:
                vals = set(gpu_offsets)
                if len(vals) == 1:
                    print(f"GPU offset: {next(iter(vals))/1000:+.0f} MHz "
                          f"(uniform across {len(gpu_offsets)} points)")
                else:
                    print(f"GPU offsets: {len(gpu_offsets)} points active "
                          f"(range: {min(vals)/1000:+.0f} to {max(vals)/1000:+.0f} MHz)")
    else:
        active = [(f, v) for f, v in points if f > 0 and v > 0]
        if active:
            freqs = [f for f, v in active]
            volts = [v for f, v in active]
            print(f"Frequency range: {min(freqs)/1000:.0f} – {max(freqs)/1000:.0f} MHz")
            print(f"Voltage range:   {min(volts)/1000:.0f} – {max(volts)/1000:.0f} mV")
            print(f"V/F points: {len(active)}")
            if offsets:
                nonzero = sum(1 for o in offsets if o != 0)
                if nonzero > 0:
                    vals = set(o for o in offsets if o != 0)
                    if len(vals) == 1:
                        print(f"Global offset: {next(iter(vals))/1000:+.0f} MHz "
                              f"(applied to {nonzero} points)")
                    else:
                        print(f"Per-point offsets active on {nonzero} points "
                              f"(range: {min(vals)/1000:+.0f} to {max(vals)/1000:+.0f} MHz)")


def print_curve(points, offsets, voltage, domains=None, full=False):
    """Print formatted V/F curve table."""
    if voltage:
        print(f"Current voltage: {voltage / 1000:.1f} mV")

    if domains:
        gpu_count = sum(1 for d in domains if d == "gpu")
        mem_count = sum(1 for d in domains if d == "memory")
        parts = [f"{gpu_count} GPU core points"]
        if mem_count:
            parts.append(f"{mem_count} memory points")
        parts.append(f"{gpu_count + mem_count} total")
        print(f"Curve: {', '.join(parts)}")
    print()

    current_idx = None
    if voltage:
        for i, (f, v) in enumerate(points):
            if v > 0 and abs(v - voltage) < 10000:
                current_idx = i
                break

    show = _select_display_indices(points, domains, full)
    _print_curve_table(points, offsets, domains, show, current_idx)
    _print_curve_summary(points, offsets, domains)


def output_json(gpu_name, points, offsets, voltage, domains=None):
    """Output JSON format."""
    gpu_points = [i for i, d in enumerate(domains) if d == "gpu"] if domains else []
    mem_points = [i for i, d in enumerate(domains) if d == "memory"] if domains else []
    data = {
        "gpu": gpu_name,
        "current_voltage_uV": voltage,
        "layout": {
            "vfp_curve": {"size": VFP_SIZE, "base": VFP_BASE,
                          "stride": VFP_STRIDE, "max_entries": len(points)},
            "clock_table": {"size": CT_SIZE, "base": CT_BASE,
                            "stride": CT_STRIDE, "delta_offset": 0x14,
                            "max_entries": CT_POINTS},
        },
        "curve_info": {
            "gpu_points": gpu_points,
            "mem_points": mem_points,
            "total_points": len(points),
        },
        "vf_curve": [],
    }
    if points:
        for i, (f, v) in enumerate(points):
            if f > 0 or v > 0:
                entry = {"index": i, "freq_kHz": f, "volt_uV": v}
                if offsets:
                    entry["freq_offset_kHz"] = offsets[i]
                if domains and i < len(domains):
                    entry["domain"] = domains[i]
                data["vf_curve"].append(entry)
    print(json.dumps(data, indent=2))


# ── Diagnostics (direct HAL, root required) ───────────────────────────────────

def run_diagnostics(gpu, gpu_name, gpu_index: int = 0):
    """Probe all known NvAPI functions and report results."""
    from .hal.vfcurve import get_boost_mask
    from .hal.monitoring import init_nvml, get_driver_version, get_vram_total
    from .hal.limits import get_power_limit, get_clock_offsets, get_mem_offset_range
    from .nvapi.bootstrap import nvcall, query_interface
    from .nvapi.constants import FUNC, MASK_SIZE, VOLT_SIZE, RANGES_SIZE, PERF_SIZE, VBOOST_SIZE

    init_nvml()  # best-effort; diagnostics degrade gracefully without it

    # ── System info ───────────────────────────────────────────────────────────
    print("=== System ===")
    print()
    driver = get_driver_version()
    vram = get_vram_total(gpu_index)
    print(f"  GPU:     {gpu_name}")
    print(f"  Driver:  {driver or '(unavailable — NVML not initialised)'}")
    if vram is not None:
        print(f"  VRAM:    {vram / (1024 ** 3):.1f} GiB  ({vram:,} bytes)")
    else:
        print("  VRAM:    (unavailable)")
    print()

    # ── Function probe ────────────────────────────────────────────────────────
    print("=== Function probe ===")
    print()

    probes = [
        ("GetVFPCurve",         FUNC["GetVFPCurve"],         VFP_SIZE,    1, True),
        ("GetClockBoostMask",   FUNC["GetClockBoostMask"],   MASK_SIZE,   1, True),
        ("GetClockBoostTable",  FUNC["GetClockBoostTable"],  CT_SIZE,     1, True),
        ("GetCurrentVoltage",   FUNC["GetCurrentVoltage"],   VOLT_SIZE,   1, False),
        ("GetClockBoostRanges", FUNC["GetClockBoostRanges"], RANGES_SIZE, 1, False),
        ("GetPerfLimits",       FUNC["GetPerfLimits"],       PERF_SIZE,   2, False),
        ("GetVoltBoostPercent", FUNC["GetVoltBoostPercent"], VBOOST_SIZE, 1, False),
        ("SetClockBoostTable",  FUNC["SetClockBoostTable"],  CT_SIZE,     1, True),
    ]

    for name, fid, size, ver, needs_mask in probes:
        ptr = query_interface(fid)
        resolved = "resolved" if ptr else "NOT FOUND"
        print(f"  {name:30s}  0x{fid:08X}  size=0x{size:04X}  ver={ver}  {resolved}")

    # ── Read function tests ───────────────────────────────────────────────────
    mask_bytes = None
    print()
    print("=== Read function tests ===")

    mask_bytes, mask_err = get_boost_mask(gpu)
    if not mask_bytes:
        print(f"  WARNING: Failed to get boost mask: {mask_err}")

    for name, fid, size, ver, needs_mask in probes:
        if name.startswith("Set"):
            continue

        def fill(buf, _nm=needs_mask, _fid=fid, _mask=mask_bytes):
            if _nm and _mask:
                for i in range(32):
                    buf[4 + i] = _mask[i]

        d, err = nvcall(fid, gpu, size, ver=ver, pre_fill=fill)
        status = f"OK ({len(d)} bytes)" if d else f"FAILED: {err}"
        print(f"  {name:30s}  {status}")
        if d:
            vw = struct.unpack_from("<I", d, 0)[0]
            print(f"    version_word = 0x{vw:08X}")

    # ── Boost mask ────────────────────────────────────────────────────────────
    print()
    print("=== Boost mask ===")
    print()
    if mask_bytes:
        active = [i for i in range(len(mask_bytes) * 8) if mask_bytes[i // 8] & (1 << (i % 8))]
        print(f"  Raw (hex):     {mask_bytes.hex()}")
        if active:
            print(f"  Active points: {len(active)} of {len(mask_bytes) * 8}"
                  f"  (indices {active[0]}–{active[-1]})")
        else:
            print(f"  Active points: 0 of {len(mask_bytes) * 8}")
    else:
        print(f"  WARNING: Could not read boost mask: {mask_err}")

    # ── Clock offsets & ranges ────────────────────────────────────────────────
    print()
    print("=== Clock offsets & ranges ===")
    print()
    offsets = get_clock_offsets(gpu_index)
    mem_range = get_mem_offset_range(gpu_index)
    gpc_cur = offsets.get("gpc_offset_mhz")
    mem_cur = offsets.get("mem_offset_mhz")
    mem_min = mem_range.get("min_mem_offset_mhz")
    mem_max = mem_range.get("max_mem_offset_mhz")
    gpc_cur_s = f"{gpc_cur:+d} MHz" if gpc_cur is not None else "unavailable"
    mem_cur_s = f"{mem_cur:+d} MHz" if mem_cur is not None else "unavailable"
    mem_range_s = (f"{mem_min:+d} / {mem_max:+d} MHz"
                   if mem_min is not None and mem_max is not None else "unavailable")
    print(f"  GPC (core) offset:  {gpc_cur_s}  (range: ±1000 MHz safety cap)")
    print(f"  Memory offset:      {mem_cur_s}  (range: {mem_range_s})")

    # ── Power limits ──────────────────────────────────────────────────────────
    print()
    print("=== Power limits ===")
    print()
    pwr = get_power_limit(gpu_index)
    cur_w  = pwr.get("power_limit_w")
    def_w  = pwr.get("default_power_limit_w")
    min_w  = pwr.get("min_power_limit_w")
    max_w  = pwr.get("max_power_limit_w")
    def fmt_w(v): return f"{v} W" if v is not None else "unavailable"
    print(f"  Current:  {fmt_w(cur_w)}")
    print(f"  Default:  {fmt_w(def_w)}")
    if min_w is not None and max_w is not None:
        print(f"  Range:    {min_w} – {max_w} W")


# ── Privilege / browser helpers ───────────────────────────────────────────────

def _open_browser_as_user(url: str) -> None:
    """Open URL in browser, switching back to the original user if running under sudo."""
    import subprocess
    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user and os.geteuid() == 0:
        try:
            subprocess.Popen(
                ["runuser", "-u", sudo_user, "--", "xdg-open", url],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return
        except Exception:
            pass
    import webbrowser
    webbrowser.open(url)



def require_root():
    """Ensure the process is running as root, re-invoking via sudo if necessary."""
    if os.geteuid() != 0:
        # Forward display/session vars so the server can open the browser as the
        # original user (Wayland sockets are user-owned; Firefox refuses to run as root).
        passthrough = [
            f"{k}={v}"
            for k in ("DISPLAY", "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR",
                      "DBUS_SESSION_BUS_ADDRESS", "XAUTHORITY")
            if (v := os.environ.get(k))
        ]
        try:
            # PYTHONDONTWRITEBYTECODE prevents root-owned __pycache__ in site-packages.
            os.execvp("sudo", [
                "sudo", "env", "PYTHONDONTWRITEBYTECODE=1", *passthrough,
                sys.executable, "-m", "nvcurve", *sys.argv[1:]
            ])
        except Exception as e:
            print(f"nvcurve: sudo failed: {e}", file=sys.stderr)
            sys.exit(1)


_SERVER_INFO_FILE = "/run/nvcurve.json"       # runtime: written by server, deleted on exit
_PERSISTENT_CONFIG_FILE = "/etc/nvcurve/config.json"  # persistent: written by service install
_DAEMON_SOCKET_PATH = "/run/nvcurve-daemon.sock"

_ALLOWED_HOSTS = {"127.0.0.1", "::1", "localhost"}


def _daemon_send(cmd: dict) -> dict | None:
    """Send a JSON command to the daemon and return its response.

    Returns None if the daemon socket is not available (daemon not running).
    """
    import socket as _socket
    try:
        with _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM) as sock:
            sock.settimeout(5.0)
            sock.connect(_DAEMON_SOCKET_PATH)
            sock.sendall(json.dumps(cmd).encode() + b"\n")
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = sock.recv(4096)
                if not chunk:
                    break
                buf += chunk
            if not buf:
                return None
            return json.loads(buf)
    except (FileNotFoundError, ConnectionRefusedError, OSError, json.JSONDecodeError):
        return None


def _safe_host(host: str, cfg: Config) -> str:
    """Return host if it is a loopback address, otherwise fall back to cfg.host.

    0.0.0.0 (bind-all) is silently remapped to 127.0.0.1 — it's a valid local
    server address, just not usable as a client connection target.
    """
    if host in ("0.0.0.0", "::"):
        return "127.0.0.1"
    if host not in _ALLOWED_HOSTS:
        print(f"nvcurve: ignoring untrusted host '{host}' in server info; "
              f"using {cfg.host}", file=sys.stderr)
        return cfg.host
    return host


def _log_file() -> str:
    return "/var/log/nvcurve.log" if os.geteuid() == 0 else "/tmp/nvcurve.log"


def _read_server_info() -> dict | None:
    """Read the server's runtime info (host, port, pid) from its info file.

    The file is written by the server process on startup and deleted on exit.
    Returns None if the file is absent, stale, or unreadable.
    """
    try:
        with open(_SERVER_INFO_FILE) as f:
            info = json.load(f)
    except (FileNotFoundError, PermissionError, json.JSONDecodeError, OSError):
        return None
    try:
        os.kill(info["pid"], 0)
    except PermissionError:
        # Process exists but is owned by another user (e.g. root's server
        # process when we're not root) — it's still alive.
        pass
    except (KeyError, ProcessLookupError, OSError, TypeError):
        return None
    return info


def _discover_server_url(cfg: Config) -> str:
    """Return the server's base URL using a three-level priority chain:

    1. /run/nvcurve.json      — runtime info written by the running server process
    2. /etc/nvcurve/config.json — persistent config written by `service install`
    3. Config defaults          — 127.0.0.1:8042
    """
    # 1. Runtime info (most accurate — reflects the actual running port)
    info = _read_server_info()
    if info:
        host = _safe_host(info["host"], cfg)
        return f"http://{host}:{info['port']}"

    # 2. Persistent config (survives reboots; written by `service install`)
    data = _persistent_cfg_load()
    if data:
        host = _safe_host(data.get("host", cfg.host), cfg)
        port = data.get("port", cfg.port)
        return f"http://{host}:{port}"

    # 3. Hardcoded defaults
    return f"http://{cfg.host}:{cfg.port}"


# ── Subcommand handlers ───────────────────────────────────────────────────────

def _show_curve(gpu_name, points, offsets, voltage, args, domains=None) -> None:
    """Format and print curve data — shared by HTTP and direct-HAL paths."""
    if args.json:
        output_json(gpu_name, points, offsets, voltage, domains=domains)
        return
    print(f"GPU: {gpu_name}")
    print_curve(points, offsets, voltage, domains=domains, full=args.full)


def cmd_read(args):
    # ── Direct HAL paths (need root) ──────────────────────────────────────────
    if args.diag:
        require_root()
        from .hal.gpu import get_gpu
        gpu, gpu_name = get_gpu(index=getattr(args, "gpu_index", 0))
        run_diagnostics(gpu, gpu_name, gpu_index=getattr(args, "gpu_index", 0))
        return

    if args.raw:
        require_root()
        from .hal.gpu import get_gpu
        from .hal.vfcurve import read_clock_table_raw, get_boost_mask
        from .hal.monitoring import read_voltage
        from .nvapi.bootstrap import nvcall
        from .nvapi.constants import FUNC
        gpu, gpu_name = get_gpu(index=getattr(args, "gpu_index", 0))

        print(f"GPU: {gpu_name}")

        mask_bytes, _ = get_boost_mask(gpu)
        def fill_vfp(buf):
            if mask_bytes:
                for i in range(32):
                    buf[4 + i] = mask_bytes[i]

        vfp_raw, _ = nvcall(FUNC["GetVFPCurve"], gpu, VFP_SIZE, ver=1, pre_fill=fill_vfp)
        ct_raw, _ = read_clock_table_raw(gpu)

        if vfp_raw:
            print()
            print("=== VFP Curve (0x21537AD4) — header + first entries ===")
            print(hexdump(vfp_raw, 0x00, 0x48))
            print("  --- data at 0x48, stride 0x1C ---")
            print(hexdump(vfp_raw, 0x48, VFP_STRIDE * 5))

        if ct_raw:
            print()
            print("=== ClockBoostTable (0x23F1B133) — header + first entries ===")
            print(hexdump(ct_raw, 0x00, 0x44))
            print("  --- data at 0x44, stride 0x24, freqDelta at +0x14 ---")
            print(hexdump(ct_raw, 0x44, CT_STRIDE * 5))
        print()

        from .hal.vfcurve import read_curve
        curve_state, _ = read_curve(gpu, gpu_name)
        voltage, _ = read_voltage(gpu)
        if curve_state:
            points = [(p.freq_khz, p.volt_uv) for p in curve_state.points]
            offsets = [p.delta_khz for p in curve_state.points]
            domains = [p.domain for p in curve_state.points]
            _show_curve(gpu_name, points, offsets, voltage, args, domains=domains)
        return

    # ── Normal path — direct HAL (requires root) ──────────────────────────────
    require_root()
    from .hal.gpu import get_gpu
    from .hal.vfcurve import read_curve
    from .hal.monitoring import read_voltage as _read_voltage
    gpu, gpu_name = get_gpu(index=getattr(args, "gpu_index", 0))
    curve_state, curve_err = read_curve(gpu, gpu_name)
    if not curve_state:
        print(f"Failed to read V/F curve: {curve_err}", file=sys.stderr)
        sys.exit(1)
    voltage, _ = _read_voltage(gpu)
    points = [(p.freq_khz, p.volt_uv) for p in curve_state.points]
    offsets = [p.delta_khz for p in curve_state.points]
    domains = [p.domain for p in curve_state.points]
    _show_curve(gpu_name, points, offsets, voltage, args, domains=domains)


def cmd_inspect(args):
    """Show detailed raw ClockBoostTable fields. Requires root (direct HAL)."""
    require_root()
    from .hal.gpu import get_gpu
    from .hal.vfcurve import read_clock_table_raw, read_clock_entry_full, read_curve

    gpu, gpu_name = get_gpu(index=getattr(args, "gpu_index", 0))
    raw, err = read_clock_table_raw(gpu)
    if not raw:
        print(f"Failed to read ClockBoostTable: {err}", file=sys.stderr)
        sys.exit(1)

    curve_state, _ = read_curve(gpu, gpu_name)
    points_data = {}
    gpu_indices = set()
    mem_indices = set()
    if curve_state:
        for p in curve_state.points:
            points_data[p.index] = (p.freq_khz, p.volt_uv)
            if p.domain == "memory":
                mem_indices.add(p.index)
            else:
                gpu_indices.add(p.index)

    if args.point is not None:
        indices = [args.point]
    elif args.range:
        indices = list(range(args.range[0], args.range[1] + 1))
    else:
        defaults = [0, 1, 50, 51, 80, 126]
        if mem_indices:
            mp = min(mem_indices)
            defaults.extend([mp - 1, mp, mp + 1, max(mem_indices)])
        else:
            defaults.append(127)
        indices = sorted(set(defaults))

    print(f"GPU: {gpu_name}")
    if curve_state:
        parts = [f"{len(gpu_indices)} GPU core points"]
        if mem_indices:
            parts.append(f"{len(mem_indices)} memory points")
        parts.append(f"{len(gpu_indices) + len(mem_indices)} total")
        print(f"Curve: {', '.join(parts)}")
    print(f"ClockBoostTable entry detail (stride=0x{CT_STRIDE:02X}, "
          f"9 fields × 4 bytes)")
    print()

    for p in indices:
        if p < 0 or p >= CT_POINTS:
            continue
        entry = read_clock_entry_full(raw, p)
        off = CT_BASE + p * CT_STRIDE

        domain_label = ""
        if p in mem_indices:
            domain_label = " [MEMORY]"
        elif p in gpu_indices:
            domain_label = " [GPU]"

        freq_str = ""
        if p in points_data:
            f, v = points_data[p]
            freq_str = f"  (VFP: {f/1000:.0f} MHz @ {v/1000:.0f} mV)"

        print(f"Point {p:3d} — buffer offset 0x{off:04X}{domain_label}{freq_str}")
        for key, val in entry.items():
            if key == "freqDelta_kHz":
                continue
            if "0x14" in key:
                print(f"  {key}: {val:12d}  (0x{val & 0xFFFFFFFF:08X})"
                      f"  = {val/1000:+.0f} MHz  ← freqDelta")
            else:
                print(f"  {key}: {val:12d}  (0x{val:08X})")
        print()


def cmd_write(args):
    if not args.reset and args.delta is None:
        print("Error: --delta is required (use --reset to zero all offsets)", file=sys.stderr)
        sys.exit(2)

    delta_khz = int((args.delta or 0.0) * 1000)
    max_delta_khz = int(args.max_delta * 1000) if args.max_delta is not None else None
    point_deltas = {}

    if args.reset:
        if args.dry_run:
            print("DRY RUN — would reset all offsets to 0.")
            return
        require_root()
        from .hal.gpu import get_gpu
        from .hal.vfcurve import reset_offsets
        gpu, _ = get_gpu(index=getattr(args, "gpu_index", 0))
        reset_offsets(gpu)
        print("Reset: all offsets set to 0.")
        return

    elif args.point is not None:
        point_deltas[args.point] = delta_khz
        target_msg = (f"Target: point {args.point}, delta {args.delta:+.0f} MHz "
                      f"({delta_khz:+d} kHz)")

    elif args.range:
        start, end = args.range
        for i in range(start, end + 1):
            point_deltas[i] = delta_khz
        target_msg = (f"Target: points {start}–{end} ({len(point_deltas)} points), "
                      f"delta {args.delta:+.0f} MHz")

    elif args.glob:
        target_msg = (f"Target: all active points (global), "
                      f"delta {args.delta:+.0f} MHz")

    else:
        print("Error: specify --point N, --range A-B, --global, or --reset", file=sys.stderr)
        sys.exit(2)

    if args.dry_run:
        print(target_msg)
        if args.glob:
            print()
            print("DRY RUN — would send:")
            print(f"  Target: Global active points")
            print(f"  Delta:  {delta_khz:+d} kHz ({args.delta:+.0f} MHz)")
        else:
            keys = sorted(point_deltas.keys())
            preview = keys[:5]
            tail = f"...and {len(keys) - 5} more" if len(keys) > 5 else ""
            print()
            print("DRY RUN — would send:")
            print(f"  Points: {preview}{(' ' + tail) if tail else ''}")
            print(f"  Delta:  {delta_khz:+d} kHz ({args.delta:+.0f} MHz)")
        if max_delta_khz is not None:
            print(f"  Max delta override: {args.max_delta:+.0f} MHz")
        return

    require_root()
    print(target_msg)
    from .hal.gpu import get_gpu
    from .hal.vfcurve import write_offsets, read_curve
    from .safety import validate_write, check_negative_freq_warnings

    gpu_index = getattr(args, "gpu_index", 0)
    gpu, gpu_name = get_gpu(index=gpu_index)

    # Read curve once, before the write, so we have the true pre-write state
    # for both the --global point expansion and the negative-frequency check.
    curve_state, curve_err = read_curve(gpu, gpu_name)
    if args.glob:
        # Build per-point deltas for all active GPU-domain points (mirrors server behaviour)
        if not curve_state:
            print(f"Failed to read curve: {curve_err}", file=sys.stderr)
            sys.exit(1)
        point_deltas = {p.index: delta_khz for p in curve_state.points if p.domain == "gpu"}

    effective_max = max_delta_khz if max_delta_khz is not None else default_config.max_delta_khz
    errors = validate_write(point_deltas, effective_max)
    if errors:
        for e in errors:
            print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    # Pre-write negative-frequency warnings, computed against the actual
    # pre-write frequencies and offsets (not the post-write state).
    pre_write_warnings = []
    if curve_state:
        vfp_freqs = [p.freq_khz for p in curve_state.points]
        cur_offsets = [p.delta_khz for p in curve_state.points]
        pre_write_warnings = check_negative_freq_warnings(point_deltas, vfp_freqs, cur_offsets)

    if default_config.auto_snapshot:
        from .hal.snapshot import save as snapshot_save
        snapshot_save(gpu, gpu_name, default_config.snapshot_dir, default_config.max_snapshots)

    ret, desc = write_offsets(gpu, point_deltas)
    if ret != 0:
        print(f"Write failed ({ret}): {desc}", file=sys.stderr)
        sys.exit(1)

    print(f"Write OK — {len(point_deltas)} point(s) updated.")

    for w in pre_write_warnings:
        print(f"WARNING: {w}")


def cmd_verify(args):
    """Write-verify-read cycle — runs directly against hardware (requires root)."""
    require_root()

    from .hal.gpu import get_gpu
    from .hal.vfcurve import read_clock_offsets, write_offsets
    from .hal.snapshot import save as snapshot_save

    delta_khz = int(args.delta * 1000)

    if args.point is not None:
        points = [args.point]
    elif args.range:
        points = list(range(args.range[0], args.range[1] + 1))
    else:
        print("Error: --point or --range required for verify mode", file=sys.stderr)
        sys.exit(2)

    point_deltas = {p: delta_khz for p in points}

    from .safety import validate_write
    errors = validate_write(point_deltas, default_config.max_delta_khz)
    if errors:
        for e in errors:
            print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    gpu, gpu_name = get_gpu(index=getattr(args, "gpu_index", 0))

    print("=== Write-Verify Cycle ===")
    print(f"GPU:    {gpu_name}")
    print(f"Points: {points[0]}{'–' + str(points[-1]) if len(points) > 1 else ''}")
    print(f"Delta:  {args.delta:+.0f} MHz ({delta_khz:+d} kHz)")
    print()

    # Step 1: read before state
    before_offsets, err = read_clock_offsets(gpu)
    if before_offsets is None:
        print(f"Failed to read current state: {err}", file=sys.stderr)
        sys.exit(1)

    # Step 2: snapshot before write
    filepath = snapshot_save(gpu, gpu_name, default_config.snapshot_dir, default_config.max_snapshots)
    if filepath:
        print(f"Snapshot saved: {filepath}")

    # Step 3: write
    print("Writing and verifying...")
    ret, desc = write_offsets(gpu, point_deltas)
    if ret != 0:
        print(f"Write failed ({ret}): {desc}", file=sys.stderr)
        sys.exit(1)

    time.sleep(0.2)

    # Step 4: read after state
    after_offsets, err = read_clock_offsets(gpu)
    if after_offsets is None:
        print(f"Verification read failed: {err}", file=sys.stderr)
        sys.exit(1)

    print()
    print("Verification results:")
    all_matched = True
    for p in sorted(point_deltas):
        expected = point_deltas[p]
        actual = after_offsets[p] if p < len(after_offsets) else 0
        match = actual == expected
        if not match:
            all_matched = False
        match_s = "OK" if match else "MISMATCH"
        print(f"  Point {p:3d}: expected {expected/1000:+8.0f} MHz, "
              f"got {actual/1000:+8.0f} MHz  [{match_s}]")

    collateral = [
        {"point": i, "before_khz": before_offsets[i], "after_khz": after_offsets[i]}
        for i in range(min(len(before_offsets), len(after_offsets)))
        if i not in point_deltas and before_offsets[i] != after_offsets[i]
    ]
    print()
    if collateral:
        print("Unintended side effects detected:")
        for c in collateral:
            print(f"  WARNING: Point {c['point']} changed: "
                  f"{c['before_khz']/1000:+.0f} → {c['after_khz']/1000:+.0f} MHz")
    else:
        print("No unintended side effects detected.")

    print()
    print("=" * 50)
    if all_matched and not collateral:
        print("RESULT: Write verified successfully.")
    elif not all_matched:
        print("RESULT: Write verification FAILED — offsets don't match.")
    else:
        print("RESULT: Write applied but with unexpected side effects.")

    print()
    print("To undo this change, run:")
    print("  nvcurve snapshot restore")


def cmd_snapshot(args):
    if args.action == "save":
        require_root()
        from .hal.gpu import get_gpu
        from .hal.snapshot import save as _snapshot_save
        gpu, gpu_name = get_gpu(index=getattr(args, "gpu_index", 0))
        path = _snapshot_save(gpu, gpu_name, default_config.snapshot_dir, default_config.max_snapshots)
        if path is None:
            print("Failed to save snapshot.", file=sys.stderr)
            sys.exit(1)
        print(f"Snapshot saved: {path}")

    elif args.action == "restore":
        require_root()
        from .hal.gpu import get_gpu
        from .hal.snapshot import restore as _snapshot_restore
        gpu, _ = get_gpu(index=getattr(args, "gpu_index", 0))
        ok = _snapshot_restore(gpu, default_config.snapshot_dir, args.file)
        if not ok:
            print("Restore failed — no snapshot found.", file=sys.stderr)
            sys.exit(1)
        print("Snapshot restored.")

    elif args.action == "list":
        from .hal.snapshot import list_snapshots as _list_snapshots
        snapshots = [
            {"filepath": s.filepath, "timestamp": s.timestamp,
             "gpu": s.gpu, "nonzero_offsets": s.nonzero_offsets}
            for s in _list_snapshots(default_config.snapshot_dir)
        ]
        if not snapshots:
            print("No snapshots found.")
            return
        print("Snapshots:")
        for s in snapshots:
            print(f"  {s['timestamp']}  {s['gpu']}  non-zero: {s['nonzero_offsets']}")
            print(f"    {s['filepath']}")


def _persistent_cfg_load() -> dict:
    """Read /etc/nvcurve/config.json, returning {} if absent or unreadable."""
    try:
        with open(_PERSISTENT_CONFIG_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _persistent_cfg_save(data: dict) -> None:
    """Write /etc/nvcurve/config.json, creating the parent directory if needed."""
    os.makedirs(os.path.dirname(_PERSISTENT_CONFIG_FILE), exist_ok=True)
    with open(_PERSISTENT_CONFIG_FILE, "w") as f:
        json.dump(data, f, indent=2)


def _profile_config_read() -> dict:
    """Read /etc/nvcurve/config.json, returning {} if absent or unreadable."""
    return _persistent_cfg_load()


def _gpu_stable_key_offline(gpu_index: int) -> str | None:
    """Resolve a stable GPU key offline (without the server) by calling discover_gpus().

    Returns None if the GPU index does not exist (discovery succeeded but index not found).
    Falls back to 'idx:{n}' if discovery itself fails (can't tell whether GPU exists).
    """
    try:
        from .hal.gpu import discover_gpus
        infos = discover_gpus()
        for info in infos:
            if info.index == gpu_index:
                if info.uuid:
                    return info.uuid
                if info.pci_bus_id is not None:
                    return f"pci:{info.pci_bus_id:04x}"
                return f"idx:{gpu_index}"
        # Discovery succeeded but this index wasn't among the found GPUs.
        return None
    except Exception:
        # Discovery failed entirely — fall back, can't validate existence.
        return f"idx:{gpu_index}"


def _profile_config_set_default(gpu_index: int, name: str | None) -> None:
    """Set or clear the default profile for a specific GPU in config.json."""
    data = _persistent_cfg_load()
    # Migrate old single-key format on write
    if "auto_load_profile" in data:
        data.setdefault("auto_load_profiles", {})["idx:0"] = data.pop("auto_load_profile")
    gpu_key = _gpu_stable_key_offline(gpu_index)
    if gpu_key is None:
        raise ValueError(f"GPU {gpu_index} not found")
    profiles = data.setdefault("auto_load_profiles", {})
    if name is None:
        profiles.pop(gpu_key, None)
    else:
        profiles[gpu_key] = name
    if not profiles:
        data.pop("auto_load_profiles", None)
    _persistent_cfg_save(data)


def cmd_profile(args):
    if args.action == "list":
        import glob as _glob, json as _json, os as _os
        gpu_index = getattr(args, "gpu_index", 0)
        profile_dir = default_config.profile_dir
        cfg_data = _profile_config_read()
        raw_defaults = cfg_data.get("auto_load_profiles", {})
        if not raw_defaults and "auto_load_profile" in cfg_data:
            raw_defaults = {"idx:0": cfg_data["auto_load_profile"]}
        gpu_key = _gpu_stable_key_offline(gpu_index)
        auto_load = raw_defaults.get(gpu_key) if gpu_key is not None else None
        raw = sorted(_glob.glob(_os.path.join(profile_dir, "*.json")))
        profiles = []
        for path in raw:
            try:
                with open(path) as f:
                    p = _json.load(f)
                name = _os.path.splitext(_os.path.basename(path))[0]
                profiles.append({"name": name, "curve_deltas": p.get("curve_deltas", {})})
            except Exception:
                pass
        if not profiles:
            print("No profiles found.")
            return
        print("Profiles:")
        for p in profiles:
            markers = []
            if p["name"] == auto_load: markers.append("default")
            marker_str = f"  [{', '.join(markers)}]" if markers else ""
            pts = len(p["curve_deltas"])
            print(f"  - {p['name']} ({pts} pts){marker_str}")

    elif args.action == "default":
        clearing = getattr(args, "clear", False)
        gpu_index = getattr(args, "gpu_index", 0)
        if not clearing and not args.name:
            print("Error: profile name required (or use --clear)", file=sys.stderr)
            sys.exit(2)
        require_root()
        try:
            _profile_config_set_default(gpu_index, None if clearing else args.name)
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
        if clearing:
            print(f"Auto-load profile cleared for GPU {gpu_index}.")
        else:
            print(f"Auto-load profile set to '{args.name}' for GPU {gpu_index}.")

    elif args.action == "save":
        if not args.name:
            print("Error: profile name required for save", file=sys.stderr)
            sys.exit(2)
        require_root()
        import os as _os
        from .hal.gpu import get_gpu
        from .hal.vfcurve import read_curve
        from .hal.limits import get_clock_offsets, get_power_limit
        from .profiles.native import ProfileData, save_profile

        gpu_index = getattr(args, "gpu_index", 0)
        gpu, gpu_name = get_gpu(index=gpu_index)

        curve_state, curve_err = read_curve(gpu, gpu_name)
        if not curve_state:
            print(f"Failed to read curve: {curve_err}", file=sys.stderr)
            sys.exit(1)

        curve_deltas = {str(p.index): p.delta_khz for p in curve_state.points if p.delta_khz != 0}

        try:
            power_info = get_power_limit(gpu_index)
            offsets = get_clock_offsets(gpu_index)
            power_limit_w = power_info.get("power_limit_w")
            mem_offset_mhz = offsets.get("mem_offset_mhz")
        except Exception:
            power_limit_w = None
            mem_offset_mhz = None

        # NVML has no query for the currently-applied memory locked-clock range
        # (unlike power limit / clock offsets, which are read back from hardware
        # above), so carry it forward from an existing profile of the same name
        # rather than silently dropping it on re-save.
        mem_locked_min_mhz = None
        mem_locked_max_mhz = None
        safe_name = "".join(c for c in args.name if c.isalnum() or c in " _-()").strip()
        existing_path = _os.path.join(default_config.profile_dir, f"{safe_name}.json")
        if _os.path.exists(existing_path):
            try:
                from .profiles.native import load_profile as _load_profile
                existing = _load_profile(existing_path)
                mem_locked_min_mhz = existing.mem_locked_min_mhz
                mem_locked_max_mhz = existing.mem_locked_max_mhz
            except Exception:
                pass

        data = ProfileData(
            name=args.name,
            gpu_name=gpu_name,
            curve_deltas=curve_deltas,
            mem_offset_mhz=mem_offset_mhz,
            power_limit_w=power_limit_w,
            mem_locked_min_mhz=mem_locked_min_mhz,
            mem_locked_max_mhz=mem_locked_max_mhz,
        )
        filepath = save_profile(default_config.profile_dir, data)
        print(f"Saved profile '{args.name}' to {filepath}")

    elif args.action == "apply":
        if not args.name:
            print("Error: profile name required for apply", file=sys.stderr)
            sys.exit(2)
        require_root()
        from .profiles.apply import apply_profile

        gpu_index = getattr(args, "gpu_index", 0)
        try:
            critical_errs, warnings = apply_profile(gpu_index, args.name, default_config)
        except FileNotFoundError:
            print(f"Profile '{args.name}' not found.", file=sys.stderr)
            sys.exit(1)

        for w in warnings:
            print(f"  warning: {w}", file=sys.stderr)

        if critical_errs:
            for e in critical_errs:
                print(f"  error: {e}", file=sys.stderr)
            print(f"Profile '{args.name}' applied with errors.", file=sys.stderr)
            sys.exit(1)

        if warnings:
            print(f"Applied profile '{args.name}' with warnings (see stderr).")
        else:
            print(f"Applied profile '{args.name}'.")


def cmd_gpus(args):
    """List all detected NVIDIA GPUs with index, name, UUID, and PCI bus ID."""
    from .hal.gpu import discover_gpus
    gpus = [
        {"index": i.index, "name": i.name, "uuid": i.uuid, "pci_bus_id": i.pci_bus_id}
        for i in discover_gpus()
    ]
    if not gpus:
        print("No NVIDIA GPUs detected.")
        return
    for g in gpus:
        uuid = g.get("uuid") or "N/A"
        pci = g.get("pci_bus_id")
        pci_str = f"PCI 0x{pci:04x}" if pci is not None else "PCI N/A"
        print(f"  [{g['index']}] {g['name']}  —  {uuid}  —  {pci_str}")


def cmd_memlock(args):
    """Get/set/reset the memory locked-clocks window (VRAM max-frequency lock).

    Unlike `write` (VF-curve offsets, NvAPI), this pins the memory clock to a
    fixed [min, max] MHz window via NVML — the same mechanism as nvidia_oc's
    `--min-mem-clock/--max-mem-clock`. It bypasses driver P-state down-clocking
    entirely rather than nudging the existing curve.
    """
    from .hal.limits import (
        get_supported_mem_clocks, get_max_mem_clock,
        set_mem_locked_clocks, reset_mem_locked_clocks, get_current_mem_clock,
    )

    gpu_index = getattr(args, "gpu_index", 0)
    action = args.action

    if action == "status":
        clocks = get_supported_mem_clocks(gpu_index)
        if not clocks:
            print("Supported memory clocks: unavailable (NVML not initialized or unsupported GPU)")
        else:
            print(f"Supported memory clocks: {clocks[0]}–{clocks[-1]} MHz "
                  f"({len(clocks)} steps)")
            print(f"Max lock target: {clocks[-1]} MHz — this is the GPU's stock/VBIOS clock table max, "
                  f"NOT the offset-boosted max. Requesting a lock above it silently snaps down to it.")
        print("Note: NVML does not expose whether a lock is currently active or its range;")
        print("this only reports what values are legal to lock to.")
        return

    if action == "reset":
        require_root()
        ok, msg = reset_mem_locked_clocks(gpu_index)
        if not ok:
            print(f"Error: {msg}", file=sys.stderr)
            sys.exit(1)
        print("Memory clock unlocked (returned to driver/P-state control).")
        return

    if action == "set":
        require_root()
        to_max = getattr(args, "to_max", False)
        max_mhz = getattr(args, "max", None)
        min_mhz = getattr(args, "min", None)

        if to_max:
            max_mhz = get_max_mem_clock(gpu_index)
            if max_mhz is None:
                print("Error: could not determine max supported memory clock "
                      "(NVML unavailable or unsupported GPU)", file=sys.stderr)
                sys.exit(1)
        elif max_mhz is None:
            print("Error: --max <MHz> or --to-max is required", file=sys.stderr)
            sys.exit(2)

        if min_mhz is None:
            min_mhz = max_mhz  # pin to a single fixed frequency by default

        ok, msg = set_mem_locked_clocks(min_mhz, max_mhz, gpu_index)
        if not ok:
            print(f"Error: {msg}", file=sys.stderr)
            sys.exit(1)

        # The driver silently snaps an unsupported request to the nearest
        # entry in its stock clock table instead of erroring — read back
        # what actually landed so the user isn't misled by the request alone.
        actual = get_current_mem_clock(gpu_index)
        if actual is not None and actual != max_mhz:
            print(f"Requested lock: {min_mhz}\u2013{max_mhz} MHz — driver resolved to {actual} MHz "
                  f"(nearest supported stock clock; see 'nvcurve memlock status' for the legal set).")
        else:
            print(f"Memory clock locked to {min_mhz}\u2013{max_mhz} MHz.")
        print("Note: a memory-offset (mem_offset_mhz) does not push the clock past this lock's "
              "ceiling — the locked value is a hard cap. What the offset changes is the voltage the "
              "driver uses to reach that same ceiling (this is the actual undervolt mechanism; see "
              "nvidia/LACT issue #486 for the community writeup). Also, set the lock BEFORE the "
              "offset (nvcurve does this automatically via `profile apply`) — doing it in the "
              "other order risks the lock call resetting the offset.")
        return


def cmd_setup(args):
    """One-shot hardware compatibility check: diag → read → write-verify → restore."""
    explicit_point = getattr(args, "point", None)
    verify_delta_mhz = getattr(args, "delta", 5.0) or 5.0
    verify_delta_khz = int(verify_delta_mhz * 1000)

    require_root()

    from .hal.gpu import get_gpu
    from .hal.vfcurve import read_curve, read_clock_offsets, write_offsets
    from .hal.monitoring import read_voltage
    from .hal.snapshot import save as snapshot_save, restore as snapshot_restore

    sep = "─" * 60

    print(sep)
    print("  NVCurve Setup — Hardware Compatibility Check")
    print(sep)
    print()

    gpu, gpu_name = get_gpu(index=getattr(args, "gpu_index", 0))

    # ── Step 1: NvAPI diagnostics ──────────────────────────────────────────────
    print("Step 1/4  NvAPI function probe")
    print()
    run_diagnostics(gpu, gpu_name, gpu_index=getattr(args, "gpu_index", 0))
    print()

    # ── Step 2: read current curve ─────────────────────────────────────────────
    print("Step 2/4  Current V/F curve")
    print()
    curve_state, curve_err = read_curve(gpu, gpu_name)
    if not curve_state:
        print(f"FAILED to read V/F curve: {curve_err}", file=sys.stderr)
        sys.exit(1)
    voltage, _ = read_voltage(gpu)
    pts = [(p.freq_khz, p.volt_uv) for p in curve_state.points]
    offsets = [p.delta_khz for p in curve_state.points]
    domains = [p.domain for p in curve_state.points]
    print_curve(pts, offsets, voltage, domains=domains)
    print()

    # Resolve verify point: explicit override, or last GPU-domain point.
    gpu_points = [p for p in curve_state.points if p.domain == "gpu"]
    if explicit_point is not None:
        verify_point = explicit_point
    elif gpu_points:
        verify_point = gpu_points[-1].index
    else:
        verify_point = len(curve_state.points) - 1

    # ── Step 3: write-verify cycle ─────────────────────────────────────────────
    if verify_point >= len(curve_state.points):
        print(f"Step 3/4  Write-verify  (SKIPPED — point {verify_point} not present; "
              f"GPU has {len(curve_state.points)} points)")
        print()
        print(sep)
        print("  RESULT: Diagnostics passed. Write-verify skipped.")
        print(f"  Use --point to specify a valid point index (0–{len(curve_state.points) - 1}).")
        print(sep)
        return

    print(f"Step 3/4  Write-verify  ({verify_delta_mhz:+.0f} MHz at point {verify_point})")
    print()

    snap_path = snapshot_save(gpu, gpu_name, default_config.snapshot_dir, default_config.max_snapshots)
    if snap_path:
        print(f"  Snapshot saved: {snap_path}")

    before_offsets, err = read_clock_offsets(gpu)
    if before_offsets is None:
        print(f"  FAILED to read state before write: {err}", file=sys.stderr)
        sys.exit(1)

    full_mask = getattr(args, "full_mask", False)
    ret, desc = write_offsets(gpu, {verify_point: verify_delta_khz}, full_mask=full_mask)
    if ret != 0:
        print(f"  Write FAILED ({ret}): {desc}")
        print()
        print(sep)
        print("  RESULT: Write path is NOT working on this configuration.")
        print(sep)
        sys.exit(1)

    time.sleep(0.2)

    after_offsets, err = read_clock_offsets(gpu)
    if after_offsets is None:
        print(f"  Verification read FAILED: {err}", file=sys.stderr)
        sys.exit(1)

    actual = after_offsets[verify_point]
    matched = actual == verify_delta_khz
    collateral = [
        i for i in range(min(len(before_offsets), len(after_offsets)))
        if i != verify_point and before_offsets[i] != after_offsets[i]
    ]

    if matched:
        print(f"  Point {verify_point}: {verify_delta_khz / 1000:+.0f} MHz  OK")
    else:
        print(f"  Point {verify_point}: MISMATCH — expected {verify_delta_khz / 1000:+.0f} MHz, "
              f"got {actual / 1000:+.0f} MHz")
    if collateral:
        print(f"  WARNING: {len(collateral)} collateral point(s) changed: {collateral[:5]}")
    else:
        print("  No collateral changes")

    # ── Step 4: restore ────────────────────────────────────────────────────────
    print()
    print("Step 4/4  Restoring snapshot")
    print()
    ok = snapshot_restore(gpu, default_config.snapshot_dir, snap_path)
    if ok:
        print("  Hardware state restored to baseline.")
    else:
        print("  WARNING: Restore failed. Run:  nvcurve snapshot restore", file=sys.stderr)

    print()
    print(sep)
    if matched and not collateral:
        print("  RESULT: Compatible. NVCurve is ready to use.")
        print()
        print("  Next:  nvcurve                  launch web UI")
        print("         nvcurve service install   auto-start on boot")
    elif not matched:
        print("  RESULT: Write verification FAILED — this configuration is not supported.")
    else:
        print("  RESULT: Write applied but unexpected collateral changes detected.")
        print("  Review the output above before using write operations.")
    print(sep)


def _systemctl(*sc_args: str, check: bool = True) -> bool:
    """Run `systemctl <sc_args>`, printing a consistent error on failure.

    Returns True on success, False on failure (when check=True and the
    command fails). With check=False, a non-zero exit is not treated as an
    error (used for is-active/best-effort calls).
    """
    import subprocess
    try:
        subprocess.run(["systemctl", *sc_args], check=check)
        return True
    except subprocess.CalledProcessError as e:
        print(f"systemctl {' '.join(sc_args)} failed: {e}", file=sys.stderr)
        return False


def cmd_service(args):
    """Manage the nvcurve systemd service."""
    action = getattr(args, "action", None)
    if not action:
        print("Usage: nvcurve service [install|uninstall|start|stop|restart|status]")
        return

    unit_path = "/etc/systemd/system/nvcurve.service"

    if action == "install":
        require_root()
        import subprocess

        exec_start = f"{sys.executable} -m nvcurve daemon"

        # Hardening notes (things deliberately left alone):
        #   - No DevicePolicy=/PrivateDevices= — the daemon's spawned server
        #     subprocess needs /dev/nvidia* access; PrivateDevices=yes in
        #     particular would hide all devices including those and break
        #     GPU access outright, so it's not used here.
        #   - ProtectKernelModules=yes only blocks loading new kernel modules
        #     (finit_module/init_module), not ioctls to an already-loaded
        #     driver — safe given the Wants=/After=nvidia-persistenced.service
        #     ordering already ensures the driver is loaded first.
        #   - RestrictAddressFamilies allows AF_INET/AF_INET6 (the spawned
        #     web server's HTTP/WS socket) alongside AF_UNIX (the daemon's
        #     own control socket) — both processes share this unit's sandbox.
        unit = (
            "[Unit]\n"
            "Description=NVCurve NVIDIA GPU V/F Curve Daemon\n"
            "After=nvidia-persistenced.service\n"
            "Wants=nvidia-persistenced.service\n"
            "\n"
            "[Service]\n"
            "Type=simple\n"
            f"ExecStart={exec_start}\n"
            "Restart=on-failure\n"
            "RestartSec=5\n"
            "Environment=PYTHONDONTWRITEBYTECODE=1\n"
            "NoNewPrivileges=yes\n"
            "ProtectHome=yes\n"
            "PrivateTmp=yes\n"
            "ProtectSystem=strict\n"
            "ProtectKernelModules=yes\n"
            "ProtectKernelTunables=yes\n"
            "ProtectControlGroups=yes\n"
            "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6\n"
            "ReadWritePaths=/etc/nvcurve /var/cache/nvcurve /var/log /run\n"
            "UMask=0027\n"
            "\n"
            "[Install]\n"
            "WantedBy=multi-user.target\n"
        )

        with open(unit_path, "w") as f:
            f.write(unit)
        print(f"Unit file written to {unit_path}")

        # Write persistent config.
        persistent_cfg = _persistent_cfg_load()
        # Only override host/port if the user explicitly passed them — otherwise
        # keep whatever was previously configured (falling back to defaults on
        # first install), so a bare re-install doesn't clobber prior settings.
        host_arg = getattr(args, "host", None)
        port_arg = getattr(args, "port", None)
        auto_serve = getattr(args, "auto_serve", False)
        if host_arg is not None:
            persistent_cfg["host"] = host_arg
        if port_arg is not None:
            persistent_cfg["port"] = port_arg
        persistent_cfg.setdefault("host", "127.0.0.1")
        persistent_cfg.setdefault("port", 8042)
        persistent_cfg["auto_serve"] = auto_serve
        host = persistent_cfg["host"]
        port = persistent_cfg["port"]
        _persistent_cfg_save(persistent_cfg)
        print(f"Persistent config written to {_PERSISTENT_CONFIG_FILE}")
        if auto_serve:
            print(f"  Web server will auto-start on boot at {host}:{port}")
        else:
            print(f"  Web server default: {host}:{port}  (start on demand: nvcurve serve start)")

        if not _systemctl("daemon-reload"):
            return

        was_active = subprocess.run(
            ["systemctl", "is-active", "--quiet", "nvcurve"],
        ).returncode == 0

        if not _systemctl("enable", "--now", "nvcurve"):
            return
        print("Service enabled and started.")

        if was_active:
            print()
            print("Note: the service was already running and is still on the old version.")
            print("  Restart it to pick up the update:  nvcurve service restart")

        print()
        print("Useful commands:")
        print("  systemctl status nvcurve")
        print("  journalctl -u nvcurve -f")
        print("  nvcurve service uninstall")

    elif action == "uninstall":
        require_root()
        import subprocess

        if not os.path.exists(unit_path):
            print("Service is not installed.")
            return
        try:
            _systemctl("stop", "nvcurve", check=False)
            _systemctl("disable", "nvcurve", check=False)
            os.remove(unit_path)
            if os.path.exists(_PERSISTENT_CONFIG_FILE):
                os.remove(_PERSISTENT_CONFIG_FILE)
            if not _systemctl("daemon-reload"):
                return
            print("Service stopped, disabled, and removed.")
        except Exception as e:
            print(f"Error during uninstall: {e}", file=sys.stderr)

    elif action == "start":
        require_root()
        if not os.path.exists(unit_path):
            print("Service is not installed. Run: nvcurve service install")
            return
        if _systemctl("start", "nvcurve"):
            print("Service started.")

    elif action == "stop":
        require_root()
        if not os.path.exists(unit_path):
            print("Service is not installed.")
            return
        if _systemctl("stop", "nvcurve"):
            print("Service stopped.")

    elif action == "restart":
        require_root()
        if not os.path.exists(unit_path):
            print("Service is not installed. Run: nvcurve service install")
            return
        if _systemctl("restart", "nvcurve"):
            print("Service restarted.")

    elif action == "status":
        import subprocess

        if os.path.exists(unit_path):
            result = subprocess.run(
                ["systemctl", "is-active", "nvcurve"],
                capture_output=True, text=True,
            )
            active = result.stdout.strip()
            pid_info = ""
            if active == "active":
                r2 = subprocess.run(
                    ["systemctl", "show", "nvcurve", "--property=MainPID"],
                    capture_output=True, text=True,
                )
                pid = r2.stdout.strip().replace("MainPID=", "")
                if pid and pid != "0":
                    pid_info = f" (PID {pid})"
            print(f"systemd service: {active}{pid_info}")
        else:
            print("systemd service: not installed")
            print(f"  (no unit file at {unit_path})")
            print()
            print("Register with:  nvcurve service install")

        # Show persistent config regardless of install state.
        pcfg = _persistent_cfg_load()

        auto_serve = pcfg.get("auto_serve", False)
        host = pcfg.get("host", "127.0.0.1")
        port = pcfg.get("port", 8042)
        print()
        print(f"web server auto-start: {'on' if auto_serve else 'off'}")
        print(f"web server address:    {host}:{port}")
        print()
        print("Change with:  nvcurve service configure [--auto-serve|--no-auto-serve] [--host H] [--port P]")

    elif action == "configure":
        require_root()

        pcfg = _persistent_cfg_load()

        if hasattr(args, "auto_serve") and args.auto_serve is not None:
            pcfg["auto_serve"] = args.auto_serve
        if hasattr(args, "host") and args.host is not None:
            pcfg["host"] = args.host
        if hasattr(args, "port") and args.port is not None:
            pcfg["port"] = args.port

        _persistent_cfg_save(pcfg)
        print(f"Config updated ({_PERSISTENT_CONFIG_FILE}):")
        print(f"  auto-serve: {'on' if pcfg.get('auto_serve', False) else 'off'}")
        print(f"  host:       {pcfg.get('host', '127.0.0.1')}")
        print(f"  port:       {pcfg.get('port', 8042)}")

        if os.path.exists(unit_path):
            if _systemctl("restart", "nvcurve"):
                print("Daemon restarted — new config is active.")
        else:
            print("(Service not installed — config will take effect on next install.)")


# ── Server management ─────────────────────────────────────────────────────────

def _cmd_serve_start(args, cfg: Config, open_browser: bool = False) -> None:
    """Start the web server — via daemon if available, otherwise directly (requires root)."""
    host = getattr(args, "host", cfg.host)
    port = getattr(args, "port", cfg.port)

    # --direct: skip daemon round-trip (used when the daemon itself spawns us).
    if getattr(args, "direct", False):
        require_root()
        with open(_SERVER_INFO_FILE, "w") as f:
            json.dump({"pid": os.getpid(), "host": host, "port": port}, f)
        try:
            from .server import run as server_run
            server_run(host=host, port=port,
                       gpu_index=getattr(args, "gpu_index", 0),
                       config=cfg, open_browser=False)
        finally:
            if os.path.exists(_SERVER_INFO_FILE):
                os.remove(_SERVER_INFO_FILE)
        return

    # Prefer daemon socket: no root required, daemon manages the server process.
    resp = _daemon_send({"cmd": "serve_start", "host": host, "port": port})
    if resp is not None:
        if resp.get("ok"):
            print(f"Web server starting (PID {resp['pid']}) at http://{host}:{port}")
            if open_browser:
                time.sleep(1.5)
                _open_browser_as_user(f"http://{host}:{port}")
        else:
            print(f"Daemon: {resp.get('error')}", file=sys.stderr)
        return

    # Daemon not running — fall back to direct start (requires root).
    require_root()

    info = _read_server_info()
    if info:
        url = f"http://{info['host']}:{info['port']}"
        print(f"Server is already running (PID {info['pid']}) at {url}.")
        if open_browser:
            _open_browser_as_user(url)
        return

    if getattr(args, "detach", False):
        import subprocess
        cmd = [sys.executable, "-m", "nvcurve", "serve", "start",
               "--host", host, "--port", str(port)]
        if getattr(args, "gpu_index", 0):
            cmd += ["--gpu", str(args.gpu_index)]
        log_path = _log_file()
        print("Starting nvcurve server in background...")
        with open(log_path, "a") as lf:
            p = subprocess.Popen(cmd, stdout=lf, stderr=lf, start_new_session=True)
        print(f"Server starting (PID {p.pid}). Logs: {log_path}")
        if open_browser:
            time.sleep(1.5)
            _open_browser_as_user(_discover_server_url(cfg))
        return

    # Foreground mode — write info file so clients can discover host:port.
    with open(_SERVER_INFO_FILE, "w") as f:
        json.dump({"pid": os.getpid(), "host": host, "port": port}, f)
    try:
        from .server import run as server_run
        server_run(
            host=host,
            port=port,
            gpu_index=getattr(args, "gpu_index", 0),
            config=cfg,
            open_browser=open_browser,
        )
    finally:
        if os.path.exists(_SERVER_INFO_FILE):
            os.remove(_SERVER_INFO_FILE)


# ── Argument parser ───────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    from importlib.metadata import version as pkg_version
    try:
        __version__ = pkg_version("nvcurve")
    except Exception:
        __version__ = "unknown"

    # Shared global flags (--server, --gpu). These are attached as `parents`
    # to the top-level parser AND to every subparser (including nested ones),
    # so they can be given either before or after the subcommand — e.g. both
    # `nvcurve --gpu 1 write ...` and `nvcurve write --gpu 1 ...` work, exactly
    # as they did under the previous two-parser argv-splitting approach.
    #
    # default=SUPPRESS is required here: without it, argparse would apply the
    # subparser's own default for --gpu/--server AFTER parsing (even when the
    # flag wasn't given to the subcommand), silently overwriting a value the
    # user set at the top level (e.g. `nvcurve --gpu 1 write ...` would reset
    # gpu_index back to 0). With SUPPRESS, the attribute is only set when the
    # flag actually appears somewhere on the command line; callers read it via
    # getattr(args, "gpu_index", 0) / getattr(args, "server", None).
    gflags = argparse.ArgumentParser(add_help=False)
    gflags.add_argument(
        "--server", default=argparse.SUPPRESS, metavar="URL",
        help="Server base URL (default: http://127.0.0.1:8042)",
    )
    gflags.add_argument(
        "--gpu", type=int, default=argparse.SUPPRESS, dest="gpu_index",
        help="GPU index to target (default: 0)",
    )

    parser = argparse.ArgumentParser(
        prog="nvcurve",
        description="Read/Write NVIDIA GPU V/F curve via undocumented NvAPI (Linux)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        parents=[gflags],
        epilog="""\
Examples:
  %(prog)s                               Launch web UI (default)
  %(prog)s read                          Condensed V/F curve
  %(prog)s read --full                   All points
  %(prog)s read --json                   JSON output
  %(prog)s write --global --delta 50     +50 MHz to all points
  %(prog)s write --point 80 --delta 100  +100 MHz to point 80
  %(prog)s write --reset                 Reset all offsets to 0
  %(prog)s verify --point 80 --delta 15  Write + verify cycle
  %(prog)s snapshot save/restore/list    Manage snapshots
  %(prog)s profile save balanced         Save current state as profile
  %(prog)s profile apply balanced        Apply saved profile (escalates to root)
  %(prog)s profile default balanced      Set profile to auto-load on daemon start
  %(prog)s memlock set --to-max          Lock VRAM to its max supported frequency
  %(prog)s memlock reset                 Unlock VRAM (return to driver control)
  %(prog)s serve start --detach          Start web server in background
  %(prog)s serve stop                    Stop running web server
  %(prog)s service install               Register daemon as systemd service (recommended)
  %(prog)s read --diag                   Probe all NvAPI functions (needs root)
  %(prog)s inspect --point 80            Raw buffer fields for a point (needs root)
""",
    )
    parser.add_argument("-v", "--version", action="version", version=f"nvcurve {__version__}")
    sub = parser.add_subparsers(dest="command")

    # read
    p_read = sub.add_parser("read", help="Read V/F curve", parents=[gflags])
    p_read.set_defaults(func=cmd_read)
    p_read.add_argument("--full", action="store_true", help="Show all points")
    p_read.add_argument("--json", action="store_true", help="JSON output")
    p_read.add_argument("--raw", action="store_true",
                        help="Raw hex dumps of hardware buffers (needs root)")
    p_read.add_argument("--diag", action="store_true",
                        help="Probe all NvAPI functions (needs root)")

    # inspect
    p_insp = sub.add_parser("inspect",
                             help="Show raw ClockBoostTable buffer fields (needs root)",
                             parents=[gflags])
    p_insp.add_argument("--point", type=int, help="Single point index")
    p_insp.add_argument("--range", type=parse_range, help="Point range A-B")
    p_insp.set_defaults(func=cmd_inspect)

    # write
    p_write = sub.add_parser("write", help="Write frequency offsets", parents=[gflags])
    p_write.set_defaults(func=cmd_write)
    tgt = p_write.add_mutually_exclusive_group()
    tgt.add_argument("--point", type=int, help="Single point index")
    tgt.add_argument("--range", type=parse_range, help="Point range A-B")
    tgt.add_argument("--global", dest="glob", action="store_true",
                     help="All points (like global NVML offset)")
    tgt.add_argument("--reset", action="store_true", help="Reset all offsets to 0")
    p_write.add_argument("--delta", type=float, default=None,
                         help="Frequency offset in MHz (e.g. 15, -30). "
                              "Required unless --reset is used.")
    p_write.add_argument("--dry-run", action="store_true",
                         help="Preview changes without applying")
    p_write.add_argument("--max-delta", type=float, default=None,
                         help="Override safety limit for this write (MHz)")

    # verify
    p_ver = sub.add_parser("verify", help="Write-verify-read cycle", parents=[gflags])
    p_ver.set_defaults(func=cmd_verify)
    p_ver.add_argument("--point", type=int, help="Single point index")
    p_ver.add_argument("--range", type=parse_range, help="Point range A-B")
    p_ver.add_argument("--delta", type=float, required=True,
                       help="Frequency offset in MHz")

    # setup
    p_setup = sub.add_parser("setup",
                              help="Hardware compatibility check: diag → read → write-verify → restore (needs root)",
                              parents=[gflags])
    p_setup.add_argument("--point", type=int, default=None,
                         help="Point index to use for write-verify test (default: last GPU-domain point)")
    p_setup.add_argument("--delta", type=float, default=5.0,
                         help="Offset in MHz to use for write-verify test (default: +5)")
    p_setup.add_argument("--full-mask", action="store_true",
                         help="Use the full GetClockBoostMask instead of a sparse mask "
                              "(try this if writes fail on older GPUs such as Pascal)")
    p_setup.set_defaults(func=cmd_setup)

    # snapshot
    p_snap = sub.add_parser("snapshot",
                             help="Save/restore/list ClockBoostTable snapshots",
                             parents=[gflags])
    p_snap.add_argument("action", choices=["save", "restore", "list"])
    p_snap.add_argument("--file", help="Snapshot file path (for restore)")
    p_snap.set_defaults(func=cmd_snapshot)

    # gpus
    sub.add_parser("gpus", help="List detected NVIDIA GPUs (server-optional)",
                    parents=[gflags]).set_defaults(func=cmd_gpus)

    # profile
    p_prof = sub.add_parser("profile", help="Manage saved clock/limit profiles (server-optional)",
                             parents=[gflags])
    p_prof.add_argument("action", choices=["save", "apply", "list", "default"])
    p_prof.add_argument("name", nargs="?", help="Profile name (for save/apply/default)")
    p_prof.add_argument("--clear", action="store_true", help="Clear the default profile (for default action)")
    p_prof.set_defaults(func=cmd_profile)

    # memlock
    p_memlock = sub.add_parser("memlock",
                                help="VRAM locked-clock (max-frequency) control (server-optional)",
                                parents=[gflags])
    p_memlock.set_defaults(func=cmd_memlock)
    p_memlock.add_argument("action", choices=["status", "set", "reset"])
    p_memlock.add_argument("--max", type=int, default=None, metavar="MHz",
                            help="Lock max memory clock to this frequency (MHz)")
    p_memlock.add_argument("--min", type=int, default=None, metavar="MHz",
                            help="Lock min memory clock to this frequency (MHz); "
                                 "defaults to --max (pins to a single fixed frequency)")
    p_memlock.add_argument("--to-max", dest="to_max", action="store_true",
                            help="Lock to the highest memory clock the driver reports as supported")

    # daemon
    sub.add_parser("daemon", help="Run the nvcurve daemon (apply auto-load profiles, requires root)",
                    parents=[gflags])

    # autoload
    sub.add_parser("autoload", help="Apply auto-load profiles from config (requires root)",
                    parents=[gflags])

    # serve
    p_srv = sub.add_parser("serve", help="Start or manage the web server", parents=[gflags])
    s_srv = p_srv.add_subparsers(dest="action")

    p_start = s_srv.add_parser("start", help="Start the server (escalates to root)", parents=[gflags])
    p_start.add_argument("--host", default="127.0.0.1",
                         help="Bind address (default 127.0.0.1)")
    p_start.add_argument("--port", type=int, default=8042,
                         help="Port (default 8042)")
    p_start.add_argument("--detach", "-d", action="store_true",
                         help="Run in background")
    p_start.add_argument("--direct", action="store_true",
                         help=argparse.SUPPRESS)  # internal: skip daemon check

    s_srv.add_parser("stop", help="Stop the running server", parents=[gflags])
    s_srv.add_parser("status", help="Check server status", parents=[gflags])

    # service
    p_svc = sub.add_parser("service", help="Manage the nvcurve systemd service", parents=[gflags])
    p_svc.set_defaults(func=cmd_service)
    s_svc = p_svc.add_subparsers(dest="action")

    p_install = s_svc.add_parser("install",
                                  help="Register as systemd service (escalates to root)",
                                  parents=[gflags])
    p_install.add_argument("--auto-serve", action="store_true", dest="auto_serve",
                           help="Auto-start web server on boot (default: off)")
    p_install.add_argument("--host", default=None,
                           help="Default web server bind address (stored in config; "
                                "default 127.0.0.1 on first install, otherwise unchanged)")
    p_install.add_argument("--port", type=int, default=None,
                           help="Default web server port (stored in config; "
                                "default 8042 on first install, otherwise unchanged)")

    p_configure = s_svc.add_parser("configure",
                                    help="Update config and restart daemon (escalates to root)",
                                    parents=[gflags])
    p_configure.add_argument("--auto-serve", dest="auto_serve",
                             action="store_true", default=None,
                             help="Auto-start web server on boot")
    p_configure.add_argument("--no-auto-serve", dest="auto_serve",
                             action="store_false",
                             help="Do not auto-start web server on boot")
    p_configure.add_argument("--host", default=None,
                             help="Web server bind address")
    p_configure.add_argument("--port", type=int, default=None,
                             help="Web server port")

    s_svc.add_parser("uninstall",
                     help="Remove systemd service (escalates to root)",
                     parents=[gflags])
    s_svc.add_parser("start",
                     help="Start systemd service (escalates to root)",
                     parents=[gflags])
    s_svc.add_parser("stop",
                     help="Stop systemd service (escalates to root)",
                     parents=[gflags])
    s_svc.add_parser("restart",
                     help="Restart systemd service (escalates to root)",
                     parents=[gflags])
    s_svc.add_parser("status", help="Check systemd service status", parents=[gflags])

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    cfg = Config()
    data = _persistent_cfg_load()
    if "auto_load_profiles" in data:
        # Keys are stable GPU identifiers (UUID, "pci:XXXX", or "idx:N")
        cfg.auto_load_profiles = dict(data["auto_load_profiles"])
    elif "auto_load_profile" in data:
        # Migrate old single-string format — GPU 0, no UUID known at this point
        cfg.auto_load_profiles = {"idx:0": data["auto_load_profile"]}

    base_url = getattr(args, "server", None) or _discover_server_url(cfg)
    client = NvCurveClient(base=base_url, gpu_index=getattr(args, "gpu_index", 0))

    # hal/gpu.py and nvapi/bootstrap.py used to call print()+sys.exit(1)
    # directly on driver/enumeration failures. That's now raised as one of
    # the exceptions below instead (so server.py/daemon.py/tests can catch
    # it too), which means the CLI has to reproduce the old "clean message,
    # exit 1" behaviour itself, once, here — rather than at each of the ~16
    # call sites in _dispatch() that previously didn't need to care.
    try:
        _dispatch(args, cfg, client, base_url)
    except NvApiUnavailableError as e:
        print(f"nvcurve: {e}", file=sys.stderr)
        sys.exit(1)
    except NoGpuFoundError as e:
        print(f"nvcurve: {e}", file=sys.stderr)
        sys.exit(1)
    except GpuIndexError as e:
        print(f"nvcurve: {e}", file=sys.stderr)
        sys.exit(2)


def _dispatch(args, cfg: Config, client: NvCurveClient, base_url: str) -> None:
    # Default — no subcommand: open the web UI.
    # If the server is already running, just open a browser tab (no root needed).
    # Otherwise start it. If we started it via the daemon (non-blocking), we
    # block here and stop the server when the user hits Ctrl+C.
    if args.command is None:
        if client.ping():
            _open_browser_as_user(base_url)
            return

        via_daemon = _daemon_send({"cmd": "ping"}) is not None
        _cmd_serve_start(args, cfg, open_browser=True)

        if via_daemon:
            print("Press Ctrl+C to stop.")
            try:
                while True:
                    time.sleep(1)
            except KeyboardInterrupt:
                print()
            finally:
                _daemon_send({"cmd": "serve_stop"})
        return

    if args.command == "serve":
        action = getattr(args, "action", None) or "start"

        if action == "start":
            _cmd_serve_start(args, cfg, open_browser=False)

        elif action == "stop":
            # Try daemon socket first.
            resp = _daemon_send({"cmd": "serve_stop"})
            if resp is not None:
                if resp.get("ok"):
                    print("Web server stopped.")
                else:
                    print(f"Daemon: {resp.get('error')}", file=sys.stderr)
                return
            # Fallback: send shutdown via HTTP API.
            try:
                client.shutdown()
                print("Server stopped.")
            except ServerNotRunning:
                print("Server is not running.")
                if os.path.exists(_SERVER_INFO_FILE):
                    try:
                        os.remove(_SERVER_INFO_FILE)
                    except OSError:
                        pass
            except ApiError as e:
                print(f"Shutdown failed: {e.detail}", file=sys.stderr)

        elif action == "status":
            # Try daemon socket first.
            resp = _daemon_send({"cmd": "serve_status"})
            if resp is not None:
                if resp.get("running"):
                    pid = resp.get("pid")
                    print(f"Daemon: web server running (PID {pid}) at {base_url}")
                else:
                    print(f"Daemon: web server not running")
                return
            # Fallback: check HTTP directly.
            if client.ping():
                try:
                    info = client.gpu()
                    print(f"Server is running at {base_url}")
                    print(f"  GPU: {info.get('name', '?')}")
                    driver = info.get("driver_version")
                    if driver:
                        print(f"  Driver: {driver}")
                except Exception:
                    print(f"Server is running at {base_url}")
            else:
                print(f"Server is NOT running at {base_url}")
        return

    if args.command == "daemon":
        from .daemon import run as daemon_run
        daemon_run()
        return

    if args.command == "autoload":
        from .profiles.apply import run_autoload
        run_autoload()
        return

    func = getattr(args, "func", None)
    if func is not None:
        func(args)
