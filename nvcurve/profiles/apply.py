"""Apply saved profiles to hardware, with optional read-back verification."""

import json
import logging
import os
import sys
import time

log = logging.getLogger("nvcurve.profiles.apply")

_PERSISTENT_CONFIG_FILE = "/etc/nvcurve/config.json"


def _gpu_stable_key(info) -> str:
    if info.uuid:
        return info.uuid
    if info.pci_bus_id is not None:
        return f"pci:{info.pci_bus_id:04x}"
    return f"idx:{info.index}"


def apply_profile(gpu_index: int, name: str, cfg) -> tuple[list[str], list[str]]:
    """Apply a named profile to the given GPU.

    Returns (critical_errors, warnings):
      - critical_errors: problems with the profile's curve deltas (the
        actual V/F curve write). Non-empty means the profile did not apply.
      - warnings: problems with secondary settings (power limit, memory
        offset, memory-locked clocks) — some driver/GPU combinations reject
        these (e.g. a memory-clock-offset domain that's locked/unsupported
        on this GPU) even though the curve itself writes fine. These are
        reported but don't fail the overall apply.

    This is the single implementation of the profile-apply sequence — it
    used to be duplicated (and had quietly drifted out of sync) across
    cli.py's "profile apply" command and server.py's own _apply_profile;
    both now call this function instead. If you're changing the apply
    order/logic, this is the only place to do it.
    """
    from .native import load_profile
    from ..hal.gpu import get_gpu
    from ..hal.limits import set_power_limit, set_mem_locked_clocks, set_clock_offsets
    from ..hal.vfcurve import write_offsets, reset_offsets
    from ..hal.snapshot import save as snapshot_save
    from ..safety import validate_write

    safe_name = "".join(c for c in name if c.isalnum() or c in " _-()").strip()
    filepath = os.path.join(cfg.profile_dir, f"{safe_name}.json")

    profile = load_profile(filepath)  # raises FileNotFoundError if missing
    gpu, gpu_name = get_gpu(index=gpu_index)

    critical_errs: list[str] = []
    warnings: list[str] = []

    # Order matters here: LACT's nvidia backend (a comparable NVML-based tool)
    # always sets locked clocks *before* the offset, and resets them in the
    # reverse order (offset first, then locked clocks) — treating the lock as
    # the outer layer and the offset as applied on top of it. Setting the
    # offset first and locking afterward risks the lock call resetting or
    # ignoring the previously-applied offset.
    if profile.mem_locked_max_mhz is not None:
        min_mhz = profile.mem_locked_min_mhz
        if min_mhz is None:
            min_mhz = profile.mem_locked_max_mhz
        ok, msg = set_mem_locked_clocks(min_mhz, profile.mem_locked_max_mhz, gpu_index)
        if not ok:
            warnings.append(f"Mem locked clocks: {msg}")

    # NOTE: was briefly routed through NvAPI's write_memory_offset. Combining
    # an NVML lock with an NvAPI offset lets the offset silently blow past
    # the lock's ceiling (confirmed empirically: lock=9600 + NvAPI offset
    # +1000 measured 10000, not capped). Back to NVML's set_clock_offsets for
    # both, so lock and offset are resolved by the same API.
    if profile.mem_offset_mhz is not None:
        ok, msg = set_clock_offsets(None, profile.mem_offset_mhz, gpu_index)
        if not ok:
            warnings.append(f"Mem offset: {msg}")

    if profile.power_limit_w is not None:
        ok, msg = set_power_limit(profile.power_limit_w, gpu_index)
        if not ok:
            warnings.append(f"Power limit: {msg}")

    if profile.curve_deltas:
        deltas = {int(k): v for k, v in profile.curve_deltas.items()}
        errors = validate_write(deltas, cfg.max_delta_khz)
        if errors:
            critical_errs.append("Curve: " + "; ".join(errors))
        else:
            if cfg.auto_snapshot:
                try:
                    snapshot_save(gpu, gpu_name, cfg.snapshot_dir, cfg.max_snapshots)
                except Exception as exc:
                    log.warning("Auto-snapshot failed: %s", exc)
            ret, desc = write_offsets(gpu, deltas)
            if ret != 0:
                critical_errs.append(f"Curve write failed ({ret}): {desc}")
    else:
        reset_offsets(gpu)

    return critical_errs, warnings


