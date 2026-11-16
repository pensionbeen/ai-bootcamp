"""같은 입력의 동시 CLI 실행이 결과 파일을 서로 덮거나 지우지 않게 합니다."""

import errno
import os
from contextlib import contextmanager
from pathlib import Path


class RequestInProgress(RuntimeError):
    pass


@contextmanager
def request_lock(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                raise RequestInProgress("request_already_running") from None
            raise
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    # 잠금 파일은 남깁니다. 삭제하면 다른 프로세스가 서로 다른 파일을 잠글 수 있습니다.
