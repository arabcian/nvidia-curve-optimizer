"""Minimal daemon: apply auto-load profiles on boot, manage web server via Unix socket.

Run via:  nvcurve daemon
Or via systemd:  nvcurve service install

Protocol: newline-delimited JSON, one request → one response, connection closed.

Commands:
  {"cmd": "ping"}
  {"cmd": "serve_start", "host": "127.0.0.1", "port": 8042}
  {"cmd": "serve_stop"}
  {"cmd": "serve_status"}

Requires root.
"""

import asyncio
import grp
import json
import logging
import os
import signal
import socket
import struct
import subprocess
import sys

log = logging.getLogger("nvcurve.daemon")

SOCKET_PATH = "/run/nvcurve-daemon.sock"
_PERSISTENT_CONFIG_FILE = "/etc/nvcurve/config.json"
_SOCKET_GROUP = "nvcurve"  # optional; if present, only root + this group can connect

# Bind addresses accepted over the daemon socket. Anything else must be set
# by root in the persistent config file — see _handle_serve_start().
_ALLOWED_BIND_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


def _allowed_uids() -> set[int]:
    """UIDs permitted to issue daemon commands: root plus _SOCKET_GROUP members.

    Recomputed per connection rather than cached at startup so that adding a
    user to the group takes effect without restarting the daemon.
    """
    allowed = {0}
    try:
        group = grp.getgrnam(_SOCKET_GROUP)
    except KeyError:
        return allowed
    import pwd
    for member in group.gr_mem:
        try:
            allowed.add(pwd.getpwnam(member).pw_uid)
        except KeyError:
            continue
    # Users whose *primary* group is _SOCKET_GROUP don't appear in gr_mem.
    try:
        for entry in pwd.getpwall():
            if entry.pw_gid == group.gr_gid:
                allowed.add(entry.pw_uid)
    except Exception:
        pass
    return allowed


def _peer_uid(writer: asyncio.StreamWriter) -> int | None:
    """Return the UID of the process on the other end of a connected Unix socket.

    Returns None if it can't be determined (should not normally happen for
    AF_UNIX SOCK_STREAM on Linux, but callers must not assume a value).
    """
    sock = writer.get_extra_info("socket")
    if sock is None:
        return None
    try:
        creds = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        _pid, uid, _gid = struct.unpack("3i", creds)
        return uid
    except (OSError, AttributeError, struct.error):
        return None

# Global server subprocess — only touched from the asyncio event loop.
_server_proc: subprocess.Popen | None = None
_cfg = None  # Config instance, set in run()


# ── Socket command handlers ────────────────────────────────────────────────────

