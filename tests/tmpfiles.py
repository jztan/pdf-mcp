"""Temp-file helpers shared by the fixtures and tests.

Lives outside conftest so tests can import it without importing the
conftest module itself, which pytest loads by its own machinery.
"""

import os


def unlink_quietly(path: "str | os.PathLike[str]") -> None:
    """Delete a temp fixture file, tolerating Windows' open-file lock.

    Windows refuses to delete a file another handle still has open
    (WinError 32). A PDF the test opened may still be held by a reader
    that has not been garbage collected, and it produced 56 teardown
    errors on the first Windows CI run. The file is a temp fixture the
    OS reclaims anyway, so failing to unlink it must not fail the test
    that already passed. POSIX is unaffected: unlink there succeeds even
    with the file open.
    """
    try:
        os.unlink(path)
    except (PermissionError, FileNotFoundError, OSError):
        pass