def apply_with_retry(gpu_index: int, name: str, cfg, max_retries: int = 3) -> bool:
    """Apply a named profile with read-back verification, retrying on mismatch."""
    from .native import load_profile
    from ..hal.gpu import get_gpu
    from ..hal.vfcurve import read_clock_offsets

    safe_name = "".join(c for c in name if c.isalnum() or c in " _-()").strip()
    filepath = os.path.join(cfg.profile_dir, f"{safe_name}.json")

    try:
        profile = load_profile(filepath)
    except FileNotFoundError:
        log.warning("Auto-load profile %r not found — skipping GPU %d", name, gpu_index)
        return False

    expected: dict[int, int] = (
        {int(k): v for k, v in profile.curve_deltas.items()}
        if profile.curve_deltas else {}
    )

    for attempt in range(max_retries):
        try:
            errs, warns = apply_profile(gpu_index, name, cfg)
        except Exception as exc:
            log.warning("Auto-load attempt %d/%d exception: %s", attempt + 1, max_retries, exc)
            errs, warns = [str(exc)], []

        if warns:
            log.warning("Auto-load attempt %d/%d non-fatal warnings: %s",
                        attempt + 1, max_retries, "; ".join(warns))

        if errs:
            log.warning("Auto-load attempt %d/%d errors: %s",
                        attempt + 1, max_retries, "; ".join(errs))
        elif expected:
            gpu, _ = get_gpu(index=gpu_index)
            offsets, err = read_clock_offsets(gpu)
            if offsets is None:
                log.warning("Auto-load attempt %d/%d: read-back failed: %s",
                            attempt + 1, max_retries, err)
            else:
                mismatches = [
                    f"pt{idx}: expected {val/1000:+.0f}MHz got {offsets[idx]/1000:+.0f}MHz"
                    for idx, val in expected.items()
                    if idx < len(offsets) and offsets[idx] != val
                ]
                if not mismatches:
                    log.info("Auto-load profile %r verified on GPU %d (attempt %d/%d)",
                             name, gpu_index, attempt + 1, max_retries)
                    return True
                log.warning("Auto-load attempt %d/%d: read-back mismatch — %s",
                            attempt + 1, max_retries, "; ".join(mismatches))
        else:
            log.info("Auto-load profile %r applied on GPU %d (attempt %d/%d)",
                     name, gpu_index, attempt + 1, max_retries)
            return True

        if attempt < max_retries - 1:
            delay = 2 ** attempt  # 1s, 2s, 4s
            log.info("Retrying auto-load in %ds…", delay)
            time.sleep(delay)

    log.warning("Auto-load profile %r failed after %d attempts on GPU %d",
                name, max_retries, gpu_index)
    return False


def run_autoload() -> None:
    """Read config and apply all configured auto-load profiles. Requires root."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if os.geteuid() != 0:
        print("nvcurve autoload: must run as root", file=sys.stderr)
        sys.exit(1)

    try:
        with open(_PERSISTENT_CONFIG_FILE) as f:
            cfg_data = json.load(f)
    except Exception:
        cfg_data = {}

    auto_load_profiles: dict = cfg_data.get("auto_load_profiles", {})
    if not auto_load_profiles:
        log.info("No auto-load profiles configured.")
        return

    from ..config import Config
    cfg = Config()
    for key in ("max_delta_khz", "auto_snapshot", "max_snapshots",
                "snapshot_dir", "profile_dir"):
        if key in cfg_data:
            setattr(cfg, key, cfg_data[key])

    from ..hal.gpu import init_nvapi, discover_gpus
    from ..hal.monitoring import init_nvml, shutdown_nvml

    try:
        init_nvapi()
    except Exception as exc:
        log.error("Failed to initialize NvAPI: %s", exc)
        sys.exit(1)

    init_nvml()  # best-effort
    gpus = discover_gpus()
    if not gpus:
        log.warning("No GPUs discovered.")

    key_to_idx = {_gpu_stable_key(info): info.index for info in gpus}
    for gpu_key, profile_name in auto_load_profiles.items():
        if not profile_name:
            continue
        gpu_idx = key_to_idx.get(gpu_key)
        if gpu_idx is None:
            log.warning("Auto-load: no GPU found with key %r — skipping", gpu_key)
            continue
        log.info("Auto-loading profile %r on GPU %d (%s)", profile_name, gpu_idx, gpu_key)
        apply_with_retry(gpu_idx, profile_name, cfg)

    shutdown_nvml()
