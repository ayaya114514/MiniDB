"""Foreign keys compared with SQLite: counting violations per statement and
per transaction, the parent key's index, actions, deferred constraints,
DROP TABLE, PRAGMA foreign_key_check."""

import sqlite3
from contextlib import closing

import pytest

from minidb.database import Database
from sqlcompare import Pair

SCRIPT = """
pragma foreign_keys=1
create table c1(a references nosuch(x))
insert into c1 values(null)
insert into c1 values(1)
delete from c1
create table p(id integer primary key, k unique, x)
insert into p values(1,'a',1),(2,'b',2),(3,'c',3),(4,'d',4)
create table cc(a references p(k) on delete cascade on update cascade, b)
insert into cc values('a', 1), ('b', 2), ('c', 3), ('b', 4)
update p set k = 'B' where k = 'b'
select * from cc
delete from p where k = 'c'
select * from cc
create table sn(a references p(k) on delete set null on update set default)
insert into sn values('B'), ('a')
update p set k = 'BB' where k = 'B'
select * from sn
select * from cc
delete from p where k = 'a'
select * from sn
select * from cc
create table sd(a default 'zzz' references p(k) on delete set default)
insert into sd values('BB')
delete from p where k = 'BB'
select * from sd
insert into p values(30, 'zzz', 0)
delete from p where k = 'BB'
select * from sd
select * from cc
create table r(a references p(id) on delete restrict on update restrict)
insert into p values(20, 'r', 0)
insert into r values(20)
delete from p where id = 20
update p set id = 21 where id = 20
update p set x = 5 where id = 20
update p set id = 20 where id = 20
create table c3(a references p)
insert into c3 values(1)
insert into c3 values(30)
insert into c3 values('30')
insert into c3 values(30.0)
insert into c3 values('x')
insert into c3 values(2), (3)
insert into p values(2,'b',2),(3,'c',3)
insert into c3 values(2), (3)
create table s(id integer primary key, parent references s(id))
insert into s values(1, 1)
insert into s values(3, 2), (2, null)
insert into s values(5, 6)
insert into s select 7, 8 union all select 8, null
delete from s where id = 2
update s set id = 10 where id = 3
select * from s
create table d(a references p(k) deferrable initially deferred)
insert into d values('zz')
begin
insert into d values('zz')
select 1
commit
insert into p values(9, 'zz', 9)
commit
select * from d
pragma foreign_key_check
select total_changes(), changes()
create table bad(a references p(x))
insert into bad values(1)
delete from p where id = 30
pragma foreign_key_check
pragma foreign_key_check(cc)
drop table bad
create table p2(id integer primary key)
create table c2(a references p2 on delete cascade)
insert into p2 values(1),(2)
insert into c2 values(1),(1),(2)
select total_changes(), changes()
delete from p2 where id = 1
select total_changes(), changes()
drop table p2
select * from c2
delete from c2
insert or replace into p values(4, 'q', 1)
create table rp(a references p(k) on delete cascade)
insert into rp values('q')
replace into p values(6, 'q', 2)
select * from rp
select * from p order by id
create table t5(a, b, c, foreign key (b, a) references p (x, k) on update cascade)
create unique index p_kx on p (k, x)
insert into t5 values('zz', 9, 1), ('zzz', 0, 2), ('nope', 1, 3)
insert into t5 values('zz', 9, 1), ('zzz', 0, 2)
update p set x = 99 where k = 'zz'
select * from t5
pragma foreign_key_list(t5)
pragma foreign_key_check(t5)
select total_changes()
begin
pragma defer_foreign_keys = 1
delete from p where id = 20
pragma defer_foreign_keys
commit
rollback
pragma defer_foreign_keys
begin
delete from p where id = 9
insert into p values(9, 'zz', 99)
commit
select * from t5
"""


@pytest.fixture(params=[None, "sqlite"])
def pair(request, tmp_path):
    path = str(tmp_path / "db") if request.param else None
    return Pair(path, check_messages=True, format=request.param)


def test_foreign_keys_as_sqlite(pair):
    for sql in SCRIPT.strip().split("\n"):
        pair.run(sql)


