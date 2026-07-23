"""Centralized safety validation for all write operations.

Every write path calls validate_write() before touching hardware.
"""

from .nvapi.constants import CT_POINTS, MAX_DELTA_KHZ

# struct.pack_into("<i", ...) in hal/vfcurve.py requires delta_khz to fit in
# a signed 32-bit int. MAX_DELTA_KHZ (1_000_000 kHz = ±1000 MHz) is nowhere
# near this range, so in normal operation the check below is unreachable —
# it exists so that a future change to MAX_DELTA_KHZ, or a --max-delta value
# large enough to matter, fails here with a clear message instead of an
# unhandled struct.error deep in the write path.
_INT32_MIN = -(2 ** 31)
_INT32_MAX = 2 ** 31 - 1

# Upper sanity bound for a memory locked-clock value, in MHz. NVML snaps an
# unsupported value to the nearest supported clock instead of rejecting it,
# so this is not a correctness check — it only rejects values that are
# obviously not a clock at all (negative, zero, or absurdly large), which
# would otherwise be passed straight into nvmlDeviceSetMemoryLockedClocks.
MAX_MEM_CLOCK_MHZ = 100_000


def check_negative_freq_warnings(
    point_deltas: dict[int, int],
    vfp_freqs_khz: list[int],
    current_offsets_khz: list[int] | None = None,
) -> list[str]:
    """Return warnings for deltas that would produce negative effective frequency.

    GetVFPCurve returns the *current effective* frequencies (already reflecting
    any applied boost delta). The true hardware base for each point is therefore:
        true_base = vfp_freq - current_offset

    The new effective with the proposed delta:
        new_effective = true_base + new_delta = vfp_freq + (new_delta - current_offset)

    The driver clamps results, but users should be informed when their delta
    would push a point's effective frequency below zero.
    """
    warnings = []
    for point, new_delta_khz in point_deltas.items():
        if point < 0 or point >= len(vfp_freqs_khz):
            continue
        vfp_freq = vfp_freqs_khz[point]
        if vfp_freq == 0:
            continue  # unused / placeholder point
        current_delta = (
            current_offsets_khz[point]
            if current_offsets_khz and point < len(current_offsets_khz)
            else 0
        )
        new_effective = vfp_freq + (new_delta_khz - current_delta)
        if new_effective < 0:
            warnings.append(
                f"Point {point}: delta {new_delta_khz / 1000:+.0f} MHz would produce "
                f"effective frequency {new_effective / 1000:.0f} MHz — driver will clamp to 0."
            )
    return warnings


