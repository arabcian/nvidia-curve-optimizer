#!/usr/bin/env python3
"""
nvcurve-gui root_helper.py
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Standalone privileged helper for the nvcurve GUI. Reads a single JSON
payload from stdin, dispatches it to an allowlisted op, prints the
JSON result to stdout, and exits.

This is a deliberately trimmed-down descendant of m16R1-power-manager's
root_helper.py: it carries ONLY the ops needed to drive `nvcurve`
(read/apply/reset the V/F curve, VRAM memlock, profile save/default/
delete). There is no nvctgp, no gaming-tunable, no CPU-isolation code
path here at all — this file cannot be used to touch anything outside
the nvcurve profile directory and the nvcurve CLI itself.

Invoked as: pkexec /path/to/root_helper.py   (JSON payload on stdin)
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path as _Path

# Only ever add this process's own install directory to sys.path/PYTHONPATH
# — never anything derived from the invoking user's environment. This file
# and the nvcurve/ package are installed side by side, both root-owned.
_HELPER_DIR = str(_Path(__file__).resolve().parent)
if _HELPER_DIR not in sys.path:
    sys.path.insert(0, _HELPER_DIR)

# The ONLY value ever used as the nvcurve PYTHONPATH/cwd below. A client-
# supplied "project_dir" is intentionally never honored — see op functions.
NVCURVE_PROJECT_DIR = _HELPER_DIR

# ─── Fixed, FHS-friendly paths ──────────────────────────────────────────
NVCURVE_PROFILES_DIR = "/etc/nvcurve/profiles"
NVCURVE_CONFIG_PATH = "/etc/nvcurve/config.json"

# Result files are written here (root-owned tmpfs dir) instead of /tmp to
# avoid symlink/TOCTOU attacks on a predictable world-writable path.
GPU_RESULT_DIR = "/run/nvcurve-gui"
GPU_READ_RESULT = "/run/nvcurve-gui/nvcurve_read.json"
GPU_APPLY_RESULT = "/run/nvcurve-gui/nvcurve_apply_result.json"
GPU_RESET_RESULT = "/run/nvcurve-gui/nvcurve_reset_result.json"

PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
MAX_PROFILE_BYTES = 256 * 1024  # 256 KB
MAX_STDIN_BYTES = 1024 * 1024  # 1 MB — generous headroom over MAX_PROFILE_BYTES

# ─── profile_data schema ────────────────────────────────────────────────
# The apply path used to accept ANY dict as profile_data — it was checked
# only for "is a dict" and "under 256 KB" — then written to a root-owned
# file under /etc and handed to `nvcurve profile apply`. Meanwhile
# op_set_vram_memlock carefully type- and range-checked the very same
# min/max values when they arrived as direct parameters. Same fields, same
# privilege, two different standards. This closes that gap on the way in.
_PROFILE_FIELDS = {
    "name": str,
    "gpu_name": str,
    "curve_deltas": dict,
    "mem_offset_mhz": int,
    "power_limit_w": int,
    "mem_locked_min_mhz": int,
    "mem_locked_max_mhz": int,
}

# Generous sanity bounds. These are NOT a substitute for the driver-range
# checks in nvcurve.safety.validate_limits (which run later, with the actual
# GPU's reported ranges) — they only reject values that are obviously not
# the quantity they claim to be.
_PROFILE_BOUNDS = {
    "mem_offset_mhz": (-100_000, 100_000),
    "power_limit_w": (1, 2_000),
    "mem_locked_min_mhz": (1, 100_000),
    "mem_locked_max_mhz": (1, 100_000),
}

_MAX_CURVE_POINTS = 512


def _validate_profile_data(data: dict) -> str:
    """Return an error string for an invalid profile_data dict, or "" if OK."""
    for key, value in data.items():
        if key not in _PROFILE_FIELDS:
            return f"unknown profile field: {key!r}"
        if value is None:
            continue
        expected = _PROFILE_FIELDS[key]
        # bool is a subclass of int — reject it explicitly for numeric fields.
        if expected is int and isinstance(value, bool):
            return f"field {key!r} must be an integer, not a boolean"
        if not isinstance(value, expected):
            return f"field {key!r} must be {expected.__name__}, got {type(value).__name__}"
        if key in _PROFILE_BOUNDS:
            lo, hi = _PROFILE_BOUNDS[key]
            if not (lo <= value <= hi):
                return f"field {key!r} value {value} outside plausible range ({lo}..{hi})"

    if isinstance(data.get("name"), str) and not PROFILE_NAME_RE.match(data["name"]):
        return "profile_data 'name' field has an invalid name"

    deltas = data.get("curve_deltas")
    if isinstance(deltas, dict):
        if len(deltas) > _MAX_CURVE_POINTS:
            return f"curve_deltas has too many entries (max {_MAX_CURVE_POINTS})"
        for point_key, delta in deltas.items():
            if not isinstance(point_key, str) or not point_key.lstrip("-").isdigit():
                return f"curve_deltas key {point_key!r} is not an integer string"
            if not (0 <= int(point_key) < _MAX_CURVE_POINTS):
                return f"curve_deltas point index {point_key} out of range"
            if isinstance(delta, bool) or not isinstance(delta, int):
                return f"curve_deltas value for point {point_key} must be an integer"
            if not (-2_000_000 <= delta <= 2_000_000):
                return f"curve_deltas delta for point {point_key} is implausible: {delta}"

    min_mhz = data.get("mem_locked_min_mhz")
    max_mhz = data.get("mem_locked_max_mhz")
    if isinstance(min_mhz, int) and isinstance(max_mhz, int) and min_mhz > max_mhz:
        return f"mem_locked_min_mhz ({min_mhz}) is greater than mem_locked_max_mhz ({max_mhz})"

    return ""


def _atomic_write_run_json(target_path: str, data) -> None:
    """Write `data` as JSON to `target_path` (which MUST live directly
    under GPU_RESULT_DIR) safely as root, then chmod 0644 so the
    unprivileged GUI can read it back.

    Hardening: containing dir is created root-owned 0755 up front; we
    write to a fresh temp file in that same dir with O_CREAT|O_EXCL|
    O_NOFOLLOW (so a pre-planted symlink/file can't redirect or hijack
    the write), then atomically rename() over the target.
    """
    if os.path.dirname(os.path.abspath(target_path)) != os.path.abspath(GPU_RESULT_DIR):
        raise ValueError("refusing to write result outside GPU_RESULT_DIR")

    os.makedirs(GPU_RESULT_DIR, exist_ok=True)
    os.chmod(GPU_RESULT_DIR, 0o755)

    tmp_path = target_path + ".tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    try:
        os.unlink(tmp_path)
    except FileNotFoundError:
        pass
    fd = os.open(tmp_path, flags, 0o644)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
        os.chmod(tmp_path, 0o644)
        os.replace(tmp_path, target_path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _ensure_profiles_dir() -> None:
    """Create /etc/nvcurve/profiles with an explicitly enforced 0755 mode.

    SECURITY: os.makedirs()'s mode argument is masked by the process umask,
    and pkexec — unlike sudo — does NOT reset the umask of the program it
    launches. It is inherited from the calling GUI, i.e. from the
    unprivileged user. So a user who runs `umask 000` before starting
    nvcurve-gui previously caused this root-owned directory under /etc to be
    created world-writable (0777), letting any local account drop profile
    JSON that root would later read and apply.

    The explicit chmod after creation is what actually guarantees the mode;
    passing mode= to makedirs alone is not sufficient for the same reason.
    """
    os.makedirs(NVCURVE_PROFILES_DIR, exist_ok=True)
    try:
        os.chmod(NVCURVE_PROFILES_DIR, 0o755)
    except OSError:
        pass


def _nvcurve_env():
    """Builds a minimal, explicit environment for the nvcurve subprocess
    instead of inheriting whatever pkexec passed through to us.

    SECURITY (defense in depth): pkexec already strips almost everything
    from the invoking user's environment before it runs us as root, but
    this process shouldn't rely on that alone. We build our own
    allowlist here so that even a pkexec/polkit misconfiguration, or a
    future change to what it forwards, can't smuggle something like
    LD_PRELOAD, PYTHONSTARTUP, or a stray pre-existing PYTHONPATH entry
    into a subprocess we spawn as root. Only PATH (needed to find
    `python3` itself and anything it shells out to) and a couple of
    harmless locale/display vars are kept; everything else is dropped
    and rebuilt from scratch.
    """
    safe_keys = ("PATH", "LANG", "LC_ALL")
    env = {k: os.environ[k] for k in safe_keys if k in os.environ}
    env.setdefault("PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin")
    # Our own install dir only — never anything derived from the caller.
    env["PYTHONPATH"] = NVCURVE_PROJECT_DIR
    # Never let a caller-controlled PYTHONSTARTUP/PYTHONNOUSERSITE/etc.
    # ride along even if it somehow ended up in os.environ.
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONNOUSERSITE"] = "1"
    return env


def _run_nvcurve(*args, timeout=30):
    """Runs `python3 -m nvcurve <args>` pinned to our own install dir."""
    return subprocess.run(
        [sys.executable, "-m", "nvcurve", *args],
        capture_output=True, text=True,
        env=_nvcurve_env(), cwd=NVCURVE_PROJECT_DIR, timeout=timeout,
    )


# ─── Ops ─────────────────────────────────────────────────────────────────

def op_read_gpu_curve(params: dict) -> dict:
    result = _run_nvcurve("read", "--json")
    if result.returncode != 0:
        return {"ok": False, "error": f"nvcurve read failed with code {result.returncode}: {result.stderr.strip()}"}
    try:
        data = json.loads(result.stdout)
        _atomic_write_run_json(GPU_READ_RESULT, data)
        return {"ok": True, "message": "Curve read successfully."}
    except Exception as e:
        return {"ok": False, "error": f"Failed to parse JSON: {e}"}


def op_apply_gpu_offsets(params: dict) -> dict:
    profile_name = params.get("profile_name")
    profile_data = params.get("profile_data")

    if not isinstance(profile_name, str) or not PROFILE_NAME_RE.match(profile_name):
        return {"ok": False, "error": "Invalid profile name"}
    if not isinstance(profile_data, dict):
        return {"ok": False, "error": "profile_data must be an object"}
    if len(json.dumps(profile_data)) > MAX_PROFILE_BYTES:
        return {"ok": False, "error": "profile_data too large"}
    schema_err = _validate_profile_data(profile_data)
    if schema_err:
        return {"ok": False, "error": f"Invalid profile_data: {schema_err}"}

    # 1. Reset the curve first so the profile applies cleanly on top.
    reset_result = _run_nvcurve("write", "--range", "0-132", "--delta", "0")
    if reset_result.returncode != 0:
        return {"ok": False, "error": f"Reset failed: {reset_result.stderr.strip()}"}

    # 2. Persist the profile JSON and apply it.
    _ensure_profiles_dir()
    profile_path = os.path.join(NVCURVE_PROFILES_DIR, f"{profile_name}.json")
    if os.path.dirname(os.path.abspath(profile_path)) != os.path.abspath(NVCURVE_PROFILES_DIR):
        return {"ok": False, "error": "Path traversal detected"}
    with open(profile_path, "w") as f:
        json.dump(profile_data, f, indent=2)
    os.chmod(profile_path, 0o644)

    result = _run_nvcurve("profile", "apply", profile_name)
    if result.returncode != 0:
        return {"ok": False, "error": result.stderr.strip() if result.stderr else "Apply failed"}

    # 3. Read the curve back so the GUI can refresh from ground truth.
    result2 = _run_nvcurve("read", "--json")
    if result2.returncode == 0:
        try:
            data = json.loads(result2.stdout)
            _atomic_write_run_json(GPU_APPLY_RESULT, data)
        except Exception:
            pass

    message = f"Profile '{profile_name}' applied successfully."
    if result.stderr and result.stderr.strip():
        message += "\n" + result.stderr.strip()
    return {"ok": True, "message": message}


def op_apply_named_profile(params: dict) -> dict:
    """Apply an already-saved profile by name (no writing of new JSON).
    Used by the tray quick-apply menu and the profile combo."""
    name = params.get("name")
    if not isinstance(name, str) or not PROFILE_NAME_RE.match(name):
        return {"ok": False, "error": "Invalid profile name"}

    profile_path = os.path.join(NVCURVE_PROFILES_DIR, f"{name}.json")
    if os.path.dirname(os.path.abspath(profile_path)) != os.path.abspath(NVCURVE_PROFILES_DIR):
        return {"ok": False, "error": "Path traversal detected"}
    if not os.path.isfile(profile_path):
        return {"ok": False, "error": f"Profile not found: {name}"}

    # Reset first so the profile applies cleanly on top of a known baseline.
    reset_result = _run_nvcurve("write", "--range", "0-132", "--delta", "0")
    if reset_result.returncode != 0:
        return {"ok": False, "error": f"Reset failed: {reset_result.stderr.strip()}"}

    result = _run_nvcurve("profile", "apply", name)
    if result.returncode != 0:
        return {"ok": False, "error": result.stderr.strip() if result.stderr else "Apply failed"}

    result2 = _run_nvcurve("read", "--json")
    if result2.returncode == 0:
        try:
            data = json.loads(result2.stdout)
            _atomic_write_run_json(GPU_APPLY_RESULT, data)
        except Exception:
            pass

    message = f"Profile '{name}' applied successfully."
    if result.stderr and result.stderr.strip():
        message += "\n" + result.stderr.strip()
    return {"ok": True, "message": message}


def op_reset_gpu_curve(params: dict) -> dict:
    reset_proc = _run_nvcurve("write", "--range", "0-132", "--delta", "0")
    if reset_proc.returncode != 0:
        return {"ok": False, "error": f"Reset write failed: {reset_proc.stderr.strip() if reset_proc.stderr else reset_proc.returncode}"}

    result = _run_nvcurve("read", "--json")
    if result.returncode == 0:
        try:
            data = json.loads(result.stdout)
            _atomic_write_run_json(GPU_RESET_RESULT, data)
        except Exception:
            pass

    return {"ok": True, "message": "Reset successful."}


def op_set_vram_memlock(params: dict) -> dict:
    min_mhz = params.get("min_mhz")
    max_mhz = params.get("max_mhz")

    if not isinstance(min_mhz, int) or not isinstance(max_mhz, int):
        return {"ok": False, "error": "min_mhz/max_mhz must be integers"}
    if min_mhz <= 0 or max_mhz <= 0 or min_mhz > max_mhz:
        return {"ok": False, "error": "Invalid min_mhz/max_mhz range"}

    result = _run_nvcurve("memlock", "set", "--min", str(min_mhz), "--max", str(max_mhz))
    if result.returncode != 0:
        return {"ok": False, "error": result.stderr.strip() or "nvcurve memlock set failed"}
    return {"ok": True, "message": result.stdout.strip() or f"VRAM locked to {min_mhz}-{max_mhz} MHz."}


def op_reset_vram_memlock(params: dict) -> dict:
    result = _run_nvcurve("memlock", "reset")
    if result.returncode != 0:
        return {"ok": False, "error": result.stderr.strip() or "nvcurve memlock reset failed"}
    return {"ok": True, "message": result.stdout.strip() or "VRAM clock unlocked."}


def op_write_nvcurve_profile(params: dict) -> dict:
    name = params.get("name")
    content = params.get("content")

    if not isinstance(name, str) or not PROFILE_NAME_RE.match(name):
        return {"ok": False, "error": "Invalid profile name"}
    if not isinstance(content, str):
        return {"ok": False, "error": "content must be a string"}
    if len(content.encode()) > MAX_PROFILE_BYTES:
        return {"ok": False, "error": "content too large"}
    try:
        json.loads(content)
    except json.JSONDecodeError as e:
        return {"ok": False, "error": f"content is not valid JSON: {e}"}

    _ensure_profiles_dir()
    target_path = os.path.join(NVCURVE_PROFILES_DIR, f"{name}.json")
    if os.path.dirname(os.path.abspath(target_path)) != os.path.abspath(NVCURVE_PROFILES_DIR):
        return {"ok": False, "error": "Path traversal detected"}

    with open(target_path, "w") as f:
        f.write(content)
    os.chmod(target_path, 0o644)
    return {"ok": True, "message": f"Profile written: {target_path}"}


def op_delete_nvcurve_profile(params: dict) -> dict:
    name = params.get("name")
    if not isinstance(name, str) or not PROFILE_NAME_RE.match(name):
        return {"ok": False, "error": "Invalid profile name"}

    target_path = os.path.join(NVCURVE_PROFILES_DIR, f"{name}.json")
    if os.path.dirname(os.path.abspath(target_path)) != os.path.abspath(NVCURVE_PROFILES_DIR):
        return {"ok": False, "error": "Path traversal detected"}
    if not os.path.isfile(target_path):
        return {"ok": False, "error": f"Profile not found: {name}"}

    try:
        os.remove(target_path)
    except OSError as e:
        return {"ok": False, "error": f"Could not delete profile: {e}"}

    cleared_default = False
    try:
        if os.path.isfile(NVCURVE_CONFIG_PATH):
            with open(NVCURVE_CONFIG_PATH) as f:
                cfg = json.load(f)
            profiles = cfg.get("auto_load_profiles", {})
            if name in profiles.values():
                _run_nvcurve("profile", "default", "--clear")
                try:
                    os.chmod(NVCURVE_CONFIG_PATH, 0o644)
                except OSError:
                    pass
                cleared_default = True
    except Exception:
        # Best-effort only: profile deletion above already succeeded.
        pass

    msg = f"Profile '{name}' deleted."
    if cleared_default:
        msg += " (it was the default profile — default cleared too.)"
    return {"ok": True, "message": msg}


def op_set_default_gpu_profile(params: dict) -> dict:
    name = params.get("name")
    clear = bool(params.get("clear", False))

    if not clear:
        if not isinstance(name, str) or not PROFILE_NAME_RE.match(name):
            return {"ok": False, "error": "Invalid profile name"}
        profile_path = os.path.join(NVCURVE_PROFILES_DIR, f"{name}.json")
        if not os.path.isfile(profile_path):
            return {"ok": False, "error": f"Profile not found: {name}"}

    try:
        os.makedirs("/etc/nvcurve", exist_ok=True)
        os.chmod("/etc/nvcurve", 0o755)
    except OSError as e:
        return {"ok": False, "error": f"Could not prepare /etc/nvcurve: {e}"}

    args = ["profile", "default"]
    args += ["--clear"] if clear else [name]
    result = _run_nvcurve(*args)
    if result.returncode != 0:
        err = (result.stderr or "").strip()
        return {"ok": False, "error": err or "nvcurve profile default failed"}

    try:
        os.chmod(NVCURVE_CONFIG_PATH, 0o644)
    except OSError as e:
        return {"ok": False, "error": f"Set, but could not make config.json readable: {e}"}

    out = (result.stdout or "").strip()
    if clear:
        msg = out or "Default GPU profile cleared."
    else:
        msg = out or f"Default GPU profile set to '{name}'."
    return {"ok": True, "message": msg}


def op_run_gpu_autoload(params: dict) -> dict:
    result = _run_nvcurve("autoload")
    combined = ((result.stdout or "") + (result.stderr or "")).strip()
    if result.returncode != 0:
        return {"ok": False, "error": combined or f"autoload failed (code {result.returncode})"}
    return {"ok": True, "message": combined or "No default GPU profile configured."}


# ─── Allowlist ───────────────────────────────────────────────────────────
OPERATIONS = {
    "read_gpu_curve": op_read_gpu_curve,
    "apply_gpu_offsets": op_apply_gpu_offsets,
    "apply_named_profile": op_apply_named_profile,
    "reset_gpu_curve": op_reset_gpu_curve,
    "set_vram_memlock": op_set_vram_memlock,
    "reset_vram_memlock": op_reset_vram_memlock,
    "write_nvcurve_profile": op_write_nvcurve_profile,
    "delete_nvcurve_profile": op_delete_nvcurve_profile,
    "set_default_gpu_profile": op_set_default_gpu_profile,
    "run_gpu_autoload": op_run_gpu_autoload,
}


def main() -> int:
    if os.geteuid() != 0:
        print(json.dumps({"ok": False, "error": "root_helper must run as root"}))
        return 1

    # pkexec does not reset the umask — it is inherited straight from the
    # unprivileged caller. Every file and directory this process creates as
    # root would otherwise have permissions chosen by the user who invoked
    # it. Pin it here, once, so no individual op has to remember.
    os.umask(0o022)

    raw = sys.stdin.read(MAX_STDIN_BYTES + 1)
    if len(raw.encode()) > MAX_STDIN_BYTES:
        print(json.dumps({"ok": False, "error": "stdin payload too large"}))
        return 1
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        print(json.dumps({"ok": False, "error": f"Invalid JSON on stdin: {e}"}))
        return 1
    if not isinstance(payload, dict):
        print(json.dumps({"ok": False, "error": "Payload must be a JSON object"}))
        return 1

    op_name = payload.get("op")
    handler = OPERATIONS.get(op_name)
    if handler is None:
        print(json.dumps({"ok": False, "error": f"Unknown or disallowed op: {op_name!r}"}))
        return 1

    try:
        result = handler(payload)
    except subprocess.TimeoutExpired:
        result = {"ok": False, "error": "nvcurve call timed out"}
    except Exception as e:
        result = {"ok": False, "error": f"Internal error: {e}"}

    print(json.dumps(result))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
