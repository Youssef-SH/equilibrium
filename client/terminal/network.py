"""Shared wire framing and private session persistence; no gameplay logic."""
import json
import os
import uuid
import tempfile
import sys
import stat
from pathlib import Path

from websockets.asyncio.client import connect


def envelope(message_type, **fields):
    return dict(v=1, type=message_type, **{"correlation_id": uuid.uuid4().hex, **fields})


async def send(socket, message_type, **fields):
    message = envelope(message_type, **fields)
    await socket.send(json.dumps(message))
    return message["correlation_id"]


def default_session_path():
    if sys.platform == "win32":
        root = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        root = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
        if not root.is_absolute():
            root = Path.home() / ".local" / "state"
    return root / "equilibrium" / (uuid.uuid4().hex + ".session.json")


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
