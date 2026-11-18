"""An OS-owned file lock. Exiting the process releases the lock."""
import errno
import os
import time
from contextlib import contextmanager


@contextmanager
def file_lock(path, timeout=0):
    if os.name == "nt":
        import msvcrt
    else:
        import fcntl

    deadline = time.monotonic() + timeout
    with open(path, "a+b") as stream:
        while True:
            try:
                stream.seek(0)
                if os.name == "nt":
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as error:
                if error.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                    raise
                if time.monotonic() >= deadline:
                    raise TimeoutError("lock in use: {}".format(path)) from error
                time.sleep(0.1)
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)