def test_cascades_through_a_tree(pair):
    """A self-referencing table: deleting a node deletes its subtree; set
    null and the counters within one statement."""
    for sql in [
        "PRAGMA foreign_keys = ON",
        "CREATE TABLE node (id INTEGER PRIMARY KEY, parent REFERENCES node ON DELETE CASCADE, name TEXT)",
        "CREATE TABLE tag (node INTEGER REFERENCES node ON DELETE SET NULL ON UPDATE CASCADE, label)",
        "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < 40) "
        "INSERT INTO node SELECT i, CASE WHEN i > 1 THEN i / 2 END, 'n' || i FROM n",
        "INSERT INTO tag SELECT id, 'tag' || id FROM node WHERE id % 3 = 0",
        "SELECT total_changes()", "DELETE FROM node WHERE id = 2", "SELECT changes(), total_changes()",
        "SELECT id FROM node ORDER BY id", "SELECT * FROM tag ORDER BY rowid",
        "UPDATE node SET id = 100 WHERE id = 3", "SELECT * FROM node ORDER BY id", "SELECT * FROM tag ORDER BY rowid",
        "INSERT INTO node VALUES (200, 201, 'x'), (201, NULL, 'y')", "INSERT INTO node VALUES (300, 301, 'x')",
        "DELETE FROM node WHERE parent IS NULL", "SELECT count(*) FROM node", "PRAGMA foreign_key_check",
    ]:
        pair.run(sql)


def test_actions_compare_as_trigger_code(pair):
    """An action finds its child rows by "OLD.parent = child" (OLD.parent
    has the parent's collation, no affinity) and runs only when the key
    changed under that collation; fkScanChildren counts with the parent's
    affinity.  Rows of a two-pass DELETE / UPDATE go in rowid order."""
    for sql in [
        "PRAGMA foreign_keys = ON",
        "CREATE TABLE p (k REAL UNIQUE COLLATE rtrim)", "CREATE TABLE c (r TEXT REFERENCES p (k) ON DELETE SET NULL)",
        "INSERT INTO p VALUES (-1)", "INSERT INTO c VALUES ('-1')", "DELETE FROM p", "SELECT * FROM c",
        "CREATE TABLE p2 (k TEXT UNIQUE)", "CREATE TABLE c2 (r INT REFERENCES p2 (k) ON DELETE SET NULL)",
        "INSERT INTO p2 VALUES ('5')", "INSERT INTO c2 VALUES (5)", "DELETE FROM p2", "SELECT * FROM c2",
        "CREATE TABLE p3 (k TEXT UNIQUE COLLATE nocase)",
        "CREATE TABLE c3 (r REFERENCES p3 (k) ON UPDATE CASCADE, s REFERENCES p3 (k) ON UPDATE SET NULL)",
        "INSERT INTO p3 VALUES ('abc')", "INSERT INTO c3 VALUES ('ABC', 'abc')", "UPDATE p3 SET k = 'ABC'",
        "SELECT * FROM c3", "UPDATE p3 SET k = 'abd'", "SELECT * FROM c3",
        "CREATE TABLE t (id INTEGER PRIMARY KEY, c0 NOT NULL REFERENCES t (id) ON DELETE SET NULL, c2)",
        "INSERT INTO t VALUES (0, 0, NULL), (1, 0, 'b'), (2, 1, 'a')", "UPDATE t SET c0 = 2 WHERE id = 0",
        "CREATE UNIQUE INDEX ti ON t (c0, c2)", "DELETE FROM t WHERE c0 <= 5", "SELECT * FROM t",
        "UPDATE t SET c2 = c2 || 'x' WHERE c0 <= 5 RETURNING id",
    ]:
        pair.run(sql)


def test_statement_journal_for_foreign_keys(pair):
    """SQLite gives a statement a journal for its foreign keys only where
    their code may abort it (an immediate key on an added row, a removed
    parent key without CASCADE / SET NULL ...): without one, a statement that
    fails for another reason inside a transaction keeps its earlier rows."""
    for sql in [
        "PRAGMA foreign_keys = ON", "CREATE TABLE p (id INTEGER PRIMARY KEY)", "INSERT INTO p VALUES (1), (2)",
        "CREATE TABLE d (id INTEGER PRIMARY KEY, r REFERENCES p DEFERRABLE INITIALLY DEFERRED)",
        "CREATE TABLE i (id INTEGER PRIMARY KEY, r REFERENCES p)",
        "CREATE TABLE n (id INTEGER PRIMARY KEY, r REFERENCES p ON DELETE SET NULL)",
        "BEGIN",
        "INSERT OR FAIL INTO d VALUES (NULL, 1), ('x', 2)", "INSERT OR FAIL INTO i VALUES (NULL, 1), ('x', 2)",
        "INSERT INTO d SELECT NULL, 1 UNION ALL SELECT 'x', 1", "SELECT * FROM d", "SELECT * FROM i",
        "INSERT INTO n VALUES (1, 1), (2, 2)", "COMMIT", "BEGIN",
        "UPDATE n SET id = CASE id WHEN 2 THEN 'x' ELSE 5 END", "SELECT * FROM n",
        "DELETE FROM p WHERE id = 1 OR id / (id - 2) > 0 OR abs(-9223372036854775807 - id)", "SELECT * FROM n",
        "COMMIT",
        # An upsert that leaves the parent key alone needs no journal for it.
        "CREATE TABLE pp (id INTEGER PRIMARY KEY, v)", "CREATE TABLE cc (r REFERENCES pp)", "BEGIN",
        "INSERT INTO pp (id) VALUES (7), ('x') ON CONFLICT (id) DO UPDATE SET v = 1", "SELECT * FROM pp",
        "INSERT INTO pp (id) VALUES (8), ('x') ON CONFLICT (id) DO UPDATE SET id = 9", "SELECT * FROM pp", "COMMIT",
    ]:
        pair.run(sql)


