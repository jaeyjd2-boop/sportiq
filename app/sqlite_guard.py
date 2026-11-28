import contextlib
import os
import random
import sqlite3
import threading
import time
from typing import Any, Callable, Iterator, TypeVar

T = TypeVar("T")

DEFAULT_TIMEOUT_SEC = float(os.environ.get("SPORTIQ_SQLITE_TIMEOUT_SEC", "30.0"))
DEFAULT_BUSY_TIMEOUT_MS = int(os.environ.get("SPORTIQ_SQLITE_BUSY_TIMEOUT_MS", "30000"))
DEFAULT_MAX_RETRIES = int(os.environ.get("SPORTIQ_SQLITE_MAX_RETRIES", "6"))

_ORIG_CONNECT = sqlite3.connect
_GUARD_LOCK = threading.Lock()
_GUARD_INSTALLED = False
_WAL_INITIALIZED_DBS: set = set()
_WAL_INIT_LOCK = threading.Lock()

def is_sqlite_lock_error(exc: BaseException) -> bool:
    if not isinstance(exc, sqlite3.OperationalError):
        return False
    msg = str(exc).lower()
    return (
        "database is locked" in msg
        or "database table is locked" in msg
        or "database schema is locked" in msg
        or "database is busy" in msg
        or "cannot commit transaction" in msg and "sql statements in progress" not in msg
    )


def _backoff_sleep(attempt: int, base_delay: float = 0.04, max_delay: float = 1.2) -> None:
    delay = min(max_delay, base_delay * (2 ** attempt))
    jitter = random.uniform(0.005, max(0.015, delay * 0.35))
    time.sleep(delay + jitter)


def run_with_db_retry(
    fn: Callable[[], T],
    max_attempts: int = DEFAULT_MAX_RETRIES,
    base_delay: float = 0.04,
) -> T:
    attempts = max(1, int(max_attempts))
    last_exc: Exception | None = None
    for attempt in range(attempts):
        try:
            return fn()
        except sqlite3.OperationalError as exc:
            last_exc = exc
            if not is_sqlite_lock_error(exc) or attempt >= attempts - 1:
                raise
            _backoff_sleep(attempt, base_delay=base_delay)
    if last_exc is not None:
        raise last_exc
    return fn()


class SportIQCursor(sqlite3.Cursor):
    def execute(self, sql: str, parameters: Any = (), /) -> "SportIQCursor":
        was_in_tx = self.connection.in_transaction
        for attempt in range(DEFAULT_MAX_RETRIES):
            try:
                return super().execute(sql, parameters)
            except sqlite3.OperationalError as exc:
                if not is_sqlite_lock_error(exc) or attempt >= DEFAULT_MAX_RETRIES - 1:
                    raise
                if not was_in_tx:
                    with contextlib.suppress(Exception):
                        sqlite3.Connection.rollback(self.connection)
                _backoff_sleep(attempt)
        return super().execute(sql, parameters)

    def executemany(self, sql: str, seq_of_parameters: Any, /) -> "SportIQCursor":
        parameters_list = list(seq_of_parameters)
        was_in_tx = self.connection.in_transaction
        for attempt in range(DEFAULT_MAX_RETRIES):
            try:
                return super().executemany(sql, parameters_list)
            except sqlite3.OperationalError as exc:
                if not is_sqlite_lock_error(exc) or attempt >= DEFAULT_MAX_RETRIES - 1:
                    raise
                if not was_in_tx:
                    with contextlib.suppress(Exception):
                        sqlite3.Connection.rollback(self.connection)
                _backoff_sleep(attempt)
        return super().executemany(sql, parameters_list)


class SportIQConnection(sqlite3.Connection):
    def cursor(self, factory: Any = SportIQCursor) -> sqlite3.Cursor:
        if factory is None:
            factory = SportIQCursor
        return super().cursor(factory)

    def execute(self, sql: str, parameters: Any = (), /) -> sqlite3.Cursor:
        cur = self.cursor(SportIQCursor)
        return cur.execute(sql, parameters)

    def executemany(self, sql: str, seq_of_parameters: Any, /) -> sqlite3.Cursor:
        cur = self.cursor(SportIQCursor)
        return cur.executemany(sql, seq_of_parameters)

    def executescript(self, sql_script: str, /) -> sqlite3.Cursor:
        for attempt in range(DEFAULT_MAX_RETRIES):
            try:
                return super().executescript(sql_script)
            except sqlite3.OperationalError as exc:
                if not is_sqlite_lock_error(exc) or attempt >= DEFAULT_MAX_RETRIES - 1:
                    raise
                _backoff_sleep(attempt)
        return super().executescript(sql_script)

    def commit(self) -> None:
        for attempt in range(DEFAULT_MAX_RETRIES):
            try:
                super().commit()
                return
            except sqlite3.OperationalError as exc:
                if not is_sqlite_lock_error(exc) or attempt >= DEFAULT_MAX_RETRIES - 1:
                    raise
                _backoff_sleep(attempt)


