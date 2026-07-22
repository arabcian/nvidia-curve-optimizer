"""Hardware Abstraction Layer for Global Limits (Power, Clock Offsets).

Uses NVML (via pynvml) for all operations.

Clock offsets use nvmlDeviceSetClockOffsets / nvmlDeviceGetClockOffsets
(introduced in driver 555.85).  The older per-domain functions
(nvmlDeviceSet/GetGpcClkVfOffset, nvmlDeviceSet/GetMemClkVfOffset) are used
as a fallback when the new API is unavailable or returns an error.
set_clock_offsets accepts Optional values and only touches the domains
that are explicitly specified, leaving others unchanged on hardware.
"""

import ctypes
import subprocess
import logging
from typing import Optional

try:
    import pynvml
    _NVML_AVAILABLE = True
except ImportError:
    _NVML_AVAILABLE = False

log = logging.getLogger("nvcurve.hal.limits")

# ── NVML library / handle helpers ─────────────────────────────────────────────

_nvml_lib: Optional[ctypes.CDLL] = None


def _nvml_cdll() -> ctypes.CDLL:
    """Return a ctypes handle to libnvidia-ml, reusing pynvml's load if possible."""
    global _nvml_lib
    if _nvml_lib is not None:
        return _nvml_lib
    # Prefer to reuse the library already loaded by pynvml to avoid dlopen races.
    for attr in ("nvml", "_nvml"):          # attribute name varies by pynvml version
        mod = getattr(pynvml, attr, None)
        lib = getattr(mod, "_lib", None) or getattr(mod, "_nvmlLib", None)
        if lib is not None:
            _nvml_lib = lib
            return _nvml_lib
    _nvml_lib = ctypes.CDLL("libnvidia-ml.so.1")
    return _nvml_lib


_nvml_initialized = False


def _ensure_nvml_init() -> None:
    """Call nvmlInit() once per process if it hasn't happened yet.

    Every `nvcurve <cmd>` invocation is a fresh subprocess, so unlike the
    long-lived daemon/server, nothing has necessarily called nvmlInit()
    before hal.limits functions run (e.g. `nvcurve memlock ...`,
    `nvcurve profile apply ...` when invoked directly). Without this, every
    call below fails with NVML_ERROR_UNINITIALIZED ("Uninitialized").
    nvmlInit() is safe to call more than once (refcounted internally), so a
    module-level flag is just an optimization, not a correctness guard.
    """
    global _nvml_initialized
    if _nvml_initialized:
        return
    try:
        pynvml.nvmlInit()
        _nvml_initialized = True
    except Exception as exc:
        # Leave the flag False so the next call retries; the actual NVML
        # call below will surface a clearer error if init is truly broken.
        log.debug("_ensure_nvml_init: %s", exc)


def _get_handle(gpu_index: int):
    """Return an NVML device handle, initialising pynvml if needed."""
    if not _NVML_AVAILABLE:
        raise RuntimeError("NVML not available (install nvidia-ml-py)")
    _ensure_nvml_init()
    return pynvml.nvmlDeviceGetHandleByIndex(gpu_index)


# ── Power limit ───────────────────────────────────────────────────────────────

def get_power_limit(gpu_index: int = 0) -> dict:
    """Return dict with power_limit_w, default_power_limit_w, min_power_limit_w, max_power_limit_w."""
    out = {
        "power_limit_w": None,
        "default_power_limit_w": None,
        "min_power_limit_w": None,
        "max_power_limit_w": None,
    }
    try:
        handle = _get_handle(gpu_index)
        limit = pynvml.nvmlDeviceGetPowerManagementLimit(handle)
        constrs = pynvml.nvmlDeviceGetPowerManagementLimitConstraints(handle)
        out["power_limit_w"] = limit // 1000
        out["min_power_limit_w"] = constrs[0] // 1000
        out["max_power_limit_w"] = constrs[1] // 1000
        try:
            default = pynvml.nvmlDeviceGetPowerManagementDefaultLimit(handle)
            out["default_power_limit_w"] = default // 1000
        except Exception:
            pass
    except Exception as exc:
        log.warning("get_power_limit: %s", exc)
    return out


