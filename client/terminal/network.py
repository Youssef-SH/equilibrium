"""Shared wire framing and private session persistence; no gameplay logic."""
import json
import os
import uuid
import tempfile
import sys
import stat
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from websockets.asyncio.client import connect


def state_directory():
    """Return the private per-user directory used by the development client."""
    if sys.platform == "win32":
        root = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        root = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
        if not root.is_absolute():
            root = Path.home() / ".local" / "state"
    return root / "equilibrium"


def envelope(message_type, **fields):
    return dict(v=1, type=message_type, **{"correlation_id": uuid.uuid4().hex, **fields})


async def send(socket, message_type, **fields):
    message = envelope(message_type, **fields)
    await socket.send(json.dumps(message))
    return message["correlation_id"]


def default_session_path():
    # A fresh name per process prevents independent local clients from sharing a
    # credential. Existing sessions are discovered separately for Continue Match.
    return state_directory() / (uuid.uuid4().hex + ".session.json")


def available_sessions():
    """Return valid-looking saved sessions newest-first, without exposing tokens."""
    root = state_directory()
    if not root.is_dir() or root.is_symlink():
        return []
    result = []
    for path in root.glob("*.session.json"):
        if session_in_use(path):
            continue
        try:
            load_session(path)
            result.append(path)
        except (OSError, ValueError):
            continue
    return sorted(result, key=lambda path: path.stat().st_mtime_ns, reverse=True)


def _lock_path(path):
    return Path(str(Path(path).expanduser().absolute()) + ".lock")


def session_in_use(path):
    lock = _lock_path(path)
    try:
        pid = int(lock.read_text(encoding="ascii"))
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        try:
            lock.unlink()
        except OSError:
            pass
        return False
    except (OSError, ValueError):
        return lock.exists()


def claim_session(path):
    """Exclusively reserve one credential file for this client process."""
    path = checked_path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock = _lock_path(path)
    if session_in_use(path):
        raise ValueError("That saved match is already open in another client.")
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise ValueError("That saved match is already open in another client.") from None
    with os.fdopen(fd, "w", encoding="ascii") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(str(os.getpid()))
        stream.flush()
        os.fsync(stream.fileno())
    return lock


def release_session(lock):
    if lock is None:
        return
    try:
        if Path(lock).read_text(encoding="ascii") == str(os.getpid()):
            Path(lock).unlink()
    except OSError:
        pass


def diagnostic_log_path():
    return state_directory() / "client-diagnostics.log"


def diagnostic_logger():
    """Create a bounded sanitized log. Callers must never pass credentials."""
    logger = logging.getLogger("equilibrium.client")
    if logger.handlers:
        return logger
    try:
        root = state_directory()
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        handler = RotatingFileHandler(root / "client-diagnostics.log", maxBytes=512_000, backupCount=2, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    except OSError:
        logger.addHandler(logging.NullHandler())
    return logger


def session_data(message):
    if not isinstance(message, dict) or not all(isinstance(message.get(k), str) and message[k] for k in ("lobby_id", "rejoin_token")):
        raise ValueError("Invalid session credentials.")
    return {k: message[k] for k in ("lobby_id", "rejoin_token")}


def checked_path(path):
    path = Path(path).expanduser().absolute()
    for part in (path, *path.parents):
        if part.is_symlink() or part.is_junction():
            raise ValueError("Session paths must not contain symbolic links.")
    if path.exists() and not path.is_file():
        raise ValueError("Session destination must be a regular file.")
    return path


def save_session(path, message):
    data = session_data(message)
    path = checked_path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    checked_path(path)
    temporary = None
    try:
        fd, temporary = tempfile.mkstemp(prefix=".session-", dir=path.parent)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(data, stream)
            stream.flush()
            os.fsync(stream.fileno())
        checked_path(path)
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            os.unlink(temporary)


def load_session(path):
    path = checked_path(path)
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, encoding="utf-8") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode) or os.fstat(stream.fileno()).st_size > 4096:
            raise ValueError("Invalid session file.")
        try:
            return session_data(json.load(stream))
        except (ValueError, UnicodeError):
            raise ValueError("Invalid session file. Use the original private session file.") from None
