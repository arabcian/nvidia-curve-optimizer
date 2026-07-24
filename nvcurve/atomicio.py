"""Atomic, mode-explicit file writes for root-owned state.

Every JSON file this project writes as root — /etc/nvcurve/config.json,
/etc/nvcurve/profiles/*.json — is read back later by a root process that
acts on its contents (the boot-time daemon applies auto_load_profiles
before the user is even logged in). A plain ``open(path, "w")`` truncates
the target first and only then writes: a crash, an OOM kill, or a power
loss in that window leaves a zero-length or half-written file that the
next boot silently treats as "no profile configured" (or fails to parse
and skips). Nothing in the codebase recovered from that.

write_json() closes the window: the payload is written to a fresh temp
file in the *same* directory (so rename() stays within one filesystem and
is therefore atomic), fsync'ed, chmod'ed explicitly, and only then
rename()d over the target. A reader either sees the complete old file or
the complete new one — never a partial write.

The explicit chmod matters independently: `sudo` and `pkexec` both pass
the invoking user's umask straight through, so mode= arguments alone let
an unprivileged caller's `umask 000` decide the permissions of a
root-owned file under /etc.
"""

import json
import os
import tempfile
from typing import Any

__all__ = ["write_json", "write_bytes"]


def _fsync_dir(directory: str) -> None:
    """Flush the directory entry itself so the rename survives a crash."""
    try:
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _atomic_replace(path: str, data: bytes, mode: int) -> None:
    directory = os.path.dirname(os.path.abspath(path)) or "."
    # mkstemp creates with O_EXCL and mode 0600 — no symlink can be
    # pre-planted at the temp name, and the file is never briefly readable
    # at a wider mode than we intend.
    fd, tmp_path = tempfile.mkstemp(prefix=".nvcurve-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp_path, mode)
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    _fsync_dir(directory)


def write_json(path: str, payload: Any, mode: int = 0o644, indent: int | None = 2) -> None:
    """Serialise `payload` as JSON and atomically replace `path` with it."""
    data = json.dumps(payload, indent=indent).encode("utf-8")
    _atomic_replace(path, data, mode)


def write_bytes(path: str, data: bytes, mode: int = 0o644) -> None:
    """Atomically replace `path` with `data`."""
    _atomic_replace(path, data, mode)