def set_power_limit(limit_w: int, gpu_index: int = 0) -> tuple[bool, str]:
    """Set the board power limit (Watts)."""
    try:
        handle = _get_handle(gpu_index)
        pynvml.nvmlDeviceSetPowerManagementLimit(handle, limit_w * 1000)
        return True, "OK"
    except Exception as exc:
        log.debug("NVML set_power_limit failed: %s — falling back to nvidia-smi", exc)

    ret = subprocess.run(
        ["nvidia-smi", "-i", str(gpu_index), "-pl", str(limit_w)],
        capture_output=True, text=True,
    )
    if ret.returncode == 0:
        return True, "OK"
    return False, ret.stderr.strip() or ret.stdout.strip()


# ── Clock offsets (GPC + memory) ──────────────────────────────────────────────

# The correct struct layout (per NVML docs and driver 590.x headers):
#
#   typedef struct {
#       unsigned int  version;          // nvmlClockOffset_v1
#       nvmlClockType_t type;           // NVML_CLOCK_GRAPHICS (0) or NVML_CLOCK_MEM (2)
#       nvmlPstates_t   pstate;         // NVML_PSTATE_0 (0)
#       int             clockOffsetMHz;
#   } nvmlClockOffset_t;
#
# nvmlDeviceSet/GetClockOffsets are called ONCE PER CLOCK DOMAIN.
# pynvml (nvidia-ml-py ≥ 12) exposes c_nvmlClockOffset_t and nvmlClockOffset_v1
# as ctypes objects; we use them when available and fall back to our own definition.

class _ClockOffset(ctypes.Structure):
    _fields_ = [
        ("version",        ctypes.c_uint),
        ("type",           ctypes.c_uint),   # nvmlClockType_t
        ("pstate",         ctypes.c_uint),   # nvmlPstates_t
        ("clockOffsetMHz", ctypes.c_int),
    ]

_CLOCK_OFFSET_VER = (1 << 24) | ctypes.sizeof(_ClockOffset)  # = 0x01000010 (16 bytes)

# NVML clock-type constants (same values as pynvml).
_NVML_CLOCK_GRAPHICS = 0
_NVML_CLOCK_MEM      = 2


def _make_clock_offset(clock_type: int, pstate: int = 0, offset_mhz: int = 0) -> ctypes.Structure:
    """Return a populated nvmlClockOffset_t struct, using pynvml's type when available."""
    if hasattr(pynvml, "c_nvmlClockOffset_t") and hasattr(pynvml, "nvmlClockOffset_v1"):
        info = pynvml.c_nvmlClockOffset_t()
        info.version        = pynvml.nvmlClockOffset_v1
        info.type           = clock_type
        info.pstate         = pstate
        info.clockOffsetMHz = offset_mhz
        return info
    info = _ClockOffset()
    info.version        = _CLOCK_OFFSET_VER
    info.type           = clock_type
    info.pstate         = pstate
    info.clockOffsetMHz = offset_mhz
    return info


def _try_nvml_fn(name: str):
    """Return a ctypes-callable for an NVML function, or None if not found."""
    lib = _nvml_cdll()
    try:
        return getattr(lib, name)
    except AttributeError:
        return None


