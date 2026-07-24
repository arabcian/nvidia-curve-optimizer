"""Save and restore ClockBoostTable snapshots to/from disk."""

import ctypes
import json
import logging
import os
import struct
from datetime import datetime
from typing import Optional

from ..nvapi.bootstrap import nvcall_raw
from ..nvapi.constants import FUNC, CT_SIZE, CT_BASE, CT_STRIDE, CT_DELTA_OFF
from ..nvapi.types import SnapshotInfo
from ..atomicio import write_bytes, write_json
from .vfcurve import read_clock_table_raw, get_boost_mask

log = logging.getLogger("nvcurve.hal.snapshot")


def save(
    gpu,
    gpu_name: str,
    snapshot_dir: str,
    max_snapshots: int = 0,
    raw: Optional[bytes] = None,
) -> Optional[str]:
    """Save the current ClockBoostTable to disk.

    Writes both a binary .bin file and a human-readable .json metadata file.
    If max_snapshots > 0, deletes the oldest snapshots to stay within the limit.
    Returns the binary filepath on success, or None on failure.

    Args:
        raw: a ClockBoostTable buffer the caller already fetched (e.g. via
            read_curve_with_raw_ct()). Passing it skips this function's own
            driver round-trip — the auto-snapshot that precedes every write
            used to re-read a table the write path had just read.

    This function is called from the API server and the daemon, not only
    from an interactive terminal, so progress goes to the logger rather
    than to stdout: printing from here corrupted any command whose stdout
    is parsed as JSON, and made the messages invisible to journald.
    """
    if raw is None:
        raw, err = read_clock_table_raw(gpu)
        if not raw:
            log.error("Failed to read ClockBoostTable: %s", err)
            return None

    # Explicit mode, not umask-derived — this runs as root and `sudo nvcurve`
    # inherits the caller's umask (see profiles/native.py:save_profile).
    os.makedirs(snapshot_dir, exist_ok=True)
    try:
        os.chmod(snapshot_dir, 0o755)
    except OSError:
        pass

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    bin_path = os.path.join(snapshot_dir, f"clock_boost_table_{ts}.bin")
    meta_path = os.path.join(snapshot_dir, f"clock_boost_table_{ts}.json")

    write_bytes(bin_path, bytes(raw), 0o644)

    offsets = []
    max_entries = (len(raw) - CT_BASE) // CT_STRIDE
    for i in range(max_entries):
        off = CT_BASE + i * CT_STRIDE + CT_DELTA_OFF
        delta = struct.unpack_from("<i", raw, off)[0]
        offsets.append(delta)

    meta = {
        "gpu": gpu_name,
        "timestamp": datetime.now().isoformat(),
        "file": bin_path,
        "size": len(raw),
        "offsets_kHz": offsets,
        "nonzero_offsets": sum(1 for o in offsets if o != 0),
    }
    write_json(meta_path, meta, 0o644)

    log.info(
        "Snapshot saved: %s (%d bytes, %d non-zero offsets)",
        bin_path, len(raw), meta["nonzero_offsets"],
    )

    if max_snapshots > 0:
        _prune_snapshots(snapshot_dir, max_snapshots)

    return bin_path


def _prune_snapshots(snapshot_dir: str, max_snapshots: int) -> None:
    """Delete oldest snapshots (both .bin and .json) to stay within max_snapshots."""
    bins = sorted(
        f for f in os.listdir(snapshot_dir) if f.endswith(".bin")
    )  # oldest first (lexicographic = chronological for our timestamp format)
    excess = len(bins) - max_snapshots
    # BUG (fixed): this used to be a bare `bins[:excess]`. When fewer
    # snapshots existed than the limit, `excess` is NEGATIVE and
    # `bins[:-n]` is not an empty slice — it is "everything but the last n".
    # With the default max_snapshots=20, the 11th snapshot silently deleted
    # the 2 oldest, the 12th deleted 4, and so on: the pruner destroyed
    # snapshots the user was still under the limit for, and it did so on
    # every single curve write because auto_snapshot is on by default.
    if excess <= 0:
        return
    for fname in bins[:excess]:
        stem = fname[:-4]  # strip .bin
        for ext in (".bin", ".json"):
            try:
                os.remove(os.path.join(snapshot_dir, stem + ext))
            except OSError:
                pass