async def _handle_serve_start(host: str, port: int, trusted: bool = False) -> dict:
    global _server_proc
    if _server_proc is not None and _server_proc.poll() is None:
        return {"ok": False, "error": "web server already running", "pid": _server_proc.pid}

    # SECURITY: `host` arrives from a socket client, which is any member of
    # _SOCKET_GROUP — not just root. Without this check a local, unprivileged
    # group member could request host="0.0.0.0" and publish a root-owned,
    # GPU-writing API to every interface on the machine.
    #
    # trusted=True marks the one caller whose host did NOT come from a client:
    # the auto_serve path at daemon startup, which uses _cfg.host as read from
    # /etc/nvcurve/config.json (root-writable only). Everything arriving over
    # the socket is untrusted and may only ask for loopback.
    #
    # Note the asymmetry: an untrusted client cannot select a configured
    # non-loopback address by echoing it back, because its requested value is
    # rejected outright rather than compared against _cfg.host — otherwise a
    # group member could trigger the wide bind on demand whenever root had
    # configured one.
    if not trusted and host not in _ALLOWED_BIND_HOSTS:
        log.warning("Refused client-requested bind host %r", host)
        return {
            "ok": False,
            "error": f"refusing to bind {host!r}: only loopback may be requested over "
                     f"the daemon socket. Set 'host' in {_PERSISTENT_CONFIG_FILE} and "
                     f"enable auto_serve to bind elsewhere.",
        }
    if trusted and host not in _ALLOWED_BIND_HOSTS:
        log.warning(
            "auto_serve is binding non-loopback address %r — the API will be "
            "reachable from other hosts. Ensure this is intended.", host,
        )

    try:
        port = int(port)
    except (TypeError, ValueError):
        return {"ok": False, "error": f"invalid port: {port!r}"}
    if not (1 <= port <= 65535):
        return {"ok": False, "error": f"port out of range: {port}"}

    cmd = [sys.executable, "-m", "nvcurve", "serve", "start",
           "--host", host, "--port", str(port), "--direct"]
    log_path = "/var/log/nvcurve-server.log"
    log.info("Starting web server on %s:%d (log: %s)", host, port, log_path)
    log_fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o640)
    with os.fdopen(log_fd, "a") as lf:
        _server_proc = subprocess.Popen(
            cmd,
            stdout=lf,
            stderr=lf,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
    log.info("Web server started (PID %d)", _server_proc.pid)
    return {"ok": True, "pid": _server_proc.pid}


async def _handle_serve_stop() -> dict:
    global _server_proc
    if _server_proc is None or _server_proc.poll() is not None:
        _server_proc = None
        return {"ok": False, "error": "web server is not running"}
    log.info("Stopping web server (PID %d)…", _server_proc.pid)
    _server_proc.terminate()
    try:
        await asyncio.get_running_loop().run_in_executor(
            None, _server_proc.wait, 5
        )
    except Exception:
        _server_proc.kill()
    _server_proc = None
    return {"ok": True}


async def _handle_serve_status() -> dict:
    global _server_proc
    if _server_proc is not None and _server_proc.poll() is None:
        return {"ok": True, "running": True, "pid": _server_proc.pid}
    _server_proc = None
    return {"ok": True, "running": False}


async def _dispatch(req: dict) -> dict:
    cmd = req.get("cmd")
    if cmd == "ping":
        return {"ok": True}
    elif cmd == "serve_start":
        # Default to loopback, not _cfg.host: a client that omits "host" is
        # not asking for the administratively-configured wide bind, and
        # _handle_serve_start would (correctly) refuse it as an untrusted
        # request anyway.
        host = req.get("host", "127.0.0.1")
        port = req.get("port", _cfg.port)
        return await _handle_serve_start(host, port)
    elif cmd == "serve_stop":
        return await _handle_serve_stop()
    elif cmd == "serve_status":
        return await _handle_serve_status()
    else:
        return {"ok": False, "error": f"unknown command: {cmd!r}"}


async def _handle_client(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    resp: dict = {"ok": False, "error": "internal error"}
    uid = _peer_uid(writer)
    try:
        # SO_PEERCRED authorization. The socket's 0660 root:nvcurve mode is
        # the first line of defence, but it is not sufficient on its own:
        # the socket necessarily exists for a moment before it can be
        # chmod'ed, and filesystem permissions alone can't distinguish a
        # legitimate group member from a process that got in during that
        # window. Checking the kernel-supplied peer credentials on every
        # connection closes that gap. Previously `uid` was computed and
        # logged but never actually used to authorize anything.
        if uid is None:
            log.warning("Rejecting daemon connection with undeterminable peer uid")
            resp = {"ok": False, "error": "unauthorized"}
            return
        if uid not in _allowed_uids():
            log.warning("Rejecting daemon request from unauthorized uid=%d", uid)
            resp = {"ok": False, "error": "unauthorized"}
            return

        data = await asyncio.wait_for(reader.readline(), timeout=5.0)
        req = json.loads(data)
        if not isinstance(req, dict):
            resp = {"ok": False, "error": "request must be a JSON object"}
            return
        # Audit trail: this socket is reachable by any member of _SOCKET_GROUP
        # (see _serve_socket), not just root, and it fronts a root-owned
        # daemon — log who asked for what so misuse is at least visible.
        log.info("daemon request from uid=%s: %r", uid, req.get("cmd"))
        resp = await _dispatch(req)
    except asyncio.TimeoutError:
        resp = {"ok": False, "error": "timeout reading request"}
    except json.JSONDecodeError as exc:
        resp = {"ok": False, "error": f"invalid JSON: {exc}"}
    except Exception as exc:
        resp = {"ok": False, "error": str(exc)}
    finally:
        try:
            writer.write(json.dumps(resp).encode() + b"\n")
            await writer.drain()
        except Exception:
            pass
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


# ── Entrypoint ─────────────────────────────────────────────────────────────────

def _load_persistent_config() -> dict:
    try:
        with open(_PERSISTENT_CONFIG_FILE) as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as exc:
        # Log it. Returning {} silently made a corrupt config look exactly
        # like a missing one, so the daemon would boot with auto-load
        # disabled and never say why.
        log.warning("Could not read %s: %s", _PERSISTENT_CONFIG_FILE, exc)
        return {}
    if not isinstance(data, dict):
        log.warning("%s is not a JSON object — ignoring", _PERSISTENT_CONFIG_FILE)
        return {}
    return data


def run() -> None:
    """Run the nvcurve daemon: apply auto-load profiles, then serve the Unix socket."""
    global _cfg

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if os.geteuid() != 0:
        print("nvcurve daemon: must run as root", file=sys.stderr)
        sys.exit(1)

    cfg_data = _load_persistent_config()

    from .config import Config
    _cfg = Config()
    # Type-check what comes out of the file: `port` arriving as a string
    # (a hand-edited config) propagated all the way into the bind call and
    # failed there with an opaque error instead of here with a clear one.
    _expected = {
        "max_delta_khz": int, "auto_snapshot": bool, "max_snapshots": int,
        "snapshot_dir": str, "profile_dir": str, "host": str, "port": int,
    }
    for key, expected_type in _expected.items():
        if key not in cfg_data:
            continue
        value = cfg_data[key]
        if expected_type is int and isinstance(value, bool):
            log.warning("Config key %r must be an int — ignoring", key)
            continue
        if not isinstance(value, expected_type):
            log.warning("Config key %r must be %s, got %s — ignoring",
                        key, expected_type.__name__, type(value).__name__)
            continue
        setattr(_cfg, key, value)

    # Apply auto-load profiles in a subprocess so the daemon process itself
    # never loads NvAPI/NVML/HAL modules — keeps steady-state RSS low.
    auto_load_profiles = cfg_data.get("auto_load_profiles", {})
    if not isinstance(auto_load_profiles, dict):
        log.warning("Config key 'auto_load_profiles' must be an object — ignoring")
        auto_load_profiles = {}
    if auto_load_profiles:
        log.info("Applying auto-load profiles…")
        subprocess.run(
            [sys.executable, "-m", "nvcurve", "autoload"],
            check=False,
        )

    auto_serve: bool = cfg_data.get("auto_serve", False)
    asyncio.run(_serve_socket(auto_serve=auto_serve))

    log.info("Daemon stopped.")


async def _serve_socket(auto_serve: bool = False) -> None:
    global _server_proc

    # Clean up stale socket from a previous (unclean) run. Only unlink if it
    # really is a socket — refuse to remove a regular file or symlink that
    # somehow occupies the path, rather than deleting something unexpected
    # as root.
    if os.path.lexists(SOCKET_PATH):
        import stat as _stat
        mode = os.lstat(SOCKET_PATH).st_mode
        if not _stat.S_ISSOCK(mode):
            raise RuntimeError(
                f"{SOCKET_PATH} exists and is not a socket — refusing to remove it."
            )
        os.unlink(SOCKET_PATH)

    # This socket fronts a root daemon that can spawn/kill a root subprocess
    # on request — it should not be connectable by every local account.
    #
    # RACE: start_unix_server() creates the socket using the process umask,
    # which typically yields 0755 — world-connectable. The chown/chmod below
    # only ran afterwards, leaving a window in which any local user could
    # connect and issue commands. Creating it under a 0177 umask means it is
    # born 0600 and is never permissive, even briefly; the chmod then widens
    # it to 0660 only once the correct group ownership is in place.
    _old_umask = os.umask(0o177)
    try:
        server = await asyncio.start_unix_server(_handle_client, path=SOCKET_PATH)
    finally:
        os.umask(_old_umask)

    try:
        gid = grp.getgrnam(_SOCKET_GROUP).gr_gid
        os.chown(SOCKET_PATH, 0, gid)
        os.chmod(SOCKET_PATH, 0o660)
        log.info("Daemon socket restricted to root + group '%s'", _SOCKET_GROUP)
    except KeyError:
        # No group — leave it at the 0600 it was created with.
        os.chmod(SOCKET_PATH, 0o600)
        log.warning(
            "Group '%s' not found — daemon socket restricted to root only. "
            "Run `groupadd %s` and add trusted users to it to allow non-root "
            "CLI access.", _SOCKET_GROUP, _SOCKET_GROUP,
        )
    log.info("Daemon listening on %s", SOCKET_PATH)

    if auto_serve:
        log.info("auto_serve enabled — starting web server on boot")
        await _handle_serve_start(_cfg.host, _cfg.port, trusted=True)

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGTERM, stop_event.set)
    loop.add_signal_handler(signal.SIGINT, stop_event.set)

    async with server:
        await stop_event.wait()

    # Gracefully stop the web server if it's running.
    if _server_proc is not None and _server_proc.poll() is None:
        log.info("Stopping web server (PID %d)…", _server_proc.pid)
        _server_proc.terminate()
        try:
            await asyncio.wait_for(
                asyncio.get_running_loop().run_in_executor(None, _server_proc.wait),
                timeout=5,
            )
        except asyncio.TimeoutError:
            _server_proc.kill()

    if os.path.exists(SOCKET_PATH):
        os.unlink(SOCKET_PATH)