def get_clock_offsets(gpu_index: int = 0) -> dict:
    """Return GPC and memory clock offsets (MHz).

    Keys: gpc_offset_mhz, mem_offset_mhz (both int or None on failure).
    Calls nvmlDeviceGetClockOffsets once per clock domain (GRAPHICS, MEM).
    """
    out = {"gpc_offset_mhz": None, "mem_offset_mhz": None}
    if not _NVML_AVAILABLE:
        return out
    try:
        handle = _get_handle(gpu_index)

        # Try pynvml wrapper first (nvidia-ml-py ≥ 12 exposes it correctly).
        # Fall back to ctypes-direct if pynvml doesn't have it.
        _pynvml_get = getattr(pynvml, "nvmlDeviceGetClockOffsets", None)
        fn_get = _try_nvml_fn("nvmlDeviceGetClockOffsets") if _pynvml_get is None else None

        used_new_api = False
        for clock_type, key in ((_NVML_CLOCK_GRAPHICS, "gpc_offset_mhz"),
                                 (_NVML_CLOCK_MEM,      "mem_offset_mhz")):
            info = _make_clock_offset(clock_type, pstate=0)
            try:
                if _pynvml_get is not None:
                    rc = _pynvml_get(handle, ctypes.byref(info))
                elif fn_get is not None:
                    rc = fn_get(handle, ctypes.byref(info))
                else:
                    break
                if rc == 0:
                    out[key] = int(info.clockOffsetMHz)
                    used_new_api = True
                else:
                    log.debug("nvmlDeviceGetClockOffsets(type=%d) returned %d", clock_type, rc)
            except Exception as exc:
                log.debug("nvmlDeviceGetClockOffsets(type=%d): %s", clock_type, exc)

        if used_new_api:
            return out

        # Deprecated per-domain fallback.
        if hasattr(pynvml, "nvmlDeviceGetGpcClkVfOffset"):
            try:
                out["gpc_offset_mhz"] = int(pynvml.nvmlDeviceGetGpcClkVfOffset(handle))
            except Exception as exc:
                log.debug("nvmlDeviceGetGpcClkVfOffset: %s", exc)
        if hasattr(pynvml, "nvmlDeviceGetMemClkVfOffset"):
            try:
                res = pynvml.nvmlDeviceGetMemClkVfOffset(handle)
                out["mem_offset_mhz"] = int(res[0] if isinstance(res, (list, tuple)) else res)
            except Exception as exc:
                log.debug("nvmlDeviceGetMemClkVfOffset: %s", exc)

    except Exception as exc:
        log.warning("get_clock_offsets: %s", exc)
    return out


def set_clock_offsets(
    gpc_offset_mhz: Optional[int] = None,
    mem_offset_mhz: Optional[int] = None,
    gpu_index: int = 0,
) -> tuple[bool, str]:
    """Set clock offsets (MHz) for the specified domains only.

    Pass None for a domain to leave it untouched on hardware.
    Calls nvmlDeviceSetClockOffsets once per requested domain (GRAPHICS, MEM).
    Falls back to deprecated per-domain functions when the new API returns an
    error (e.g. NVML_ERROR_DEPRECATED=25 on Blackwell with driver 590.x).
    """
    if gpc_offset_mhz is None and mem_offset_mhz is None:
        return True, "OK"
    if not _NVML_AVAILABLE:
        return False, "NVML not available (install nvidia-ml-py)"
    try:
        handle = _get_handle(gpu_index)

        domains = []
        if gpc_offset_mhz is not None:
            domains.append((_NVML_CLOCK_GRAPHICS, gpc_offset_mhz))
        if mem_offset_mhz is not None:
            domains.append((_NVML_CLOCK_MEM, mem_offset_mhz))

        _pynvml_set = getattr(pynvml, "nvmlDeviceSetClockOffsets", None)
        fn_set = _try_nvml_fn("nvmlDeviceSetClockOffsets") if _pynvml_set is None else None

        if _pynvml_set is not None or fn_set is not None:
            all_ok = True
            for clock_type, offset in domains:
                info = _make_clock_offset(clock_type, pstate=0, offset_mhz=offset)
                try:
                    rc = _pynvml_set(handle, ctypes.byref(info)) if _pynvml_set else fn_set(handle, ctypes.byref(info))
                    if rc != 0:
                        log.debug("nvmlDeviceSetClockOffsets(type=%d) returned %d — trying fallback", clock_type, rc)
                        all_ok = False
                        break
                except Exception as exc:
                    log.debug("nvmlDeviceSetClockOffsets(type=%d): %s — trying fallback", clock_type, exc)
                    all_ok = False
                    break
            if all_ok:
                return True, "OK"
            # Non-zero rc (e.g. 25=DEPRECATED on Blackwell) — fall through to deprecated path.

        # Deprecated per-domain fallback (works on Blackwell/driver 590.x).
        errs = []
        if gpc_offset_mhz is not None and hasattr(pynvml, "nvmlDeviceSetGpcClkVfOffset"):
            try:
                pynvml.nvmlDeviceSetGpcClkVfOffset(handle, gpc_offset_mhz)
            except Exception as exc:
                errs.append(f"GPC: {exc}")
        if mem_offset_mhz is not None and hasattr(pynvml, "nvmlDeviceSetMemClkVfOffset"):
            try:
                pynvml.nvmlDeviceSetMemClkVfOffset(handle, mem_offset_mhz)
            except Exception as exc:
                errs.append(f"MEM: {exc}")
        if errs:
            return False, "; ".join(errs)
        return True, "OK"

    except Exception as exc:
        log.warning("set_clock_offsets: %s", exc)
        return False, str(exc)