def test_single_row_insert_into_a_parent(pair):
    """A one-row INSERT does not look for the child rows its new parent key
    fixes (SQLite: it cannot fix an immediate violation), so its own
    dangling key still fails; a multi-row INSERT counts both."""
    for sql in [
        "PRAGMA foreign_keys = ON",
        "CREATE TABLE t (id INTEGER PRIMARY KEY, c0 NOT NULL REFERENCES t (id) ON DELETE SET NULL)",
        "INSERT OR REPLACE INTO t VALUES (1, 10)", "INSERT INTO t VALUES (10, -1)", "SELECT * FROM t",
        "INSERT INTO t VALUES (10, -1), (11, 1)", "INSERT INTO t SELECT 10, -1", "SELECT * FROM t",
        "PRAGMA foreign_key_check",
    ]:
        pair.run(sql)


def test_foreign_keys_off_by_default(pair):
    for sql in ["CREATE TABLE p (id INTEGER PRIMARY KEY)", "CREATE TABLE c (a REFERENCES p, b REFERENCES nosuch)",
                "INSERT INTO c VALUES (1, 2)", "PRAGMA foreign_key_check", "PRAGMA foreign_keys = ON",
                "INSERT INTO c VALUES (1, NULL)", "DELETE FROM p", "DROP TABLE p", "PRAGMA foreign_key_check(c)"]:
        pair.run(sql)


def test_sqlite_agrees_with_what_minidb_wrote(tmp_path):
    path = str(tmp_path / "db")
    with Database(path, format="sqlite") as db:
        db.execute("PRAGMA foreign_keys = ON")
        db.execute("CREATE TABLE artist (id INTEGER PRIMARY KEY, name TEXT UNIQUE COLLATE nocase)")
        db.execute("CREATE TABLE album (id INTEGER PRIMARY KEY, artist TEXT REFERENCES artist (name) "
                   "ON UPDATE CASCADE ON DELETE CASCADE, title)")
        db.execute("INSERT INTO artist VALUES (1, 'Abba'), (2, 'Blur')")
        db.execute("INSERT INTO album (artist, title) VALUES ('abba', 'Waterloo'), ('Blur', 'Parklife'), "
                   "('BLUR', '13')")
        db.execute("UPDATE artist SET name = 'ABBA!' WHERE id = 1")
        db.execute("DELETE FROM artist WHERE name = 'blur'")
        rows = db.execute("SELECT * FROM album")
        assert rows == [(1, "ABBA!", "Waterloo")]
    with closing(sqlite3.connect(path)) as lite:
        assert lite.execute("PRAGMA foreign_key_check").fetchall() == []
        assert lite.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert lite.execute("SELECT * FROM album").fetchall() == rows


@pytest.mark.parametrize("statement", [
    "SELECT 1", "SELECT * FROM t", "VALUES (1)", "WITH c AS (SELECT 1) SELECT * FROM c", "SELECT * FROM sqlite_master",
    "SELECT 1 WHERE EXISTS (SELECT 1 FROM t)", "SELECT * FROM pragma_table_info('t')", "SELECT * FROM nosuch",
    "INSERT INTO t VALUES (1)", "INSERT INTO nosuch VALUES (1)", "CREATE TABLE t (b)", "PRAGMA user_version",
    "PRAGMA user_version = 1", "PRAGMA foreign_keys", "PRAGMA cache_size = 10", "PRAGMA table_info(t)",
    "PRAGMA table_info", "PRAGMA index_list(t)", "PRAGMA index_list(nosuch)", "PRAGMA index_info(ti)",
    "PRAGMA index_info(t)", "PRAGMA integrity_check",
])
def test_defer_foreign_keys_ends_with_an_implicit_transaction(pair, statement):
    """Outside a transaction, PRAGMA defer_foreign_keys lasts until a
    statement whose SQLite program reads the database file has run."""
    for sql in ["CREATE TABLE t (a)", "CREATE INDEX ti ON t (a)", "PRAGMA defer_foreign_keys = ON", statement,
                "PRAGMA defer_foreign_keys"]:
        pair.run(sql)


