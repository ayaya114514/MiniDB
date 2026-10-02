"""Triggers, compared with SQLite (both file formats)."""

import sqlite3
from contextlib import closing

import pytest

from minidb.database import Database
from sqlcompare import Pair

SCHEMA = "SELECT type, name, tbl_name, rootpage, sql FROM sqlite_master WHERE type = 'trigger' ORDER BY name"


@pytest.fixture(params=[None, "sqlite"])
def pair(request, tmp_path):
    path = str(tmp_path / "db") if request.param else None
    return Pair(path, check_messages=True, format=request.param)


def run(pair, script):
    for sql in script:
        pair.run(sql)


def test_firing_order_and_new_values(pair):
    """Newest trigger first; BEFORE INSERT sees the values with their
    affinities and row id -1 when unknown; UPDATE OF, WHEN; changes()."""
    run(pair, [
        "CREATE TABLE t (id INTEGER PRIMARY KEY, a REAL, b TEXT COLLATE nocase, c)", "CREATE TABLE log (x)",
        "CREATE TRIGGER t1 BEFORE INSERT ON t BEGIN INSERT INTO log VALUES ('t1:' || typeof(new.a) || ':' "
        "|| new.rowid || ':' || quote(new.id) || ':' || quote(new.a) || ':' || quote(new.b)); END",
        "CREATE TRIGGER t2 BEFORE INSERT ON t BEGIN INSERT INTO log VALUES ('t2'); END",
        "CREATE TRIGGER t3 AFTER INSERT ON t BEGIN INSERT INTO log VALUES ('t3:' || new.rowid || ':' "
        "|| last_insert_rowid() || ':' || changes()); END",
        "CREATE TRIGGER t4 AFTER INSERT ON t FOR EACH ROW WHEN new.b = 'X' BEGIN INSERT INTO log VALUES ('t4'); END",
        "INSERT INTO t (a, b, c) VALUES ('5', 7, 1)", "INSERT INTO t VALUES (10, '1.0', 'x', NULL), (NULL, 2, 'y', 3)",
        "SELECT * FROM log", "SELECT changes(), total_changes(), last_insert_rowid()",
        "CREATE TRIGGER u1 AFTER UPDATE OF b, c ON t WHEN new.a > 3 BEGIN INSERT INTO log VALUES "
        "(old.a || '>' || new.a || ',' || quote(old.b) || '>' || quote(new.b) || ' ' || (new.b = 'Y')); END",
        "CREATE TRIGGER u2 BEFORE UPDATE ON t BEGIN INSERT INTO log VALUES ('u2 ' || quote(new.a) || quote(new.id)); END",
        "UPDATE t SET a = a + 10", "UPDATE t SET b = upper(b), id = id + 100 WHERE id > 1", "SELECT * FROM log",
        "SELECT changes(), total_changes()", "SELECT * FROM t",
        "CREATE TRIGGER d1 AFTER DELETE ON t BEGIN INSERT INTO log VALUES ('d1 ' || old.id || ' ' || old.b); END",
        "DELETE FROM t WHERE a > 11", "DELETE FROM t", "SELECT * FROM log", "SELECT changes(), total_changes()",
        SCHEMA,
    ])


def test_recursion_and_depth(pair):
    run(pair, [
        "CREATE TABLE t (a)", "CREATE TRIGGER r AFTER INSERT ON t WHEN new.a < 5 BEGIN INSERT INTO t VALUES (new.a + 1); END",
        "INSERT INTO t VALUES (1)", "SELECT * FROM t", "PRAGMA recursive_triggers = 1", "INSERT INTO t VALUES (1)",
        "SELECT * FROM t", "SELECT total_changes()",
        "CREATE TABLE u (a)", "CREATE TRIGGER r2 AFTER INSERT ON u BEGIN INSERT INTO u VALUES (new.a + 1); END",
        "INSERT INTO u VALUES (1)", "SELECT count(*) FROM u",
        # Two triggers firing each other: off, each runs once per chain.
        "PRAGMA recursive_triggers = 0", "CREATE TABLE p (a)", "CREATE TABLE q (a)",
        "CREATE TRIGGER pq AFTER INSERT ON p WHEN new.a < 6 BEGIN INSERT INTO q VALUES (new.a + 1); END",
        "CREATE TRIGGER qp AFTER INSERT ON q WHEN new.a < 6 BEGIN INSERT INTO p VALUES (new.a + 1); END",
        "INSERT INTO p VALUES (1)", "SELECT * FROM p", "SELECT * FROM q",
    ])