# ── Range queries ─────────────────────────────────────────────────────────────

def get_mem_offset_range(gpu_index: int = 0) -> dict:
    """Return the min/max allowed memory clock offset (MHz).

    Keys: min_mem_offset_mhz, max_mem_offset_mhz.
    Uses nvmlDeviceGetMemClkMinMaxVfOffset; falls back to observed RTX values.
    """
    # Observed RTX 5090 defaults (NvAPI GetClockBoostRanges says -1000/+3000).
    out = {"min_mem_offset_mhz": -1000, "max_mem_offset_mhz": 3000}
    if not _NVML_AVAILABLE:
        return out
    try:
        handle = _get_handle(gpu_index)

        if hasattr(pynvml, "nvmlDeviceGetMemClkMinMaxVfOffset"):
            result = pynvml.nvmlDeviceGetMemClkMinMaxVfOffset(handle)
            if isinstance(result, (list, tuple)) and len(result) >= 2:
                out["min_mem_offset_mhz"] = int(result[0])
                out["max_mem_offset_mhz"] = int(result[1])
            else:
                min_v = getattr(result, "minOffset", None)
                max_v = getattr(result, "maxOffset", None)
                if min_v is not None:
                    out["min_mem_offset_mhz"] = int(min_v)
                if max_v is not None:
                    out["max_mem_offset_mhz"] = int(max_v)
            return out

        fn = _try_nvml_fn("nvmlDeviceGetMemClkMinMaxVfOffset")
        if fn is not None:
            min_v = ctypes.c_int(0)
            max_v = ctypes.c_int(0)
            rc = fn(handle, ctypes.byref(min_v), ctypes.byref(max_v))
            if rc == 0:
                out["min_mem_offset_mhz"] = int(min_v.value)
                out["max_mem_offset_mhz"] = int(max_v.value)

    except Exception as exc:
        log.debug("get_mem_offset_range: %s", exc)
    return out


# ── Memory locked clocks (VRAM max-frequency lock) ────────────────────────────
#
# This is a *different* mechanism from the VF-curve mem offset above. The
# offset (set_clock_offsets) nudges every point of the existing V/F curve by a
# delta and still lets the driver pick a P-state/clock dynamically. This one —
# mirroring nvidia_oc's `--min-mem-clock/--max-mem-clock` (which calls NVML's
# `device.set_mem_locked_clocks`) — pins the memory clock to a fixed [min, max]
# MHz window via nvmlDeviceSetMemoryLockedClocks, overriding P-state-driven
# down-clocking entirely. Passing min == max (e.g. the highest supported clock
# from get_max_mem_clock) is what gives "VRAM always at max frequency" behavior.
# nvmlDeviceResetMemoryLockedClocks hands control back to the driver.
#
# Confirmed model (from LACT's nvidia backend, a comparable NVML-based tool,
# and github.com/ilya-zlobintsev/LACT issue #486): a locked clock is a hard
# ceiling on the achieved frequency — combining it with a positive
# mem_offset_mhz does NOT raise the achieved clock past the lock's max. What
# the offset changes is the *voltage* the driver picks to reach that same
# ceiling (this is the actual undervolt trick: same clock, lower voltage).
# To get a higher achieved clock than a lock's max, don't lock at all and use
# the offset alone against the natural boost algorithm.
# LACT also always applies the lock *before* the offset (and undoes them in
# reverse order) — callers here (profiles/apply.py, cli.py profile apply,
# server.py _apply_profile) follow the same order; setting the offset first
# and locking afterward risks the lock call resetting it.