def validate_limits(
    gpu_index: int,
    power_limit_w: int | None = None,
    mem_offset_mhz: int | None = None,
    mem_locked_min_mhz: int | None = None,
    mem_locked_max_mhz: int | None = None,
) -> list[str]:
    """Validate the non-curve ("secondary") settings against driver-reported ranges.

    Returns a list of error strings; empty means the values are acceptable.

    This is the single implementation of these checks. The REST endpoint
    (/api/limits) used to carry its own inline copy while the profile-apply
    path (profiles/apply.py, and root_helper's apply_gpu_offsets which feeds
    it) applied the very same fields with no bounds checking at all — so the
    same out-of-range value was rejected with a clear 400 over HTTP but
    passed straight to NVML when it arrived inside a profile. Both callers
    now go through here.

    Driver queries that fail are treated as "can't validate" rather than
    "invalid": a missing range must not block an otherwise legitimate apply
    on a GPU/driver combination that doesn't report it.
    """
    from .hal.limits import get_power_limit, get_mem_offset_range

    errors: list[str] = []

    if power_limit_w is not None:
        if not isinstance(power_limit_w, int) or isinstance(power_limit_w, bool):
            errors.append("power_limit_w must be an integer")
        else:
            try:
                info = get_power_limit(gpu_index)
                min_w, max_w = info.get("min_power_limit_w"), info.get("max_power_limit_w")
            except Exception:
                min_w = max_w = None
            if min_w is not None and max_w is not None and not (min_w <= power_limit_w <= max_w):
                errors.append(
                    f"power_limit_w {power_limit_w} is outside the driver's "
                    f"reported range ({min_w}-{max_w} W)"
                )

    if mem_offset_mhz is not None:
        if not isinstance(mem_offset_mhz, int) or isinstance(mem_offset_mhz, bool):
            errors.append("mem_offset_mhz must be an integer")
        else:
            try:
                rng = get_mem_offset_range(gpu_index)
                min_off, max_off = rng.get("min_mem_offset_mhz"), rng.get("max_mem_offset_mhz")
            except Exception:
                min_off = max_off = None
            if min_off is not None and max_off is not None and not (min_off <= mem_offset_mhz <= max_off):
                errors.append(
                    f"mem_offset_mhz {mem_offset_mhz} is outside the driver's "
                    f"reported range ({min_off}-{max_off} MHz)"
                )

    for label, val in (("mem_locked_min_mhz", mem_locked_min_mhz),
                       ("mem_locked_max_mhz", mem_locked_max_mhz)):
        if val is None:
            continue
        if not isinstance(val, int) or isinstance(val, bool):
            errors.append(f"{label} must be an integer")
        elif not (0 < val <= MAX_MEM_CLOCK_MHZ):
            errors.append(
                f"{label} {val} is outside the plausible range "
                f"(1-{MAX_MEM_CLOCK_MHZ} MHz)"
            )

    if (
        isinstance(mem_locked_min_mhz, int) and isinstance(mem_locked_max_mhz, int)
        and not isinstance(mem_locked_min_mhz, bool) and not isinstance(mem_locked_max_mhz, bool)
        and mem_locked_min_mhz > mem_locked_max_mhz
    ):
        # The driver snaps an out-of-range single value to its nearest
        # supported clock rather than erroring (see hal/limits.py's
        # get_current_mem_clock() docstring), so a full bounds check against
        # the supported-clock list would just duplicate that silently
        # permissive behaviour. An inverted min > max is unambiguously a
        # caller mistake, though.
        errors.append(
            f"mem_locked_min_mhz ({mem_locked_min_mhz}) is greater than "
            f"mem_locked_max_mhz ({mem_locked_max_mhz})"
        )

    return errors


def validate_write(
    point_deltas: dict[int, int],
    max_delta_khz: int,
) -> list[str]:
    """Validate a proposed write request.

    Args:
        point_deltas: {point_index: delta_kHz}
        max_delta_khz: absolute delta limit (e.g. 300_000 for ±300 MHz).
            Callers (e.g. --max-delta) can raise this, but it is clamped to
            MAX_DELTA_KHZ (the driver's own documented hard cap) below — that
            cap can't be overridden from the CLI or API.

    Returns a list of error message strings. Empty list means the request is safe.
    """
    errors = []
    effective_max = min(max_delta_khz, MAX_DELTA_KHZ)

    for point, delta_khz in point_deltas.items():
        if point < 0 or point >= CT_POINTS:
            errors.append(f"Point {point} out of range (0–{CT_POINTS - 1})")
            continue

        if not (_INT32_MIN <= delta_khz <= _INT32_MAX):
            errors.append(
                f"Delta {delta_khz} kHz for point {point} does not fit the driver's "
                f"signed 32-bit freqDelta field (must be within {_INT32_MIN}..{_INT32_MAX} kHz)."
            )
            continue

        if abs(delta_khz) > effective_max:
            if max_delta_khz > MAX_DELTA_KHZ:
                errors.append(
                    f"Delta {delta_khz / 1000:+.0f} MHz for point {point} exceeds the "
                    f"driver's hard cap of ±{MAX_DELTA_KHZ / 1000:.0f} MHz — "
                    "this cannot be raised with --max-delta."
                )
            else:
                errors.append(
                    f"Delta {delta_khz / 1000:+.0f} MHz for point {point} exceeds "
                    f"safety limit of ±{max_delta_khz / 1000:.0f} MHz. "
                    "Use --max-delta to raise the limit if needed."
                )

    return errors