def test_replace_rechecks_with_a_stale_row_id(pair):
    """With foreign keys on, after a REPLACE SQLite checks the REPLACE indexes
    again, comparing the row id its last index lookup found (not the one it
    finds now) with the updated row's: an index the UPDATE checks but whose
    key it kept reports a conflict with the row itself.  Which indexes an
    UPDATE checks: those with a column it sets, all with an ON UPDATE action."""
    for sql in [
        'PRAGMA foreign_keys = ON',
        'CREATE TABLE t0 (id INTEGER PRIMARY KEY, c0, c1 TEXT UNIQUE)',
        'CREATE TABLE t1 (c0 REFERENCES t0(c1) ON UPDATE CASCADE)',
        'CREATE UNIQUE INDEX i11 ON t0 (id)',
        "INSERT INTO t0 VALUES (27, NULL, NULL), (29, 1.0, '1')",
        'UPDATE OR REPLACE t0 SET c1 = 1 WHERE rowid = 27',
        'SELECT * FROM t0',
        'CREATE TABLE p (id INTEGER PRIMARY KEY, a UNIQUE, b UNIQUE)',
        'CREATE TABLE c (x REFERENCES p(a))',
        "INSERT INTO p VALUES (1, 'a', 'b'), (2, 'x', 'y')",
        "UPDATE OR REPLACE p SET a = 'x', b = b WHERE id = 1",
        'SELECT * FROM p',
        "UPDATE OR REPLACE p SET b = b, a = 'x' WHERE id = 1",
        'SELECT * FROM p',
        "INSERT INTO p VALUES (3, 'q', 'r')",
        "UPDATE OR REPLACE p SET a = 'x' WHERE id = 3",
        'SELECT * FROM p',
        'CREATE TABLE q (id INTEGER PRIMARY KEY, a UNIQUE ON CONFLICT REPLACE, b UNIQUE)',
        'CREATE TABLE qc (x REFERENCES q(a))',
        "INSERT INTO q VALUES (1, 'a', 'b'), (2, 'x', 'y')",
        "UPDATE q SET a = 'x', b = b WHERE id = 1",
        'SELECT * FROM q',
        "REPLACE INTO q VALUES (1, 'x', 'z')",
        'SELECT * FROM q',
        'PRAGMA foreign_keys = OFF',
        "INSERT INTO p VALUES (4, 'm', 'n')",
        "UPDATE OR REPLACE p SET a = 'm', b = b WHERE id = 1",
        'SELECT * FROM p',
    ]:
        pair.run(sql)


def test_upsert_update_checks_its_own_foreign_keys(pair):
    """An upsert's UPDATE is compiled before the REPLACE's DELETE, so the
    SET NULL action of that DELETE does not make SQLite leave out the
    UPDATE's check of the new row (isSetNullAction)."""
    for sql in [
        "PRAGMA foreign_keys = ON",
        "CREATE TABLE t1 (id INTEGER PRIMARY KEY, c0 REFERENCES t1(id) ON DELETE SET NULL, c1 TEXT, "
        "c2 INT NOT NULL, c4, UNIQUE (c1, c2))",
        "REPLACE INTO t1 VALUES (1, NULL, NULL, 'a1', 1), (1, 1, 0, 0, 'A') ON CONFLICT (id) DO UPDATE SET c1 = 0, c0 = 2",
        "INSERT INTO t1 VALUES (1, NULL, NULL, 'a1', 1), (1, 1, 0, 0, 'A') ON CONFLICT (id) DO UPDATE SET c1 = 0, c0 = 2",
        "REPLACE INTO t1 VALUES (3, NULL, 'x', 1, 1), (3, 1, 0, 0, 'A') ON CONFLICT (id) DO UPDATE SET c0 = 3",
        "SELECT * FROM t1",
    ]:
        pair.run(sql)


