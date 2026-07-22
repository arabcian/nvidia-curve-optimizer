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