def _apply_pragmas(conn: sqlite3.Connection, db_target: str) -> None:
    for attempt in range(DEFAULT_MAX_RETRIES):
        try:
            sqlite3.Connection.execute(conn, f"PRAGMA busy_timeout = {DEFAULT_BUSY_TIMEOUT_MS};")
            if db_target and db_target != ":memory:" and not db_target.startswith("file::memory:"):
                norm_path = os.path.abspath(db_target)
                if norm_path not in _WAL_INITIALIZED_DBS:
                    with _WAL_INIT_LOCK:
                     if norm_path not in _WAL_INITIALIZED_DBS:
                         try:
                            sqlite3.Connection.execute(conn, "PRAGMA journal_mode = WAL;")
                           sqlite3.Connection.execute(conn, "PRAGMA synchronous = NORMAL;")
                                _WAL_INITIALIZED_DBS.add(norm_path)
                            except sqlite3.OperationalError:
                                pass
                        
                sqlite3.Connection.execute(conn, "PRAGMA wal_autocheckpoint = 1000;")
            sqlite3.Connection.execute(conn, "PRAGMA temp_store = MEMORY;")
            return
        except sqlite3.OperationalError as exc:
            if not is_sqlite_lock_error(exc) or attempt >= DEFAULT_MAX_RETRIES - 1:
                return
            _backoff_sleep(attempt)


def resilient_connect(database: Any, *args: Any, **kwargs: Any) -> sqlite3.Connection:
    raw_timeout = kwargs.get("timeout", DEFAULT_TIMEOUT_SEC)
    try:
        effective_timeout = max(float(raw_timeout or 0.0), DEFAULT_TIMEOUT_SEC)
    except (TypeError, ValueError):
        effective_timeout = DEFAULT_TIMEOUT_SEC
    kwargs["timeout"] = effective_timeout
    kwargs["check_same_thread"] = False
    kwargs.setdefault("factory", SportIQConnection)

    last_exc: Exception | None = None
    conn: sqlite3.Connection | None = None
    for attempt in range(DEFAULT_MAX_RETRIES):
        try:
            conn = _ORIG_CONNECT(database, *args, **kwargs)
            break
        except sqlite3.OperationalError as exc:
            last_exc = exc
            if not is_sqlite_lock_error(exc) or attempt >= DEFAULT_MAX_RETRIES - 1:
                raise
            _backoff_sleep(attempt)
    if conn is None:
        if last_exc is not None:
            raise last_exc
        conn = _ORIG_CONNECT(database, *args, **kwargs)

    _apply_pragmas(conn, os.fspath(database) if isinstance(database, (str, bytes, os.PathLike)) else "")
    return conn


@contextlib.contextmanager
def managed_sqlite_conn(
    db_path: str,
    *,
    write: bool = False,
    row_factory: Any = sqlite3.Row,
) -> Iterator[sqlite3.Connection]:
    conn = resilient_connect(
        db_path,
        timeout=DEFAULT_TIMEOUT_SEC,
        check_same_thread=False,
        isolation_level=None if write else "",
    )
    if row_factory is not None:
        conn.row_factory = row_factory
    try:
        if write:
            conn.execute("BEGIN IMMEDIATE")
        yield conn
        if conn.in_transaction:
            conn.commit()
    except Exception:
        if conn.in_transaction:
            with contextlib.suppress(Exception):
                conn.rollback()
        raise
    finally:
        with contextlib.suppress(Exception):
            conn.close()


def install_sqlite_guard() -> None:
    global _GUARD_INSTALLED
    with _GUARD_LOCK:
        if _GUARD_INSTALLED:
            return
        sqlite3.connect = resilient_connect  # type: ignore[assignment]
        _GUARD_INSTALLED = True