@pytest.mark.parametrize("child, index, value, parent", [
    ("INTEGER", "CREATE INDEX ci ON c(x)", "'2.5'", "TEXT"),
    ("REAL", "CREATE UNIQUE INDEX ci ON c(x, y)", "'5'", "TEXT"),
    ("NUMERIC", "CREATE INDEX ci ON c(x)", "5.0", ""),
    ("NUMERIC", "CREATE INDEX ci ON c(x)", "5.0", "REAL"),
    ("INTEGER", "CREATE INDEX ci ON c(x)", "'abc'", "TEXT"),
    ("INTEGER", "CREATE INDEX ci ON c(y, x)", "'2.5'", "TEXT"),
    ("INTEGER", "CREATE INDEX ci ON c(x COLLATE nocase)", "'2.5'", "TEXT"),
    ("INTEGER", None, "'2.5'", "TEXT"),
    ("TEXT", "CREATE INDEX ci ON c(x)", "5", "INTEGER"),
])
def test_child_index_converts_the_old_parent_key(pair, child, index, value, parent):
    """SQLite applies a numeric child index's affinity to the deleted parent
    row's key in place while it looks for children (ForeignKeys.convert_old):
    the actions, AFTER triggers and RETURNING see the converted value."""
    for sql in [
        "PRAGMA foreign_keys = ON", f"CREATE TABLE p (id INTEGER PRIMARY KEY, k {parent} UNIQUE)",
        f"CREATE TABLE c (x {child} REFERENCES p(k) ON DELETE SET NULL, y)", index or "SELECT 1",
        "CREATE TABLE log (a, b)",
        "CREATE TRIGGER t AFTER DELETE ON p BEGIN INSERT INTO log VALUES (old.k, typeof(old.k)); END",
        "CREATE TRIGGER u AFTER UPDATE ON p BEGIN INSERT INTO log VALUES (old.k || '>' || new.k, typeof(old.k)); END",
        f"INSERT INTO p VALUES (1, {value}), (2, {value} || 'z')", "INSERT INTO c VALUES ((SELECT k FROM p WHERE id = 1), 1)",
        "UPDATE p SET k = k || 'q' WHERE id = 2 RETURNING k", "DELETE FROM p WHERE id = 1 RETURNING k",
        "SELECT * FROM log", "SELECT * FROM c",
    ]:
        pair.run(sql)


def test_replace_with_foreign_keys_may_abort(pair):
    """With foreign keys on, a REPLACE is followed by SQLite's uniqueness
    recheck, which halts as ABORT: a multi-row REPLACE then has a statement
    journal, and a later datatype mismatch undoes its rows."""
    for sql in [
        "PRAGMA foreign_keys = ON",
        "CREATE TABLE p (id INTEGER PRIMARY KEY, u UNIQUE, v)",
        "CREATE TABLE c (x REFERENCES p(id) DEFERRABLE INITIALLY DEFERRED)",
        "BEGIN", "REPLACE INTO p VALUES (1, 1, 1), (-3.75, 2, 2)", "SELECT * FROM p", "ROLLBACK",
        "CREATE TABLE q (id INTEGER PRIMARY KEY, v)",
        "CREATE TABLE d (x REFERENCES q(id) DEFERRABLE INITIALLY DEFERRED)",
        "BEGIN", "REPLACE INTO q VALUES (1, 1), (-3.75, 2)", "SELECT * FROM q", "ROLLBACK",
        "BEGIN", "INSERT OR ROLLBACK INTO q VALUES (1, 1), (-3.75, 2)", "SELECT * FROM q", "ROLLBACK",
    ]:
        pair.run(sql)


def test_a_replace_that_may_delete_a_parent_row(pair):
    # A REPLACE that may delete a row of a table with foreign keys is a
    # multi-row write (sqlite3MultiWrite): when RESTRICT stops it inside a
    # transaction, the statement journal brings the deleted row back.
    for sql in """
        PRAGMA foreign_keys = ON;
        CREATE TABLE t1 (id INTEGER PRIMARY KEY, c0 REFERENCES t1 (id) ON DELETE RESTRICT, c1);
        INSERT INTO t1 VALUES (1, NULL, 0), (2, 1, 0);
        BEGIN;
        INSERT INTO t1 VALUES (3, NULL, 0);
        REPLACE INTO t1 (id, c1) VALUES (1, 5);
        SELECT * FROM t1;
        COMMIT;
        CREATE TABLE p (id INTEGER PRIMARY KEY, u UNIQUE);
        CREATE TABLE c (x REFERENCES p (u) ON DELETE RESTRICT);
        INSERT INTO p VALUES (1, 'a'), (2, 'b');
        INSERT INTO c VALUES ('a');
        BEGIN;
        INSERT OR REPLACE INTO p VALUES (3, 'a');
        SELECT * FROM p;
        ROLLBACK
    """.strip().split(";\n"):
        pair.run(sql)