def test_raise(pair):
    run(pair, [
        "CREATE TABLE t (a)", "CREATE TABLE log (x)",
        "CREATE TRIGGER b BEFORE INSERT ON t BEGIN SELECT RAISE(IGNORE) WHERE new.a = 2; "
        "SELECT RAISE(ABORT, 'no threes ' || new.a) WHERE new.a = 3; SELECT RAISE(FAIL, 'fail4') WHERE new.a = 4; "
        "SELECT RAISE(ROLLBACK, 'rb5') WHERE new.a = 5; SELECT RAISE(ABORT, NULL) WHERE new.a = 6; "
        "SELECT RAISE(FAIL, 7.5) WHERE new.a = 7; END",
        "INSERT INTO t VALUES (1), (2), (1)", "SELECT * FROM t", "SELECT changes()",
        "INSERT INTO t VALUES (1), (3)", "SELECT * FROM t", "INSERT INTO t VALUES (8), (4), (8)", "SELECT * FROM t",
        "INSERT INTO t VALUES (6)", "INSERT INTO t VALUES (7)",
        "BEGIN", "INSERT INTO t VALUES (9)", "INSERT INTO t VALUES (5)", "SELECT * FROM t", "COMMIT",
        "SELECT RAISE(ABORT, 'x')", "SELECT RAISE(IGNORE) FROM t WHERE 0",
        # RAISE(IGNORE) in a nested trigger abandons only that one (and its row).
        "CREATE TABLE u (x)",
        "CREATE TRIGGER n1 AFTER INSERT ON u BEGIN INSERT INTO log VALUES ('u ' || new.x); "
        "SELECT RAISE(IGNORE) WHERE new.x = 2; INSERT INTO log VALUES ('after ' || new.x); END",
        "CREATE TRIGGER n0 AFTER INSERT ON t WHEN new.a > 10 BEGIN INSERT INTO u VALUES (1), (2), (3); "
        "INSERT INTO log VALUES ('t done'); END",
        "INSERT INTO t VALUES (11)", "SELECT * FROM log", "SELECT * FROM u", "SELECT changes(), total_changes()",
        "UPDATE t SET a = RAISE(IGNORE)",
    ])


def test_errors(pair):
    run(pair, [
        "CREATE TABLE t (a, b)", "CREATE VIEW v AS SELECT * FROM t", "CREATE TABLE u (x)", "CREATE INDEX ui ON u (x)",
        "CREATE TRIGGER x INSTEAD OF INSERT ON t BEGIN SELECT 1; END",
        "CREATE TRIGGER x BEFORE INSERT ON v BEGIN SELECT 1; END", "CREATE TRIGGER x AFTER DELETE ON v BEGIN SELECT 1; END",
        "CREATE TRIGGER x AFTER INSERT ON nosuch BEGIN SELECT 1; END",
        "CREATE TRIGGER x AFTER INSERT ON sqlite_master BEGIN SELECT 1; END",
        "CREATE TRIGGER sqlite_x AFTER INSERT ON t BEGIN SELECT 1; END",
        "CREATE TRIGGER x AFTER INSERT ON t BEGIN INSERT INTO main.u VALUES (1); END",
        "CREATE TRIGGER x AFTER INSERT ON t BEGIN UPDATE u INDEXED BY ui SET x = 1; END",
        "CREATE TRIGGER x AFTER INSERT ON t BEGIN DELETE FROM u NOT INDEXED; END",
        "CREATE TRIGGER x AFTER INSERT ON t BEGIN INSERT INTO u VALUES (1) RETURNING x; END",
        "CREATE TRIGGER x AFTER INSERT ON t WHEN ? BEGIN SELECT 1; END",
        "CREATE TRIGGER x AFTER INSERT ON t BEGIN SELECT ?; END",
        "CREATE TRIGGER x AFTER INSERT ON t BEGIN INSERT INTO nosuch VALUES (1); END",
        "INSERT INTO t VALUES (1, 2)", "DROP TRIGGER x", "DROP TRIGGER x", "DROP TRIGGER IF EXISTS x",
        "CREATE TRIGGER y AFTER INSERT ON t BEGIN SELECT nosuchcol FROM t; END", "INSERT INTO t VALUES (1, 2)",
        "CREATE TRIGGER IF NOT EXISTS y AFTER INSERT ON t BEGIN SELECT 1; END",
        "CREATE TRIGGER y AFTER INSERT ON t BEGIN SELECT 1; END", "DROP TRIGGER main.y",
        "CREATE TRIGGER z1 AFTER INSERT ON t BEGIN SELECT old.a; END", "INSERT INTO t VALUES (1, 2)", "DROP TRIGGER z1",
        "CREATE TRIGGER z2 AFTER DELETE ON t BEGIN SELECT new.a; END", "DELETE FROM t", "DROP TRIGGER z2",
        "CREATE TRIGGER z3 AFTER INSERT ON t WHEN a > 0 BEGIN SELECT 1; END", "INSERT INTO t VALUES (1, 2)",
        "DROP TRIGGER z3", "CREATE TRIGGER z4 AFTER INSERT ON t BEGIN SELECT new.nosuch; END",
        "INSERT INTO t VALUES (1, 2)", "DROP TRIGGER z4",
        # Triggers have a namespace of their own.
        "CREATE TRIGGER t AFTER INSERT ON t BEGIN SELECT 1; END", "CREATE TRIGGER ui AFTER INSERT ON t BEGIN SELECT 1; END",
        "CREATE TABLE ui2 (a)", "CREATE TRIGGER \"x 13\" AFTER DELETE ON \"t\" BEGIN SELECT 1 ; END ;",
        "CREATE  TRIGGER IF NOT EXISTS  main.x3 AFTER INSERT ON main.t FOR EACH ROW BEGIN SELECT 1; END",
        SCHEMA, "DROP TABLE t", SCHEMA, "SELECT name FROM sqlite_master ORDER BY name",
    ])


