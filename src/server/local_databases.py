"""Read a SQLite database a running application is holding open.

Every browser keeps its state in SQLite and keeps a write lock on it for as
long as it is running — and the browser IS running, because the person is
using it right now. Opening the live file read-only is not enough: a database
mid-transaction reports as locked or, worse, as malformed.

So the file is copied aside, together with its write-ahead log, and the copy
is what gets read. The copy is deleted immediately afterwards. Every module
that reads a browser's own storage goes through here, so the locking rule
lives in one place.
"""

from __future__ import annotations

import shutil
import sqlite3
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

# The files SQLite keeps beside a database; a copy without them can be missing
# the most recent writes, which for a history database is the last hour of
# browsing — exactly the part that matters most.
SQLITE_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")


class LocalDatabaseError(Exception):
    """A local database could not be read; the message is owner-safe."""


@contextmanager
def opened_copy(database_path: Path) -> Iterator[sqlite3.Connection]:
    """Yield a connection to a throwaway copy of a database, then clean up.

    Raises:
        LocalDatabaseError: when the file is missing, cannot be copied (a
            permission the operating system withholds, which on macOS means
            Full Disk Access has not been granted), or is not a database.
    """
    path = Path(database_path)
    if not path.is_file():
        raise LocalDatabaseError(f"No database at {path}.")
    temporary_directory = Path(tempfile.mkdtemp(prefix="neuralnexus-db-"))
    connection: sqlite3.Connection | None = None
    try:
        copied = temporary_directory / path.name
        try:
            shutil.copy2(path, copied)
            for suffix in SQLITE_SIDECAR_SUFFIXES:
                sidecar = path.with_name(path.name + suffix)
                if sidecar.is_file():
                    shutil.copy2(sidecar, copied.with_name(copied.name + suffix))
        except PermissionError as permission_error:
            raise LocalDatabaseError(
                f"{path} could not be read: {permission_error}. On macOS, grant "
                "the app Full Disk Access in System Settings → Privacy & "
                "Security; on Windows, close the browser and try again."
            ) from permission_error
        except OSError as copy_error:
            raise LocalDatabaseError(f"{path} could not be read: {copy_error}") from copy_error
        try:
            connection = sqlite3.connect(str(copied))
            connection.row_factory = sqlite3.Row
        except sqlite3.Error as database_error:
            raise LocalDatabaseError(
                f"{path} could not be opened: {database_error}"
            ) from database_error
        yield connection
    finally:
        if connection is not None:
            connection.close()
        shutil.rmtree(temporary_directory, ignore_errors=True)


def table_exists(connection: sqlite3.Connection, table_name: str) -> bool:
    """Whether a table is present, so a browser version change reads as empty."""
    row = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table_name,)
    ).fetchone()
    return row is not None
