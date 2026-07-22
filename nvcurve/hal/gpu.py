"""GPU discovery and initialization."""

import ctypes

from ..nvapi.bootstrap import query_interface
from ..nvapi.constants import FUNC
from ..nvapi.errors import NvApiUnavailableError, NoGpuFoundError, GpuIndexError
from ..nvapi.types import GpuInfo


def init_nvapi() -> None:
    """Initialize NvAPI. Must be called before any GPU operations.

    Raises NvApiUnavailableError on failure — this used to print()+exit(1)
    directly, which killed any process that called it (server.py, daemon.py,
    a test) rather than letting that caller decide how to handle it. CLI
    entry points that want the old behaviour catch this in main() instead.
    """
    init_fn = query_interface(FUNC["Initialize"], nargs=0)
    if not init_fn or init_fn() != 0:
        raise NvApiUnavailableError("NvAPI_Initialize failed")


def enumerate_gpus() -> tuple[ctypes.Array, int]:
    """Return (gpu_handles_array, count).

    Raises NvApiUnavailableError if the enumeration call itself fails, or
    NoGpuFoundError if it succeeds but reports zero GPUs.
    """
    gpus = (ctypes.c_void_p * 64)()
    ngpu = ctypes.c_int32()
    ret = query_interface(FUNC["EnumPhysicalGPUs"])(ctypes.byref(gpus), ctypes.byref(ngpu))
    if ret != 0:
        raise NvApiUnavailableError(f"EnumPhysicalGPUs failed (code {ret})")
    if ngpu.value == 0:
        raise NoGpuFoundError("No NVIDIA GPUs found")
    return gpus, ngpu.value


def get_gpu_name(gpu) -> str:
    """Return the full name string for a GPU handle."""
    name_buf = ctypes.create_string_buffer(256)
    query_interface(FUNC["GetFullName"])(gpu, name_buf)
    return name_buf.value.decode(errors="replace")


def discover_gpus() -> list[GpuInfo]:
    """Initialize NvAPI and NVML, and return a list of GpuInfo for all physical GPUs.

    Returns an empty list (rather than raising) when NvAPI initializes fine
    but reports zero GPUs — this is the one case callers like server.py's
    startup already treat as a soft "nothing found" condition rather than a
    hard error. A genuinely unavailable driver (NvApiUnavailableError from
    init_nvapi()/enumerate_gpus() itself failing) still propagates, since
    that's a real environment problem, not "zero of N found".
    """
    init_nvapi()
    try:
        gpus, count = enumerate_gpus()
    except NoGpuFoundError:
        return []
    infos = []

    try:
        import pynvml
        pynvml.nvmlInit()
        has_nvml = True
    except Exception:
        has_nvml = False

    for i in range(count):
        name = get_gpu_name(gpus[i])
        uuid = None
        pci_bus_id = None
        if has_nvml:
            try:
                handle = pynvml.nvmlDeviceGetHandleByIndex(i)
                uuid = pynvml.nvmlDeviceGetUUID(handle)
                # NVML might return bytes
                if isinstance(uuid, bytes):
                    uuid = uuid.decode('utf-8', errors='ignore')
                pci_info = pynvml.nvmlDeviceGetPciInfo(handle)
                # Parse something like "00000000:01:00.0" -> bus is 1
                if isinstance(pci_info.bus, bytes):
                    pci_bus_id = int(pci_info.bus.decode('utf-8', errors='ignore'), 16)
                else:
                    pci_bus_id = pci_info.bus
            except Exception:
                pass
        infos.append(GpuInfo(name=name, index=i, uuid=uuid, pci_bus_id=pci_bus_id))

    if has_nvml:
        try:
            pynvml.nvmlShutdown()
        except Exception:
            pass

    return infos


def get_gpu(index: int = 0):
    """Initialize NvAPI, enumerate GPUs, and return the handle for `index`.

    Also returns the GPU name as a convenience.
    Returns (handle, name).
    """
    init_nvapi()
    gpus, count = enumerate_gpus()
    if index >= count:
        raise GpuIndexError(f"GPU index {index} out of range (found {count} GPU(s))")
    gpu = gpus[index]
    name = get_gpu_name(gpu)
    return gpu, name
