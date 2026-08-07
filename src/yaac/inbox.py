"""Local inbox: an append-only JSONL log plus a byte-offset cursor.

One inbox per handle, in two files: ``{handle}.jsonl`` holding one JSON message per line, and ``{handle}.cursor``
holding the byte offset consumed so far. The log is never truncated for the lifetime of the membership, so
``tail -f`` on it shows every message this participant received.

Lines are written with `protocol.dumps`, so each is canonical JSON: keys sorted at every level, no insignificant
whitespace, and exactly one line per message however many newlines the body contains. Equal content therefore
produces equal bytes, which is what signing or content-hashing a message would need.

Consumers read forward from the cursor offset and rewrite it under `flock`, which prevents two readers from
returning the same message. The only consumer in v0 is the ``check_inbox`` MCP tool, but the files are readable by
any process, so the v1 delivery hook needs no change here.

This module imports no pyzmq and opens no sockets. It is called only from ``Backend.connect`` onwards, so a session
that never connects creates no files.
"""

import json
import os
import pathlib
import tempfile
from typing import Any

try:
    import fcntl
except ImportError:  # Windows: emulate flock over the first byte via msvcrt
    fcntl = None
    import msvcrt

from .protocol import dumps


def _lock_exclusive(file_obj) -> None:
    if fcntl is not None:
        fcntl.flock(file_obj.fileno(), fcntl.LOCK_EX)
    else:
        saved_position = file_obj.tell()
        file_obj.seek(0)
        msvcrt.locking(file_obj.fileno(), msvcrt.LK_LOCK, 1)
        file_obj.seek(saved_position)


def _unlock(file_obj) -> None:
    if fcntl is not None:
        fcntl.flock(file_obj.fileno(), fcntl.LOCK_UN)
    else:
        saved_position = file_obj.tell()
        file_obj.seek(0)
        msvcrt.locking(file_obj.fileno(), msvcrt.LK_UNLCK, 1)
        file_obj.seek(saved_position)


def runtime_dir() -> pathlib.Path:
    """Base directory for YAAC's transient state.

    ``XDG_RUNTIME_DIR`` when the platform provides one, ``/tmp/yaac`` otherwise
    (macOS, which is the common case for this project).
    """
    if os.name == "nt":
        # "/tmp/yaac" on Windows resolves against the current drive, so sessions
        # started from different drives would silently get separate inboxes.
        return pathlib.Path(tempfile.gettempdir()) / "yaac"
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    return pathlib.Path(xdg) / "yaac" if xdg else pathlib.Path("/tmp/yaac")


class Inbox:
    """The on-disk inbox for one handle.

    Keyed by handle rather than by nickname: one session holding several
    memberships is out of scope for v0, but keying this way means it will not
    require a file-format change.
    """

    def __init__(self, handle: str, base: pathlib.Path | None = None) -> None:
        self.handle = handle
        self.base = base or runtime_dir()
        self.dir = self.base / "inbox"
        self.log_path = self.dir / f"{handle}.jsonl"
        self.cursor_path = self.dir / f"{handle}.cursor"
        self.live_path = self.base / "live" / f"{handle}.json"

    # -- lifecycle -------------------------------------------------------

    def create(self, descriptor: dict[str, Any]) -> None:
        """Create the inbox. Called on ``connect_to_channel``, never before.

        ``descriptor`` records who this handle is and which process owns it, so
        that a later out-of-process consumer can find the right inbox and tell
        whether its owner is still alive.
        """
        self.dir.mkdir(parents=True, exist_ok=True)
        self.live_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_path.touch()
        self.cursor_path.write_text("0")
        self.live_path.write_text(dumps(descriptor).decode("utf-8"))

    def destroy(self) -> None:
        """Remove every file this inbox owns. Called on ``disconnect``."""
        for path in (self.log_path, self.cursor_path, self.live_path):
            path.unlink(missing_ok=True)

    # -- writing ---------------------------------------------------------

    def append(self, message: dict[str, Any]) -> None:
        """Append one message. Opened per call in append mode, so an external
        reader always sees a complete line."""
        line = dumps(message).decode("utf-8") + "\n"
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(line)
            f.flush()

    # -- reading ---------------------------------------------------------

    def read_new(self, advance: bool = True) -> list[dict[str, Any]]:
        """Return everything appended since the cursor, and move the cursor past it.

        The cursor is held under ``flock`` for the whole read so that two
        consumers cannot deliver the same message twice. A partially written
        trailing line is left for the next call rather than being parsed.
        """
        if not self.log_path.exists():
            return []

        self.cursor_path.touch()
        with self.cursor_path.open("r+", encoding="utf-8") as cursor_file:
            _lock_exclusive(cursor_file)
            try:
                raw = cursor_file.read().strip()
                offset = int(raw) if raw else 0

                with self.log_path.open("rb") as log:
                    log.seek(offset)
                    data = log.read()

                # Only whole lines are consumed; a torn tail waits for next time.
                consumed = data.rfind(b"\n") + 1
                if consumed == 0:
                    return []

                messages = []
                for line in data[:consumed].splitlines():
                    if not line.strip():
                        continue
                    try:
                        messages.append(json.loads(line.decode("utf-8")))
                    except UnicodeDecodeError, json.JSONDecodeError:
                        continue  # a corrupt line must not wedge the inbox

                if advance:
                    cursor_file.seek(0)
                    cursor_file.truncate()
                    cursor_file.write(str(offset + consumed))
                    cursor_file.flush()
                return messages
            finally:
                _unlock(cursor_file)

    def pending_count(self) -> int:
        """How many whole messages are waiting, without consuming them."""
        return len(self.read_new(advance=False))