def test_before_triggers_change_the_row(pair):
    """After BEFORE UPDATE / DELETE triggers SQLite looks at the row again:
    gone, it is skipped; changed, the columns the UPDATE does not set take
    the new values (SQLite's trigger1-18.0); OLD stays what it was."""
    run(pair, [
        "CREATE TABLE t (id INTEGER PRIMARY KEY, a, b)", "CREATE TABLE log (x)",
        "INSERT INTO t VALUES (1, 1, 1), (2, 2, 2), (3, 3, 3)",
        "CREATE TRIGGER bu BEFORE UPDATE ON t BEGIN UPDATE t SET b = b * 10 WHERE id = new.id; "
        "DELETE FROM t WHERE id = 3 AND new.id = 2; END",
        "CREATE TRIGGER au AFTER UPDATE ON t BEGIN INSERT INTO log VALUES (old.b || '>' || new.b || ' ' || new.a); END",
        "UPDATE t SET a = a + 100", "SELECT * FROM t", "SELECT * FROM log", "SELECT changes()",
        "CREATE TRIGGER bd BEFORE DELETE ON t BEGIN DELETE FROM t WHERE id = old.id + 1; END",
        "CREATE TRIGGER ad AFTER DELETE ON t BEGIN INSERT INTO log VALUES ('gone ' || old.id); END",
        "INSERT INTO t VALUES (5, 5, 5), (6, 6, 6), (7, 7, 7)", "DELETE FROM t WHERE id >= 5", "SELECT * FROM t",
        "SELECT * FROM log", "SELECT changes(), total_changes()",
    ])


def test_interplay(pair):
    """Triggers with foreign key actions, REPLACE (delete triggers only with
    recursive_triggers), upserts (UPDATE triggers), and the outer statement's
    OR clause, which overrides the trigger's own."""
    run(pair, [
        "PRAGMA foreign_keys = ON", "CREATE TABLE p (id INTEGER PRIMARY KEY)",
        "CREATE TABLE c (id INTEGER PRIMARY KEY, r REFERENCES p ON DELETE CASCADE ON UPDATE SET NULL)",
        "CREATE TABLE log (x)",
        "CREATE TRIGGER cd AFTER DELETE ON c BEGIN INSERT INTO log VALUES ('c del ' || old.id); END",
        "CREATE TRIGGER cu AFTER UPDATE ON c BEGIN INSERT INTO log VALUES ('c upd ' || quote(new.r)); END",
        "INSERT INTO p VALUES (1), (2)", "INSERT INTO c VALUES (10, 1), (11, 1), (12, 2)",
        "DELETE FROM p WHERE id = 1", "UPDATE p SET id = 3", "SELECT * FROM log", "SELECT changes(), total_changes()",
        "CREATE TABLE k (a PRIMARY KEY, b)",
        "CREATE TRIGGER kd AFTER DELETE ON k BEGIN INSERT INTO log VALUES ('k del ' || old.a || old.b); END",
        "CREATE TRIGGER ku AFTER UPDATE ON k BEGIN INSERT INTO log VALUES ('k upd ' || new.b); END",
        "CREATE TRIGGER ki BEFORE INSERT ON k BEGIN INSERT INTO log VALUES ('k ins ' || new.b); END",
        "INSERT INTO k VALUES (1, 'a')", "REPLACE INTO k VALUES (1, 'b')", "PRAGMA recursive_triggers = 1",
        "REPLACE INTO k VALUES (1, 'c')", "INSERT INTO k VALUES (1, 'd') ON CONFLICT (a) DO UPDATE SET b = 'e'",
        "INSERT INTO k VALUES (1, 'f') ON CONFLICT DO NOTHING", "SELECT * FROM log", "SELECT * FROM k",
        "CREATE TABLE o (a UNIQUE)", "CREATE TABLE src (a)",
        "CREATE TRIGGER so AFTER INSERT ON src BEGIN INSERT INTO o VALUES (new.a); INSERT INTO log VALUES ('so ' || new.a); END",
        "INSERT INTO src VALUES (1)", "INSERT INTO src VALUES (1)", "INSERT OR IGNORE INTO src VALUES (1)",
        "INSERT OR REPLACE INTO src VALUES (1)", "SELECT * FROM o", "SELECT count(*) FROM src",
        "SELECT * FROM log ORDER BY rowid DESC LIMIT 3",
    ])


