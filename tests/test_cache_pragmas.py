"""SQLite durability/concurrency pragmas on PDFCache connections.

These guard the Windows commit-latency fix: rollback-journal mode costs
22ms per commit on Windows against 0.5ms on Linux, and the cost is the
per-transaction journal file rather than the fsync, so WAL is the fix and
`synchronous=NORMAL` under it removes the remaining per-commit flush.

`journal_mode` is persistent in the database file; `synchronous` and
`busy_timeout` are PER-CONNECTION and reset on every one of the ~42
connections PDFCache opens. Tests that open a second connection exist to
catch an implementation that sets them once at init and lets them lapse
everywhere else.
"""

import sqlite3


def test_fresh_cache_uses_wal_journal_mode(cache):
    """The cache database is created in (or migrated to) WAL mode."""
    with sqlite3.connect(cache.db_path) as conn:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode == "wal"


def test_connect_helper_sets_synchronous_normal(cache, real_fsync):
    """Every connection PDFCache opens runs at synchronous=NORMAL.

    Fails against an implementation that sets the pragma once in _init_db:
    synchronous is per-connection, so it would silently revert to FULL (2)
    on all ~42 other connections, which is where the Windows cost lives.
    """
    with cache._connect() as conn:
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1


def test_connect_helper_sets_busy_timeout(cache, real_fsync):
    """Every connection gets a non-zero busy_timeout.

    Without it a concurrent reader gets SQLITE_BUSY as soon as the default
    busy handler gives up. Also per-connection, same trap as above.
    """
    with cache._connect() as conn:
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] > 0


def test_cache_operations_open_no_bare_connections(cache, monkeypatch):
    """No code path bypasses _connect().

    The pragmas only hold if every one of the cache's ~42 connect sites
    routes through the helper. A bare sqlite3.connect anywhere reverts to
    synchronous=FULL for that operation, which is exactly the Windows cost
    this change removes, and nothing about the result would look wrong.
    """
    import pdf_mcp.cache as cache_mod

    raw_calls = []
    helper_calls = []

    real_connect = cache_mod.sqlite3.connect
    real_helper = type(cache)._connect

    def spy_connect(*args, **kwargs):
        raw_calls.append(args[0] if args else None)
        return real_connect(*args, **kwargs)

    def spy_helper(self):
        helper_calls.append(self.db_path)
        return real_helper(self)

    monkeypatch.setattr(cache_mod.sqlite3, "connect", spy_connect)
    monkeypatch.setattr(type(cache), "_connect", spy_helper)

    cache.get_stats()
    cache.clear_expired()

    assert raw_calls, "expected the operations to open at least one connection"
    assert len(raw_calls) == len(helper_calls), (
        f"{len(raw_calls) - len(helper_calls)} connection(s) bypassed "
        f"_connect() and ran without the durability pragmas"
    )


def test_cache_size_accounts_for_wal_sidecars(cache, sample_pdf):
    """cache_size_bytes counts the -wal and -shm files.

    WAL moves recently-committed data out of cache.db and into the sidecar
    until a checkpoint. Sizing only cache.db would under-report the cache
    by however much is still in the log, which is what pdf_cache_stats
    shows a user.
    """
    for i in range(200):
        cache.save_page_text(sample_pdf, i, f"page {i} " + "x" * 4000)

    wal = cache.db_path.with_name(cache.db_path.name + "-wal")
    assert wal.exists() and wal.stat().st_size > 0, "expected an active WAL"

    on_disk = sum(
        f.stat().st_size for f in cache.cache_dir.glob("cache.db*") if f.is_file()
    )
    assert cache.get_stats()["cache_size_bytes"] >= on_disk


def _refuse_wal(monkeypatch, cache_mod):
    """Make PRAGMA journal_mode=wal report failure, as a network FS does.

    Mocked because the condition needs a filesystem without shared-memory
    support, which a test cannot create. Only the one pragma is intercepted;
    every other statement runs against real SQLite.
    """
    real_connect = cache_mod.sqlite3.connect

    class _Conn:
        def __init__(self, inner):
            self._inner = inner

        def execute(self, sql, *a, **kw):
            if "journal_mode=wal" in sql.replace(" ", ""):
                return self._inner.execute("PRAGMA journal_mode=delete")
            return self._inner.execute(sql, *a, **kw)

        def __getattr__(self, name):
            return getattr(self._inner, name)

        def __enter__(self):
            self._inner.__enter__()
            return self

        def __exit__(self, *exc):
            return self._inner.__exit__(*exc)

    monkeypatch.setattr(
        cache_mod.sqlite3, "connect", lambda *a, **kw: _Conn(real_connect(*a, **kw))
    )


def test_cache_works_when_filesystem_refuses_wal(temp_cache_dir, monkeypatch):
    """A filesystem that refuses WAL degrades to rollback, it does not abort.

    cache_dir is env-overridable, so a network mount is reachable in
    production. A performance pragma must never be able to fail startup.
    """
    import pdf_mcp.cache as cache_mod

    _refuse_wal(monkeypatch, cache_mod)
    c = cache_mod.PDFCache(cache_dir=temp_cache_dir)

    assert c.journal_mode == "delete"
    c.get_stats()  # still functional


def test_synchronous_stays_full_without_wal(temp_cache_dir, monkeypatch, real_fsync):
    """synchronous=NORMAL is only safe under WAL.

    Under WAL, NORMAL loses at most recent transactions on an OS crash. Under
    a rollback journal it drops the guarantee that protects the journal write
    ordering, risking a corrupt database rather than a stale one. So the
    relaxation must be conditional on WAL actually being in force.
    """
    import pdf_mcp.cache as cache_mod

    _refuse_wal(monkeypatch, cache_mod)
    c = cache_mod.PDFCache(cache_dir=temp_cache_dir)

    with c._connect() as conn:
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2  # FULL


def test_existing_rollback_cache_migrates_to_wal(temp_cache_dir, monkeypatch):
    """An existing pre-WAL cache.db is migrated in place on next open.

    Users upgrading pdf-mcp already have a rollback-mode cache.db. They must
    get the fix without deleting their cache, and without re-extracting.
    """
    import pdf_mcp.cache as cache_mod

    _refuse_wal(monkeypatch, cache_mod)
    old = cache_mod.PDFCache(cache_dir=temp_cache_dir)
    assert old.journal_mode == "delete"
    monkeypatch.undo()

    migrated = cache_mod.PDFCache(cache_dir=temp_cache_dir)
    assert migrated.journal_mode == "wal"
