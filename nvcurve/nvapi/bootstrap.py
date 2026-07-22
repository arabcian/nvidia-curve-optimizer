"""NvAPI bootstrap — library loading, function resolution, versioned struct calls."""

import ctypes
import struct

from .errors import NVAPI_ERRORS, NvApiUnavailableError


def load_nvapi() -> ctypes.CDLL:
    """Load libnvidia-api.so from the NVIDIA driver.

    Raises NvApiUnavailableError if the library can't be found — callers
    that want the previous print()+exit(1) CLI behaviour catch this at the
    entry point (see cli.py:main()) rather than the process dying wherever
    this happens to be imported from.
    """
    for name in ("libnvidia-api.so", "libnvidia-api.so.1"):
        try:
            return ctypes.CDLL(name)
        except OSError:
            continue
    raise NvApiUnavailableError(
        "Cannot load libnvidia-api.so — ensure the NVIDIA proprietary driver is installed."
    )


# Lazily-initialized library handle and QueryInterface function pointer.
# Loading used to happen eagerly at module import time, which meant simply
# importing this module (e.g. from a test, or `nvcurve --version` with no
# driver present) crashed the whole process. Function pointers don't change
# for the lifetime of the process once resolved, so a cache here also avoids
# re-resolving the same fid on every single call (see nvcall()/nvcall_raw()
# below and hal.monitoring.read_voltage(), which runs once per monitor poll).
_nvapi: ctypes.CDLL | None = None
_QI = None
_fn_cache: dict[int, "ctypes._CFuncPtr"] = {}


def _ensure_loaded() -> None:
    global _nvapi, _QI
    if _QI is not None:
        return
    _nvapi = load_nvapi()
    qi = _nvapi.nvapi_QueryInterface
    qi.restype = ctypes.c_void_p
    qi.argtypes = [ctypes.c_uint32]
    _QI = qi


def query_interface(fid: int, nargs: int = 2):
    """Resolve an NvAPI function pointer by its 32-bit ID.

    Returns a callable ctypes function, or None if the driver doesn't expose
    it. Raises NvApiUnavailableError if the driver library itself can't be
    loaded. Resolved pointers are cached per (fid, nargs) since they're
    constant for the process lifetime.
    """
    _ensure_loaded()
    key = (fid, nargs)
    cached = _fn_cache.get(key)
    if cached is not None:
        return cached
    ptr = _QI(fid)
    if not ptr:
        return None
    fn = ctypes.CFUNCTYPE(ctypes.c_int32, *[ctypes.c_void_p] * nargs)(ptr)
    _fn_cache[key] = fn
    return fn


def nvcall(
    fid: int,
    gpu,
    size: int,
    ver: int = 1,
    pre_fill=None,
) -> tuple[bytes | None, str]:
    """Call an NvAPI function with a versioned struct buffer.

    Allocates a buffer of `size` bytes, writes the version word
    ``(ver << 16) | size`` at offset 0, optionally calls ``pre_fill(buf)``
    to populate request fields, then invokes the function.

    Returns ``(bytes, "OK")`` on success or ``(None, error_description)`` on
    failure.
    """
    func = query_interface(fid)
    if not func:
        return None, "function pointer not found (driver too old?)"
    buf = ctypes.create_string_buffer(size)
    struct.pack_into("<I", buf, 0, (ver << 16) | size)
    if pre_fill:
        pre_fill(buf)
    ret = func(gpu, buf)
    if ret != 0:
        return None, f"error {ret} ({NVAPI_ERRORS.get(ret, 'unknown')})"
    return bytes(buf), "OK"


def nvcall_raw(fid: int, gpu, buf: ctypes.Array) -> tuple[int, str]:
    """Call an NvAPI function with a pre-built mutable buffer.

    Used for write operations where the caller needs full control over the
    buffer contents (e.g. SetClockBoostTable).

    Returns ``(return_code, description)``.
    """
    func = query_interface(fid)
    if not func:
        return -999, "function pointer not found (driver too old?)"
    ret = func(gpu, buf)
    return ret, NVAPI_ERRORS.get(ret, f"unknown ({ret})")