def restore(gpu, snapshot_dir: str, filepath: str = None) -> bool:
    """Restore a ClockBoostTable snapshot from disk.

    If no filepath is given, uses the most recent snapshot in snapshot_dir.
    Returns True on success.

    Security: when a caller-supplied filepath is used (e.g. from the REST
    API's /api/snapshot/restore), it is required to resolve inside
    snapshot_dir. Without this check, this function is a straight
    "read any file the process can see and push its bytes to the GPU via
    SetClockBoostTable" primitive that completely bypasses safety.validate_write
    and its max_delta_khz limit — the size/version-word check below is not
    sufficient on its own, since an attacker only needs to produce a buffer of
    the right size and version word to have its contents accepted.
    """
    if filepath is None:
        if not os.path.isdir(snapshot_dir):
            log.error("No snapshots found in %s", snapshot_dir)
            return False
        bins = sorted(
            [f for f in os.listdir(snapshot_dir) if f.endswith(".bin")],
            reverse=True,
        )
        if not bins:
            log.error("No snapshot .bin files in %s", snapshot_dir)
            return False
        filepath = os.path.join(snapshot_dir, bins[0])
    else:
        # Caller-supplied path — must stay inside snapshot_dir. Resolve both
        # sides through realpath so symlinks can't be used to point outside
        # the allowed directory either.
        real_dir = os.path.realpath(snapshot_dir)
        candidate = filepath if os.path.isabs(filepath) else os.path.join(snapshot_dir, filepath)
        real_path = os.path.realpath(candidate)
        if os.path.commonpath([real_dir, real_path]) != real_dir:
            log.error("Refusing to restore from outside snapshot_dir: %s", filepath)
            return False
        filepath = real_path

    if not os.path.isfile(filepath):
        log.error("Snapshot file not found: %s", filepath)
        return False

    # Bounded read: the only valid snapshot is exactly CT_SIZE bytes, so
    # never pull an arbitrary-length file into memory just to reject it.
    with open(filepath, "rb") as f:
        raw = f.read(CT_SIZE + 1)

    if len(raw) != CT_SIZE:
        log.error("Snapshot size mismatch: expected %d, got %d", CT_SIZE, len(raw))
        return False

    vw = struct.unpack_from("<I", raw, 0)[0]
    expected_vw = (1 << 16) | CT_SIZE
    if vw != expected_vw:
        log.error("Version word mismatch: 0x%08X (expected 0x%08X)", vw, expected_vw)
        return False

    buf = ctypes.create_string_buffer(CT_SIZE)
    ctypes.memmove(buf, raw, CT_SIZE)

    # Full mask for restore — write all points
    mask, _ = get_boost_mask(gpu)
    if mask:
        for i in range(32):
            buf[4 + i] = mask[i]

    log.info("Restoring ClockBoostTable from %s", filepath)
    ret, desc = nvcall_raw(FUNC["SetClockBoostTable"], gpu, buf)
    if ret != 0:
        log.error("SetClockBoostTable returned %d (%s)", ret, desc)
    return ret == 0


def list_snapshots(snapshot_dir: str) -> list[SnapshotInfo]:
    """Return metadata for all snapshots in snapshot_dir, newest first."""
    if not os.path.isdir(snapshot_dir):
        return []

    results = []
    for fname in sorted(os.listdir(snapshot_dir), reverse=True):
        if not fname.endswith(".json"):
            continue
        meta_path = os.path.join(snapshot_dir, fname)
        try:
            with open(meta_path) as f:
                meta = json.load(f)
            bin_path = meta.get("file", meta_path.replace(".json", ".bin"))
            results.append(SnapshotInfo(
                filepath=bin_path,
                timestamp=meta.get("timestamp", ""),
                gpu=meta.get("gpu", ""),
                nonzero_offsets=meta.get("nonzero_offsets", 0),
                size=meta.get("size", 0),
            ))
        except (json.JSONDecodeError, KeyError):
            continue

    return results
