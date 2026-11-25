"""Local, versioned profiles with atomic writes and serialized registry updates."""

import errno
import hashlib
import json
import os
import re
import tempfile
from contextlib import ExitStack, contextmanager
from pathlib import Path

from .catalog import CatalogError, FORMAT, KINDS


def bundle_id(value):
    if value.casefold().endswith(".exe"):
        stem = value[:-4]
        if (not stem.strip(" .") or re.search(r'[<>:"/\\|?*\x00-\x1f]', stem)
                or re.fullmatch(r"CON|PRN|AUX|NUL|(?:COM|LPT)[1-9¹²³]", stem.split(".")[0].rstrip(), re.I)):
            raise CatalogError("Invalid Windows executable filename.")
        return value.casefold()
    if re.fullmatch(r"[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)+", value) is None:
        raise CatalogError("Expected an application bundle ID or an executable name ending in .exe.")
    return value


def profile_path(value):
    p = Path(value).expanduser()
    if p.parent == Path(".") and p.suffix.casefold() == ".exe":
        return Path(".any2cli") / (bundle_id(p.name) + ".json")
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
            temporary = None
            try:
                with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=self.path.parent,
                                                 prefix=".any2cli-", delete=False) as tmp:
                    temporary = Path(tmp.name)
                    json.dump(data, tmp, ensure_ascii=False, indent=2, allow_nan=False)
                    tmp.write("\n")
                    tmp.flush()
                    os.fsync(tmp.fileno())
                os.replace(temporary, self.path)
            finally:
                if temporary is not None:
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
    with os.fdopen(fd, "r+b") as lock, ExitStack() as stack:
        try:
            stack.enter_context(exclusive(lock, wait=False))
        except OSError as exc:
            if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                raise
            raise CatalogError("Another Any2CLI session is using this application.", status="busy") from exc
        yield
