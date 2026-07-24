"""Native profile storage and schema."""

import json
import logging
import os
import glob
from dataclasses import dataclass, asdict
from typing import Dict, Optional, List

from ..atomicio import write_json

log = logging.getLogger("nvcurve.profiles.native")

# ── Profile name policy ──────────────────────────────────────────────────
#
# This filter used to be copy-pasted as an inline generator expression in
# six places (save/rename/delete here, apply_profile and apply_with_retry
# in profiles/apply.py, and twice inside server.py), and root_helper.py
# enforced a *different*, stricter rule (^[A-Za-z0-9_-]{1,64}$). The two
# policies disagreed on spaces and parentheses, so a profile the server
# was perfectly happy to create — "Silent (UV)" — could not be applied
# through the GUI's privileged helper at all. One definition, one policy.
#
# The character set is deliberately unchanged from what shipped, so
# existing profile files on disk keep resolving to the same filename.
# What is new is the length cap and the explicit empty-result contract:
# a name of "..." previously collapsed to "" and produced a hidden
# /etc/nvcurve/profiles/.json that no listing would ever show again.
PROFILE_NAME_MAX_LEN = 64
_PROFILE_NAME_EXTRA = " _-()"


def safe_profile_name(name) -> str:
    """Return the canonical on-disk form of a profile name, or "" if unusable.

    An empty return value means the caller must refuse the operation — it
    must never be turned into a filename.
    """
    if not isinstance(name, str):
        return ""
    cleaned = "".join(
        c for c in name if c.isalnum() or c in _PROFILE_NAME_EXTRA
    ).strip()
    return cleaned[:PROFILE_NAME_MAX_LEN].strip()


def profile_path(profile_dir: str, name) -> Optional[str]:
    """Resolve a profile name to its file path, or None if the name is unusable."""
    safe = safe_profile_name(name)
    if not safe:
        return None
    return os.path.join(profile_dir, f"{safe}.json")


@dataclass
class ProfileData:
    name: str
    curve_deltas: Dict[str, int]  # { "index": delta_khz }
    gpu_name: str = ""  # optional; older profiles predate this field
    mem_offset_mhz: Optional[int] = None
    power_limit_w: Optional[int] = None
    mem_locked_min_mhz: Optional[int] = None
    mem_locked_max_mhz: Optional[int] = None


def save_profile(profile_dir: str, data: ProfileData) -> str:
    """Save profile to JSON, sanitising the filename.

    Modes are set explicitly rather than left to the umask: this runs as
    root from the CLI/daemon, and `sudo nvcurve ...` inherits the invoking
    user's umask, so a permissive umask would otherwise decide the
    permissions of a root-owned directory and file under /etc.
    """
    os.makedirs(profile_dir, exist_ok=True)
    try:
        os.chmod(profile_dir, 0o755)
    except OSError:
        pass

    safe_name = safe_profile_name(data.name) or "Unnamed"

    filepath = os.path.join(profile_dir, f"{safe_name}.json")
    payload = asdict(data)
    # Store the canonical name in the file too. Previously the filename was
    # sanitised but the `name` field kept the raw string, so list_profiles()
    # handed clients a name that did not round-trip: apply/delete re-derived
    # a *different* filename from it and reported "profile not found" for a
    # profile that plainly existed.
    payload["name"] = safe_name
    write_json(filepath, payload, 0o644)
    return filepath


_MAX_PROFILE_BYTES = 256 * 1024


def load_profile(filepath: str) -> ProfileData:
    """Load profile from JSON.

    The read is bounded: these files live under /etc and are parsed by a
    root process at boot, so a truncated or deliberately oversized file
    should fail fast rather than be pulled into memory in full.
    """
    with open(filepath, "r", encoding="utf-8") as f:
        text = f.read(_MAX_PROFILE_BYTES + 1)
    if len(text.encode("utf-8")) > _MAX_PROFILE_BYTES:
        raise ValueError(f"profile {filepath} exceeds {_MAX_PROFILE_BYTES} bytes")
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError(f"profile {filepath} is not a JSON object")
    # Migrate old field names.
    if "vram_p0_offset_mhz" in data and "mem_offset_mhz" not in data:
        data["mem_offset_mhz"] = data.pop("vram_p0_offset_mhz")
    # Drop removed fields so old profiles don't cause TypeError.
    for obsolete in ("gpu_locked_min_mhz", "gpu_locked_max_mhz", "vram_p0_offset_mhz"):
        data.pop(obsolete, None)
    # Ignore any field we don't know about rather than dying with a bare
    # TypeError from the dataclass constructor — a profile written by a
    # newer version must not make an older one unable to list *any*
    # profile in the directory.
    known = set(ProfileData.__dataclass_fields__)
    unknown = set(data) - known
    if unknown:
        log.warning("Profile %s has unknown field(s) %s — ignoring",
                    filepath, ", ".join(sorted(unknown)))
        for key in unknown:
            data.pop(key)
    return ProfileData(**data)


def list_profiles(profile_dir: str) -> List[ProfileData]:
    """Return a list of all safely readable profiles."""
    if not os.path.exists(profile_dir):
        return []
    profiles = []
    for fp in glob.glob(os.path.join(profile_dir, "*.json")):
        try:
            profiles.append(load_profile(fp))
        except Exception as e:
            # Keep going so one bad file doesn't hide every other profile,
            # but say so — silently swallowing this meant a corrupted or
            # tampered profile simply vanished from the list with no trace.
            log.warning("Skipping unreadable profile %s: %s", fp, e)
    # Sort alphabetically by name
    profiles.sort(key=lambda p: p.name.lower())
    return profiles


def rename_profile(profile_dir: str, old_name: str, new_name: str) -> bool:
    """Rename a profile: update the name field and move the file."""
    old_safe = safe_profile_name(old_name)
    new_safe = safe_profile_name(new_name)
    if not old_safe or not new_safe:
        return False
    old_path = os.path.join(profile_dir, f"{old_safe}.json")
    new_path = os.path.join(profile_dir, f"{new_safe}.json")
    if not os.path.exists(old_path):
        return False
    try:
        profile = load_profile(old_path)
        profile.name = new_safe
        payload = asdict(profile)
        write_json(new_path, payload, 0o644)
        if old_path != new_path:
            os.remove(old_path)
        return True
    except Exception as exc:
        log.warning("rename_profile(%r -> %r) failed: %s", old_name, new_name, exc)
        return False


def delete_profile(profile_dir: str, name: str) -> bool:
    """Delete a profile by name."""
    safe_name = safe_profile_name(name)
    if not safe_name:
        return False
    filepath = os.path.join(profile_dir, f"{safe_name}.json")
    if os.path.exists(filepath):
        try:
            os.remove(filepath)
            return True
        except Exception:
            return False
    return False
