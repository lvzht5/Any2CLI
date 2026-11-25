"""Local, versioned profiles with atomic writes and serialized registry updates."""

import json
import os
import re
import tempfile
from contextlib import contextmanager
from pathlib import Path

from .catalog import CatalogError, FORMAT


def bundle_id(value):
    if re.fullmatch(r"[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)+", value) is None:
        raise CatalogError("Expected an application bundle ID or a path to an installed .app.")
    return value


def profile_path(value):
    p = Path(value).expanduser()
    if p.suffix == ".json" or p.is_file() or p.parent != Path("."):
        return p
    return Path(".any2cli") / (bundle_id(value) + ".json")


@contextmanager
def exclusive(file, *, wait=True):
    if os.name == "nt":
        import msvcrt

        file.seek(0)
        if not file.read(1):
            file.write(b"\0")
            file.flush()
        file.seek(0)
        msvcrt.locking(file.fileno(), msvcrt.LK_LOCK if wait else msvcrt.LK_NBLCK, 1)
        try:
            yield
        finally:
            file.seek(0)
            msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(file, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
        try:
            yield
        finally:
            fcntl.flock(file, fcntl.LOCK_UN)


class Store:
    def __init__(self, path):
        self.path = Path(path)

    def read(self):
        with self.path.open(encoding="utf-8") as stream:
            data = json.load(stream)
        if not isinstance(data, dict) or data.get("format") != FORMAT:
            raise CatalogError("Unsupported Any2CLI profile format.")
        return data

    def update(self, change, *, initial=None):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.with_suffix(self.path.suffix + ".lock").open("a+b") as lock, exclusive(lock):
            if self.path.exists() or initial is None:
                data = self.read()
            else:
                data = initial
            result = change(data)
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=self.path.parent,
                                             prefix=".any2cli-", delete=False) as tmp:
                temporary = Path(tmp.name)
                try:
                    json.dump(data, tmp, ensure_ascii=False, indent=2, allow_nan=False)
                    tmp.write("\n")
                    tmp.flush()
                    os.fsync(tmp.fileno())
                except BaseException:
                    temporary.unlink(missing_ok=True)
                    raise
            try:
                os.replace(temporary, self.path)
            finally:
                temporary.unlink(missing_ok=True)
            return result


@contextmanager
def live_session(app):
    # Window state is process-global even if two profiles target the same app.
    if os.name == "nt":
        raise CatalogError("Live desktop operation currently requires macOS.")
    path = Path(tempfile.gettempdir()) / f"any2cli-{os.getuid()}-{bundle_id(app)}.lock"
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "a+b") as lock:
        import fcntl

        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise CatalogError("Another Any2CLI session is using this application.", status="busy") from exc
        yield
