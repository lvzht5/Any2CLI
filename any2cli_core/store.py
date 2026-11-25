"""Local, versioned profiles with atomic writes and serialized registry updates."""

import json
import errno
import hashlib
import os
import re
import tempfile
from contextlib import contextmanager
from pathlib import Path

from .catalog import CatalogError, FORMAT, KINDS


def bundle_id(value):
    if value.casefold().endswith(".exe") and re.fullmatch(r'[\w(][\w .()\-]*\.exe', value, re.I):
        if value.split(".", 1)[0].upper() not in {"CON", "PRN", "AUX", "NUL", *(f"{p}{i}" for p in ("COM", "LPT") for i in range(1, 10))}:
            return value.casefold()
        raise CatalogError("Reserved Windows device name.")
    if re.fullmatch(r"[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)+", value) is None:
        raise CatalogError("Expected an application bundle ID or an executable name ending in .exe.")
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
        # Windows permits locking a range beyond EOF. Reading an already
        # locked byte here would fail before the blocking acquisition.
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
        if any(op.get("kind") not in KINDS for op in data["operations"].values()):
            raise CatalogError("This profile contains retired operations. Explore into a new profile.",
                               status="unsupported_profile")
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
    key = bundle_id(app)
    name = ("any2cli-" + hashlib.sha256(key.encode()).hexdigest() + ".lock" if os.name == "nt"
            else f"any2cli-{os.getuid()}-{key}.lock")
    path = Path(tempfile.gettempdir()) / name
    fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "r+b") as lock:
        guard = exclusive(lock, wait=False)
        try:
            guard.__enter__()
        except OSError as exc:
            if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                raise
            raise CatalogError("Another Any2CLI session is using this application.", status="busy") from exc
        try:
            yield
        finally:
            guard.__exit__(None, None, None)
