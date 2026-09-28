"""Cross-platform exclusive file lock: never two commands at once."""

from __future__ import annotations

import os
import time
from pathlib import Path


class LockTimeout(Exception):
    pass


class FileLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._fh = None

    def _try(self) -> bool:
        assert self._fh is not None
        try:
            if os.name == "nt":
                import msvcrt

                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False

    def acquire(self, wait_seconds: float, poll: float = 1.0) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "a+", encoding="utf-8")  # noqa: SIM115
        deadline = time.monotonic() + wait_seconds
        while not self._try():
            if time.monotonic() >= deadline:
                self._fh.close()
                self._fh = None
                raise LockTimeout(f"another btcperp command holds {self.path} (waited {wait_seconds:.0f}s)")
            time.sleep(poll)
        self._fh.seek(0)
        self._fh.truncate()
        self._fh.write(f"pid={os.getpid()}\n")
        self._fh.flush()

    def release(self) -> None:
        if self._fh is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        finally:
            self._fh.close()
            self._fh = None

    def __enter__(self) -> "FileLock":
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()
