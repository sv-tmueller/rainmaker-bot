from rainmaker.store.db import connect, init_schema
from rainmaker.store.migrate import apply_migrations


def test_migration_adds_predictions_bucket_column():
    conn = connect(":memory:")
    init_schema(conn)
    conn.execute(
        "INSERT INTO predictions (run_id, market_id, bucket, p_win, edge, recommended, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (None, None, "70-71°F", 0.9, 0.1, 1, "2026-06-04T00:00:00Z"),
    )
    conn.commit()
    row = conn.execute("SELECT bucket FROM predictions").fetchone()
    conn.close()
    assert row["bucket"] == "70-71°F"


def test_apply_migrations_is_idempotent():
    conn = connect(":memory:")
    init_schema(conn)
    apply_migrations(conn)  # second pass must not error
    n = conn.execute("SELECT count(*) AS n FROM schema_migrations").fetchone()["n"]
    conn.close()
    assert n == 1
