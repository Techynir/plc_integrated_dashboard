from app.bootstrap import MIGRATIONS_DIR, split_sql


def test_split_sql_handles_comments_and_multiline():
    sql = """
    -- a comment; with semicolon
    CREATE TABLE a (x int);
    SELECT f('a',
             b => 1);  -- trailing
    """
    assert split_sql(sql) == ["CREATE TABLE a (x int)", "SELECT f('a',\n             b => 1)"]


def test_initial_migration_splits_into_statements():
    statements = split_sql((MIGRATIONS_DIR / "001_init.sql").read_text())
    assert statements[0] == "CREATE EXTENSION IF NOT EXISTS timescaledb"
    assert any("CREATE MATERIALIZED VIEW IF NOT EXISTS telemetry_1h" in s for s in statements)
    assert all(";" not in s for s in statements)