def get_supported_mem_clocks(gpu_index: int = 0) -> list[int]:
    """Return the memory clocks (MHz) the driver reports as supported, ascending.

    Uses nvmlDeviceGetSupportedMemoryClocks. Empty list on failure/unavailable.
    """
    if not _NVML_AVAILABLE:
        return []
    try:
        handle = _get_handle(gpu_index)
        clocks = pynvml.nvmlDeviceGetSupportedMemoryClocks(handle)
        return sorted(int(c) for c in clocks)
    except Exception as exc:
        log.debug("get_supported_mem_clocks: %s", exc)
        return []


def get_max_mem_clock(gpu_index: int = 0) -> Optional[int]:
    """Return the highest memory clock (MHz) the driver reports as supported.

    None if the list can't be read (e.g. NVML unavailable or unsupported GPU).
    """
    clocks = get_supported_mem_clocks(gpu_index)
    return clocks[-1] if clocks else None


def get_current_mem_clock(gpu_index: int = 0) -> Optional[int]:
    """Return the memory clock (MHz) NVML currently reports as running.

    Useful right after set_mem_locked_clocks: NVML silently snaps a
    requested lock value to the nearest entry in get_supported_mem_clocks
    (the GPU's stock/VBIOS clock table) rather than erroring, so the actual
    resulting clock can differ from what was requested — this lets callers
    report the real outcome instead of assuming the request was honored.
    """
    if not _NVML_AVAILABLE:
        return None
    try:
        handle = _get_handle(gpu_index)
        return int(pynvml.nvmlDeviceGetClockInfo(handle, pynvml.NVML_CLOCK_MEM))
    except Exception as exc:
        log.debug("get_current_mem_clock: %s", exc)
        return None


def set_mem_locked_clocks(min_mhz: int, max_mhz: int, gpu_index: int = 0) -> tuple[bool, str]:
    """Lock the memory clock to the [min_mhz, max_mhz] MHz window.

    Uses nvmlDeviceSetMemoryLockedClocks (the same NVML call nvidia_oc's
    `--min-mem-clock/--max-mem-clock` drives). Pass min_mhz == max_mhz to pin
    the clock to a single fixed frequency.
    """
    if not _NVML_AVAILABLE:
        return False, "NVML not available (install nvidia-ml-py)"
    try:
        handle = _get_handle(gpu_index)

        fn = getattr(pynvml, "nvmlDeviceSetMemoryLockedClocks", None)
        if fn is not None:
            fn(handle, int(min_mhz), int(max_mhz))
            return True, "OK"

        fn = _try_nvml_fn("nvmlDeviceSetMemoryLockedClocks")
        if fn is None:
            return False, "nvmlDeviceSetMemoryLockedClocks not found in libnvidia-ml"
        rc = fn(handle, ctypes.c_uint(int(min_mhz)), ctypes.c_uint(int(max_mhz)))
        if rc != 0:
            return False, f"NVML error code: {rc}"
        return True, "OK"
    except Exception as exc:
        log.warning("set_mem_locked_clocks: %s", exc)
        return False, str(exc)


def reset_mem_locked_clocks(gpu_index: int = 0) -> tuple[bool, str]:
    """Undo set_mem_locked_clocks, returning the memory clock to driver/P-state control.

    Uses nvmlDeviceResetMemoryLockedClocks.
    """
    if not _NVML_AVAILABLE:
        return False, "NVML not available (install nvidia-ml-py)"
    try:
        handle = _get_handle(gpu_index)

        fn = getattr(pynvml, "nvmlDeviceResetMemoryLockedClocks", None)
        if fn is not None:
            fn(handle)
            return True, "OK"

        fn = _try_nvml_fn("nvmlDeviceResetMemoryLockedClocks")
        if fn is None:
            return False, "nvmlDeviceResetMemoryLockedClocks not found in libnvidia-ml"
        rc = fn(handle)
        if rc != 0:
            return False, f"NVML error code: {rc}"
        return True, "OK"
    except Exception as exc:
        log.warning("reset_mem_locked_clocks: %s", exc)
        return False, str(exc)