def test_instead_of_triggers(pair):
    run(pair, [
        "CREATE TABLE t (id INTEGER PRIMARY KEY, a REAL, b TEXT)", "CREATE TABLE log (x)",
        "INSERT INTO t VALUES (1, 1.5, 'x'), (2, 2.5, 'y'), (3, 3.5, 'z')",
        "CREATE VIEW v AS SELECT id, a * 2 AS dbl, b FROM t", "CREATE VIEW w (p, q) AS SELECT a, b FROM t",
        "CREATE TRIGGER vi INSTEAD OF INSERT ON v BEGIN INSERT INTO log VALUES ('ins ' || quote(new.id) || ',' "
        "|| quote(new.dbl) || ',' || quote(new.b)); END",
        "CREATE TRIGGER vu INSTEAD OF UPDATE ON v BEGIN INSERT INTO log VALUES ('upd ' || old.id || ':' "
        "|| quote(old.dbl) || '->' || quote(new.dbl) || ' b ' || old.b || '->' || quote(new.b)); END",
        "CREATE TRIGGER vd INSTEAD OF DELETE ON v BEGIN INSERT INTO log VALUES ('del ' || old.id); END",
        "INSERT INTO v VALUES (7, '8', 9)", "SELECT changes(), total_changes()", "INSERT INTO v (b) VALUES ('q')",
        "INSERT INTO v SELECT * FROM v WHERE id < 3", "UPDATE v SET dbl = dbl + 1 WHERE id >= 2", "UPDATE v SET b = 'B'",
        "UPDATE OR IGNORE v SET b = 'C' WHERE b = 'x'", "DELETE FROM v WHERE dbl > 4", "SELECT changes()",
        "DELETE FROM v", "SELECT * FROM log", "INSERT INTO w VALUES (1, 2)", "UPDATE w SET p = 1", "DELETE FROM w",
        "CREATE TRIGGER wi INSTEAD OF INSERT ON w BEGIN SELECT RAISE(IGNORE) WHERE new.p = 0; "
        "INSERT INTO t (a, b) VALUES (new.p, new.q); END",
        "INSERT INTO w VALUES (0, 'zero'), (5, 'five')", "SELECT changes(), last_insert_rowid()", "SELECT * FROM t",
        "INSERT INTO v VALUES (1) RETURNING *", "UPDATE v SET nosuch = 1", "UPDATE v SET b = 1 WHERE nosuch",
        "INSERT INTO v (nosuch) VALUES (1)", "INSERT INTO v VALUES (1, 2)", "INSERT OR REPLACE INTO v VALUES (1, 2, 3)",
        "CREATE TRIGGER wr INSTEAD OF INSERT ON w BEGIN SELECT new.rowid; END", "INSERT INTO w VALUES (1, 1)",
        "DROP TRIGGER wr", "INSERT INTO v VALUES (1, 2, 3) RETURNING id", "UPDATE v SET b = 2 RETURNING *",
        "DELETE FROM v RETURNING b", "UPDATE v SET id = id + 100 WHERE id = 1",
        "SELECT * FROM log ORDER BY rowid DESC LIMIT 3", "DROP VIEW v", "SELECT name FROM sqlite_master ORDER BY name",
    ])


def test_alter_table_edits_triggers(pair):
    run(pair, [
        "CREATE TABLE t (a, b)", "CREATE TABLE log (x, y)", "CREATE TABLE other (p)",
        "CREATE TRIGGER tr1 AFTER UPDATE OF a ON t WHEN new.a > old.a BEGIN INSERT INTO log (x, y) "
        "SELECT new.a, t.b FROM t WHERE a = new.a; UPDATE other SET p = new.b; END",
        "CREATE TRIGGER tr2 AFTER INSERT ON other BEGIN DELETE FROM t WHERE a = new.p; "
        "INSERT INTO log VALUES ((SELECT count(*) FROM t), 0); UPDATE t SET a = 1, b = 2 WHERE b = new.p; END",
        "ALTER TABLE t RENAME TO \"T 2\"", SCHEMA, "ALTER TABLE \"T 2\" RENAME COLUMN a TO aa", SCHEMA,
        "ALTER TABLE \"T 2\" RENAME COLUMN b TO \"b b\"", SCHEMA, "ALTER TABLE log RENAME COLUMN x TO xx", SCHEMA,
        "ALTER TABLE other RENAME TO o2", SCHEMA, "ALTER TABLE \"T 2\" DROP COLUMN aa",
        "ALTER TABLE \"T 2\" DROP COLUMN \"b b\"",
        "ALTER TABLE \"T 2\" ADD COLUMN c", SCHEMA, "INSERT INTO o2 VALUES (1)", "UPDATE \"T 2\" SET aa = 5",
        "SELECT * FROM log",
        "CREATE TABLE q (m, n)", "CREATE TRIGGER tq AFTER INSERT ON q BEGIN SELECT new.n; END", "ALTER TABLE q DROP COLUMN n",
        "ALTER TABLE log DROP COLUMN y", "DROP TABLE log", SCHEMA, "INSERT INTO o2 VALUES (1)",
    ])


