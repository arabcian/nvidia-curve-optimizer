# NvAPI error codes and exception class.
# Linux NvAPI uses negative integers, not the 0x80000000+ range from Windows.

NVAPI_ERRORS: dict[int, str] = {
    0:   "OK",
    -1:  "GENERIC_ERROR",
    -5:  "INVALID_ARGUMENT",
    -6:  "NVIDIA_DEVICE_NOT_FOUND",
    -7:  "END_ENUMERATION",
    -8:  "INVALID_HANDLE",
    -9:  "INCOMPATIBLE_STRUCT_VERSION",
    -10: "HANDLE_INVALIDATED",
    -14: "INVALID_POINTER",
}


class NvAPIError(Exception):
    """Raised when an NvAPI call returns a non-zero error code."""

    def __init__(self, code: int, context: str = ""):
        self.code = code
        self.name = NVAPI_ERRORS.get(code, f"unknown ({code})")
        self.context = context
        msg = f"NvAPI error {code} ({self.name})"
        if context:
            msg = f"{context}: {msg}"
        super().__init__(msg)


class NvApiUnavailableError(Exception):
    """Raised when the NVIDIA driver / NvAPI library can't be loaded or initialized.

    This (and the two exceptions below) replace what used to be bare
    print()+sys.exit(1) calls inside hal/gpu.py and nvapi/bootstrap.py.
    Library code raising SystemExit deep in the call stack meant any process
    embedding it (server.py, daemon.py, a test suite) died outright on a
    transient GPU hiccup instead of being able to catch and handle it.
    Callers that want the old CLI behaviour (print + exit 1) now do that
    explicitly at the entry point — see cli.py:main().
    """


class NoGpuFoundError(Exception):
    """Raised when NvAPI enumerates zero physical GPUs."""


class GpuIndexError(Exception):
    """Raised when a requested GPU index is out of range."""