def test_triggers_survive_reopening(tmp_path):
    for fmt in (None, "sqlite"):
        path = str(tmp_path / f"db{fmt}")
        with Database(path, format=fmt) as db:
            db.execute("CREATE TABLE t (a)")
            db.execute("CREATE TABLE log (x)")
            db.execute("CREATE TRIGGER tr AFTER INSERT ON t BEGIN INSERT INTO log VALUES (new.a * 2); END")
            db.execute("CREATE TRIGGER gone AFTER INSERT ON t BEGIN SELECT 1; END")
            db.execute("BEGIN")
            db.execute("DROP TRIGGER gone")
            db.execute("ROLLBACK")
        with Database(path) as db:
            db.execute("INSERT INTO t VALUES (21)")
            assert db.execute("SELECT * FROM log") == [(42,)]
            assert [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")] == ["tr", "gone"]


def test_sqlite_runs_minidb_triggers_and_back(tmp_path):
    path = str(tmp_path / "db")
    with Database(path, format="sqlite") as db:
        db.execute("CREATE TABLE t (a)")
        db.execute("CREATE TABLE log (x)")
        db.execute("CREATE TRIGGER tr BEFORE INSERT ON t WHEN new.a > 0 BEGIN INSERT INTO log VALUES ('mini ' || new.a); END")
    with closing(sqlite3.connect(path)) as lite:
        lite.execute("INSERT INTO t VALUES (1)")
        lite.execute("CREATE TRIGGER tr2 AFTER DELETE ON t BEGIN INSERT INTO log VALUES ('lite ' || old.a); END")
        lite.commit()
        assert lite.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
    with Database(path) as db:
        db.execute("INSERT INTO t VALUES (2)")
        db.execute("DELETE FROM t WHERE a = 1")
        assert db.execute("SELECT * FROM log") == [("mini 1",), ("mini 2",), ("lite 1",)]
    with closing(sqlite3.connect(path)) as lite:
        assert lite.execute("PRAGMA integrity_check").fetchall() == [("ok",)]


def test_found_by_the_fuzzer(pair):
    """Corners SQLite's code generation decides, each found by tests/fuzz.py."""
    run(pair, [
        # Recursion is checked per trigger, whatever OR clause its program was compiled for.
        "CREATE TABLE t1 (id INTEGER PRIMARY KEY, c0 TEXT, c2 REAL NOT NULL)", "CREATE TABLE log (x)",
        "CREATE TRIGGER tr1 INSERT ON t1 BEGIN INSERT INTO log VALUES (quote(new.c0)); "
        "INSERT OR REPLACE INTO t1 (c0, id, c2) VALUES (NULL, 10, (-4 >> new.c0)); END",
        "INSERT INTO t1 VALUES (0, 3, 0)", "SELECT * FROM t1", "SELECT * FROM log",
        # NEW.x of an INTEGER PRIMARY KEY is the row id: INTEGER affinity.
        "CREATE TABLE t2 (id INTEGER PRIMARY KEY, c0 REAL, c1 TEXT)",
        "CREATE TRIGGER tr2 AFTER INSERT ON t2 BEGIN INSERT INTO log VALUES ((new.c1 BETWEEN new.c1 AND new.id) "
        "|| (new.c1 <= new.id) || (new.c0 = '1')); END",
        "INSERT INTO t2 VALUES (2, 1, 0)", "SELECT * FROM log",
        # After REPLACE ran DELETE triggers, uniqueness is checked again (one kept the row).
        "PRAGMA recursive_triggers = ON", "CREATE TABLE t3 (id INTEGER PRIMARY KEY, a UNIQUE)",
        "CREATE TRIGGER tr3 BEFORE DELETE ON t3 BEGIN SELECT RAISE(IGNORE) WHERE old.a = 'keep'; END",
        "INSERT INTO t3 VALUES (1, 'keep'), (2, 'go')", "REPLACE INTO t3 VALUES (1, 'x')",
        "REPLACE INTO t3 VALUES (5, 'go')", "SELECT * FROM t3", "PRAGMA recursive_triggers = OFF",
    ])


def test_foreign_keys_through_triggers(pair):
    run(pair, [
        "PRAGMA foreign_keys = ON",
        # isSetNullAction: the SET NULL action a BEFORE trigger's UPDATE compiled is the
        # last program before the INSERT's own check of t2's key, which is left out.
        "CREATE TABLE t0 (id INTEGER PRIMARY KEY, c0 FLOAT UNIQUE, c2 BLOB)",
        "CREATE TABLE t2 (c0 VARCHAR(5) NOT NULL REFERENCES t0(c0) ON UPDATE SET NULL, c1 TEXT)",
        "CREATE TRIGGER tr0 INSERT ON t2 WHEN 0 BEGIN UPDATE t0 SET c2 = 0, c0 = NULL WHERE t0.rowid = 21; END",
        "REPLACE INTO t2 VALUES (7, 1)", "SELECT * FROM t2",
        # An upsert whose UPDATE has triggers makes the INSERT a multi-row write:
        # its foreign keys look for t0's children, and find the mismatch.
        "CREATE TABLE p (c0, c2 REAL, c1 REAL)", "CREATE TABLE c (id INTEGER PRIMARY KEY, r REFERENCES p(c0))",
        "CREATE UNIQUE INDEX pi ON p (c0, c2)", "CREATE TRIGGER pu AFTER UPDATE ON p BEGIN SELECT 1; END",
        "INSERT INTO p (c2, c1, c0) VALUES (1, 2, 3) ON CONFLICT DO UPDATE SET c1 = 5",
        # Inside a trigger program a one-row INSERT into a parent looks for its children too.
        "CREATE TABLE q (a)", "CREATE TRIGGER qi AFTER INSERT ON q BEGIN INSERT INTO p (c0) VALUES (1); END",
        "INSERT INTO q VALUES (1)",
    ])


def test_statement_journal_with_triggers(pair):
    """With triggers a statement is a multi-row write; SQLite keeps a
    statement journal only if something in it may abort - else rows an
    error leaves (here a datatype mismatch, in a transaction) stay."""
    run(pair, [
        "CREATE TABLE t0 (id INTEGER PRIMARY KEY, c1)", "CREATE TABLE t1 (a)", "INSERT INTO t1 VALUES (1), (2)",
        "CREATE TRIGGER tr BEFORE INSERT ON t0 BEGIN DELETE FROM t1 WHERE t1.a = new.c1; END",
        "BEGIN", "INSERT OR IGNORE INTO t0 VALUES (1, 1), ('x', 2)", "SELECT * FROM t1", "SELECT * FROM t0",
        "CREATE TRIGGER tr2 BEFORE INSERT ON t0 BEGIN SELECT RAISE(ABORT, 'no') WHERE new.c1 = 99; END",
        "INSERT OR IGNORE INTO t0 VALUES (2, 2), ('y', 3)", "SELECT * FROM t1", "SELECT * FROM t0", "COMMIT",
    ])


def test_views_corners(pair):
    run(pair, [
        "CREATE TABLE t (a INTEGER, b TEXT)", "INSERT INTO t VALUES (1, 'x')", "CREATE TABLE log (x)",
        "CREATE VIEW v AS SELECT a, b, rowid AS r FROM t",
        "CREATE TRIGGER vd INSTEAD OF DELETE ON v BEGIN SELECT 1; END",
        # RETURNING with any trigger on the view: writable; NEW converted only with an INSTEAD OF trigger.
        "UPDATE v SET a = '5', b = 6, r = '7' RETURNING typeof(a), typeof(b), typeof(r)",
        "INSERT INTO v VALUES ('5', 6, '7') RETURNING typeof(a), typeof(b), typeof(r)",
        "UPDATE v SET a = 1", "INSERT INTO v VALUES (1, 2, 3)",
        "CREATE TRIGGER vu INSTEAD OF UPDATE ON v BEGIN INSERT INTO log VALUES (typeof(new.a) || typeof(new.r)); END",
        "CREATE TRIGGER vi INSTEAD OF INSERT ON v BEGIN INSERT INTO log VALUES (typeof(new.a) || typeof(new.r)); END",
        "UPDATE v SET a = '5', b = 6, r = '7' RETURNING typeof(a), typeof(b), typeof(r)",
        "INSERT INTO v VALUES ('5', 6, '7') RETURNING typeof(a), typeof(b), typeof(r)", "SELECT * FROM log",
        # A row id for a view is checked (for NEW, so only with an INSTEAD OF INSERT trigger) and ignored.
        "CREATE VIEW w AS SELECT a FROM t", "CREATE TRIGGER wd INSTEAD OF DELETE ON w BEGIN SELECT 1; END",
        "INSERT INTO w (rowid, a) VALUES ('x', 1) RETURNING *",
        "INSERT INTO v (rowid, a) VALUES (3, 1)", "INSERT INTO v (rowid, a) VALUES ('x', 1)",
        "INSERT INTO v (oid, a) VALUES (NULL, 1)", "SELECT * FROM log",
    ])


def test_total_changes_when_a_trigger_fails(pair):
    """A program's completed statements count in total_changes() even when a
    later one fails (SQLite's OP_ResetCount); the failing statement's rows
    do not.  Under FAIL the statement's own rows count, the row whose AFTER
    trigger failed included.  An upsert whose UPDATE a trigger skipped
    returns no row."""
    run(pair, [
        'CREATE TABLE w(x)',
        'CREATE TABLE t(x)',
        'CREATE TABLE a(x)',
        'CREATE TABLE n(x)',
        'CREATE TABLE u(x UNIQUE)',
        'CREATE TABLE m(x)',
        'CREATE VIEW v AS SELECT x FROM m',
        "CREATE TRIGGER vi INSTEAD OF INSERT ON v BEGIN INSERT INTO m VALUES (new.x); SELECT RAISE(FAIL, 'stop') WHERE new.x = 2; END",
        "CREATE TRIGGER ta BEFORE INSERT ON a BEGIN INSERT INTO m VALUES (new.x); SELECT RAISE(ABORT, 'stop') WHERE new.x = 2; END",
        'CREATE TRIGGER tn BEFORE INSERT ON n BEGIN INSERT INTO t VALUES (new.x); END',
        "CREATE TRIGGER tt BEFORE INSERT ON t BEGIN INSERT INTO m VALUES (new.x); INSERT INTO m VALUES (new.x); SELECT RAISE(FAIL, 'stop') WHERE new.x = 2; END",
        'CREATE TRIGGER tu AFTER INSERT ON w WHEN new.x = 5 BEGIN INSERT INTO u VALUES (1); INSERT OR FAIL INTO u VALUES (1); END',
        'INSERT INTO v VALUES (2)',
        'SELECT total_changes(), changes()',
        'INSERT INTO v VALUES (1), (2), (3)',
        'SELECT total_changes(), changes()',
        'INSERT INTO a VALUES (2)',
        'SELECT total_changes(), changes()',
        'INSERT INTO a VALUES (1), (2)',
        'SELECT total_changes(), changes()',
        'INSERT INTO n VALUES (1), (2)',
        'SELECT total_changes(), changes()',
        'BEGIN',
        'INSERT INTO a VALUES (1), (2)',
        'SELECT total_changes(), changes()',
        'INSERT INTO n VALUES (1), (2)',
        'SELECT total_changes(), changes()',
        'COMMIT',
        'INSERT INTO w VALUES (5)',
        'SELECT total_changes(), changes()',
        'INSERT INTO w VALUES (4), (5)',
        'SELECT total_changes(), changes()',
        'INSERT OR FAIL INTO w VALUES (4), (5)',
        'SELECT total_changes(), changes()',
        'CREATE TRIGGER tm AFTER INSERT ON m WHEN new.x = 9 BEGIN INSERT INTO u SELECT 7 UNION ALL SELECT 8 UNION ALL SELECT 1; END',
        'INSERT INTO m VALUES (9)',
        'SELECT total_changes(), changes()',
        'INSERT INTO m VALUES (8), (9)',
        'SELECT total_changes(), changes()',
        'SELECT count(*) FROM m',
    ])
    run(pair, [
        'CREATE TABLE p(x)',
        'CREATE TABLE q(x UNIQUE)',
        'CREATE TABLE lg(x)',
        'INSERT INTO q VALUES (1)',
        'INSERT INTO p VALUES (1), (2), (3)',
        'CREATE TRIGGER pu AFTER UPDATE ON p WHEN new.x = 20 BEGIN INSERT INTO lg VALUES (new.x); INSERT OR FAIL INTO q VALUES (1); END',
        "CREATE TRIGGER pd AFTER DELETE ON p WHEN old.x = 3 BEGIN INSERT INTO lg VALUES (old.x); SELECT RAISE(FAIL, 'nope'); END",
        'UPDATE p SET x = x * 10',
        'SELECT total_changes(), changes()',
        'SELECT * FROM p',
        'UPDATE OR FAIL p SET x = x + 1 WHERE x < 5',
        'SELECT total_changes(), changes()',
        'DELETE FROM p WHERE x < 40',
        'SELECT total_changes(), changes()',
        'SELECT * FROM p',
        'UPDATE p SET x = 3 WHERE x = 30',
        'DELETE FROM p',
        'SELECT total_changes(), changes()',
        'SELECT * FROM p',
        'SELECT * FROM lg',
    ])


def test_upsert_skipped_by_a_trigger(pair):
    run(pair, [
        'CREATE TABLE uu (a UNIQUE, b)',
        "INSERT INTO uu VALUES (1, 'one'), (2, 'two')",
        'CREATE TRIGGER ig BEFORE UPDATE ON uu WHEN old.a = 1 BEGIN SELECT RAISE(IGNORE); END',
        'CREATE TRIGGER del BEFORE UPDATE ON uu WHEN old.a = 2 BEGIN DELETE FROM uu WHERE a = 2; END',
        "INSERT INTO uu VALUES (1, 'x'), (2, 'y'), (3, 'z') ON CONFLICT (a) DO UPDATE SET b = excluded.b RETURNING a, b",
        'SELECT changes(), total_changes()',
        'SELECT * FROM uu',
    ])


def test_replace_pins_the_updated_row(pair):
    """While an UPDATE's REPLACE of a UNIQUE conflict runs DELETE triggers,
    a write to the same table from them fails ("constraint failed": SQLite's
    pinned cursor); a REPLACE of the row id does not pin it."""
    run(pair, [
        'PRAGMA recursive_triggers = ON',
        'CREATE TABLE t(a INTEGER PRIMARY KEY, b UNIQUE, c)',
        'CREATE TABLE log(x)',
        "INSERT INTO t VALUES (1, 'x', 0), (2, 'y', 0), (3, 'z', 0)",
        'CREATE TRIGGER d AFTER DELETE ON t BEGIN INSERT INTO log VALUES (old.b); UPDATE t SET c = c + 1 WHERE a = 3; END',
        "UPDATE OR REPLACE t SET b = 'y' WHERE a = 1",
        'SELECT * FROM t',
        'SELECT * FROM log',
        'DROP TRIGGER d',
        "CREATE TRIGGER d BEFORE DELETE ON t BEGIN INSERT INTO log VALUES ('b' || old.b); END",
        "UPDATE OR REPLACE t SET b = 'z' WHERE a = 1",
        'SELECT * FROM t',
        'SELECT * FROM log',
        "CREATE TRIGGER e AFTER DELETE ON t BEGIN DELETE FROM t WHERE a = 99; INSERT INTO t VALUES (50, 'q', 0); END",
        "INSERT INTO t VALUES (10, 'k', 0), (11, 'm', 0)",
        "UPDATE OR REPLACE t SET b = 'k' WHERE a = 11",
        'SELECT * FROM t',
        'UPDATE OR REPLACE t SET a = 10 WHERE a = 11',
        'SELECT * FROM t',
        'SELECT * FROM log',
        'PRAGMA recursive_triggers = OFF',
        "UPDATE OR REPLACE t SET b = 'k' WHERE a = 11",
        'SELECT * FROM t',
    ])


def test_new_and_old_of_a_real_column_are_real(pair):
    """SQLite reads NEW.x / OLD.x of a REAL column with OP_RealAffinity: an
    integer in a view's row (which has no affinities applied) becomes a
    REAL, in the trigger and in the INSERT's RETURNING."""
    run(pair, [
        "CREATE TABLE t1 (c0, c1 FLOAT, c2 INTEGER)", "CREATE TABLE log (a, b)",
        "CREATE VIEW w AS SELECT c1 AS x, c2 AS z FROM t1", "INSERT INTO w VALUES (3, 4) RETURNING *",
        "CREATE TRIGGER tw INSTEAD OF INSERT ON w BEGIN INSERT INTO log VALUES (new.x, new.x || ''); END",
        "INSERT INTO w VALUES (3, 4) RETURNING *", "INSERT INTO w VALUES ('3', 4.0) RETURNING *, typeof(z)",
        "CREATE TRIGGER bt BEFORE INSERT ON t1 BEGIN INSERT INTO log VALUES (new.c1, typeof(new.c1)); END",
        "INSERT INTO t1 VALUES (1, 5, 6)",
        "CREATE TRIGGER wu INSTEAD OF UPDATE ON w BEGIN INSERT INTO log VALUES (old.x || '>' || new.x, "
        "typeof(old.x) || typeof(new.x)); END",
        "UPDATE w SET x = 9 RETURNING x, typeof(x)",
        "CREATE VIEW w2 AS SELECT x + 0 AS s, CAST(x AS REAL) AS r FROM w",
        "CREATE TRIGGER w2t INSTEAD OF INSERT ON w2 BEGIN INSERT INTO log VALUES (new.r, typeof(new.r)); END",
        "INSERT INTO w2 VALUES (1, 2) RETURNING r", "SELECT * FROM log",
    ])


def test_foreign_key_check_in_a_program_may_abort(pair):
    """An INSERT into a child table in a trigger program may abort (fkey.c's
    sqlite3MayAbort) whatever its OR clause, so a DELETE whose own foreign
    keys cannot abort (CASCADE) still gets a statement journal."""
    run(pair, [
        "PRAGMA foreign_keys = ON", "CREATE TABLE t0 (id INTEGER PRIMARY KEY, c0)",
        "CREATE TABLE t1 (id INTEGER PRIMARY KEY, c0 REFERENCES t0 ON DELETE CASCADE)",
        "INSERT INTO t0 VALUES (1, 'a'), (2, 'b'), (3, 'c')", "INSERT INTO t1 VALUES (5, 1)",
        "CREATE TRIGGER tr0 BEFORE DELETE ON t0 WHEN old.id = 2 BEGIN "
        "INSERT OR IGNORE INTO t1 (id) VALUES (old.id || 'abc'); END",
        "BEGIN", "DELETE FROM t0 WHERE id >= 1", "SELECT * FROM t0", "SELECT * FROM t1", "ROLLBACK",
        "DROP TRIGGER tr0",
        "CREATE TRIGGER tr0 BEFORE DELETE ON t0 WHEN old.id = 2 BEGIN UPDATE OR IGNORE t1 SET id = 'x' || old.id; END",
        "BEGIN", "DELETE FROM t0 WHERE id >= 1", "SELECT * FROM t0", "ROLLBACK",
    ])


def test_function_in_when_may_abort(pair):
    """A function call (LIKE too) in a trigger's WHEN may abort, so a multi-row
    INSERT OR ROLLBACK firing it gets a statement journal: a later datatype
    mismatch undoes the rows it wrote."""
    run(pair, [
        "CREATE TABLE t0 (id INTEGER PRIMARY KEY, c0)", "CREATE TABLE log (x)",
        "CREATE TRIGGER tr AFTER INSERT ON t0 WHEN new.c0 NOT LIKE 'q%' BEGIN INSERT INTO log VALUES (1); END",
        "BEGIN", "INSERT OR ROLLBACK INTO t0 VALUES (1, 10), ('x', 2)", "SELECT * FROM t0", "ROLLBACK",
        "DROP TRIGGER tr",
        "CREATE TRIGGER tr AFTER INSERT ON t0 WHEN new.c0 > 5 BEGIN INSERT INTO log VALUES (1); END",
        "BEGIN", "INSERT OR ROLLBACK INTO t0 VALUES (1, 10), ('x', 2)", "SELECT * FROM t0", "ROLLBACK",
    ])
