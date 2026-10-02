"""Real SQLite databases (tools/sample_databases.py): sqlite3 writes, MiniDB
changes them with foreign keys on, sqlite3 checks the result.

Each statement runs on two copies - MiniDB on one, sqlite3 on the other -
and must give the same rows or the same error; afterwards sqlite3 finds the
MiniDB copy intact (integrity_check, foreign_key_check) and with the same
contents as its own.  Skipped when the files cannot be downloaded."""

import os
import shutil
import sqlite3
import sys
from contextlib import closing

import pytest

from minidb.database import Database

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))
import sample_databases  # noqa: E402


def sample(name):
    try:
        return sample_databases.fetch(name)
    except (OSError, ValueError) as exc:
        pytest.skip(f"sample database {name} unavailable: {exc}")


def copies(name, tmp_path, prepare=()):
    """Two copies of the sample, after sqlite3 ran ``prepare`` on it."""
    original = str(tmp_path / "original.db")
    shutil.copyfile(sample(name), original)
    with closing(sqlite3.connect(original, isolation_level=None)) as lite:
        for sql in prepare:
            lite.execute(sql)
    mini_path, lite_path = str(tmp_path / "mini.db"), str(tmp_path / "lite.db")
    shutil.copyfile(original, mini_path)
    shutil.copyfile(original, lite_path)
    return mini_path, lite_path


def outcome(run, sql):
    try:
        rows = run(sql)
    except Exception as exc:  # (sqlite3's and MiniDB's errors have the same messages)
        return "error", str(exc)
    return "rows", [tuple(row) for row in rows]


def run_both(mini_path, lite_path, statements):
    with Database(mini_path) as mini, closing(sqlite3.connect(lite_path, isolation_level=None)) as lite:
        for sql in statements:
            expected = outcome(lambda s: lite.execute(s).fetchall(), sql)
            assert outcome(mini.execute, sql) == expected, sql


def assert_same_files(mini_path, lite_path):
    with closing(sqlite3.connect(mini_path)) as mini, closing(sqlite3.connect(lite_path)) as lite:
        assert mini.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert mini.execute("PRAGMA foreign_key_check").fetchall() == lite.execute(
            "PRAGMA foreign_key_check").fetchall()
        schema = "SELECT type, name, tbl_name, sql FROM sqlite_schema ORDER BY name"
        assert mini.execute(schema).fetchall() == lite.execute(schema).fetchall()
        tables = [name for (name,) in lite.execute("SELECT name FROM sqlite_schema WHERE type = 'table'")]
        for table in tables:
            query = f'SELECT rowid, * FROM "{table}" ORDER BY rowid'
            assert mini.execute(query).fetchall() == lite.execute(query).fetchall(), table


def test_chinook(tmp_path):
    # Chinook comes with 1024-byte pages; MiniDB writes 4096-byte pages only (stage 25).
    mini_path, lite_path = copies("chinook", tmp_path, ["PRAGMA page_size = 4096", "VACUUM"])
    run_both(mini_path, lite_path, [
        "PRAGMA foreign_keys = ON",
        "SELECT count(*), sum(UnitPrice) FROM Track",
        "INSERT INTO Artist (Name) VALUES ('MiniDB Ensemble')",
        "INSERT INTO Album (Title, ArtistId) SELECT 'Pages and Trees', max(ArtistId) FROM Artist",
        "INSERT INTO Track (Name, AlbumId, MediaTypeId, GenreId, Composer, Milliseconds, Bytes, UnitPrice) "
        "SELECT 'Split ' || value, (SELECT max(AlbumId) FROM Album), 1, 1, 'B. Tree', 1000 * value, NULL, 0.99 "
        "FROM (SELECT 1 AS value UNION ALL SELECT 2 UNION ALL SELECT 3)",
        "INSERT INTO Album (Title, ArtistId) VALUES ('Nobody', 99999)",
        "DELETE FROM Artist WHERE ArtistId = 1",
        "UPDATE Track SET UnitPrice = round(UnitPrice * 1.1, 2) WHERE GenreId = 1",
        "UPDATE Genre SET GenreId = 100 WHERE GenreId = 25",
        "DELETE FROM InvoiceLine WHERE InvoiceId IN (SELECT InvoiceId FROM Invoice WHERE CustomerId = 5)",
        "DELETE FROM Customer WHERE CustomerId = 5",
        "DELETE FROM Invoice WHERE CustomerId = 5",
        "DELETE FROM Customer WHERE CustomerId = 5",
        "BEGIN", "PRAGMA defer_foreign_keys = ON",
        "DELETE FROM Genre WHERE GenreId = 25", "UPDATE Track SET GenreId = 24 WHERE GenreId = 25", "COMMIT",
        "BEGIN", "DELETE FROM MediaType WHERE MediaTypeId = 5", "PRAGMA defer_foreign_keys = ON",
        "DELETE FROM MediaType WHERE MediaTypeId = 5", "COMMIT", "ROLLBACK",
        "UPDATE Employee SET ReportsTo = 99 WHERE EmployeeId = 8",
        "CREATE INDEX TrackNameNocase ON Track (Name COLLATE NOCASE)",
        "SELECT TrackId, Name FROM Track WHERE Name = 'split 2' COLLATE NOCASE",
        "SELECT ar.Name, count(*) FROM Artist ar JOIN Album al USING (ArtistId) JOIN Track t USING (AlbumId) "
        "GROUP BY ar.ArtistId ORDER BY count(*) DESC, ar.Name LIMIT 5",
        "PRAGMA foreign_key_check",
        "PRAGMA integrity_check",
    ])
    assert_same_files(mini_path, lite_path)


def test_northwind(tmp_path):
    mini_path, lite_path = copies("northwind", tmp_path)
    run_both(mini_path, lite_path, [
        "PRAGMA foreign_keys = ON",
        "SELECT seq FROM sqlite_sequence WHERE name = 'Orders'",
        "INSERT INTO Orders (CustomerID, EmployeeID, OrderDate, ShipVia, Freight) "
        "VALUES ('ALFKI', 1, '2026-10-01', 1, 12.5)",
        "SELECT seq FROM sqlite_sequence WHERE name = 'Orders'",
        "INSERT INTO [Order Details] (OrderID, ProductID, UnitPrice, Quantity, Discount) "
        "SELECT max(OrderID), 11, 14.0, 3, 0 FROM Orders",
        "INSERT INTO [Order Details] (OrderID, ProductID, UnitPrice, Quantity, Discount) "
        "SELECT max(OrderID), 12, -1, 3, 0 FROM Orders",
        "INSERT INTO [Order Details] (OrderID, ProductID, UnitPrice, Quantity, Discount) "
        "SELECT max(OrderID), 13, 1, 0, 0 FROM Orders",
        "INSERT INTO [Order Details] (OrderID, ProductID) VALUES (1, 11)",
        "INSERT INTO [Order Details] (OrderID, ProductID) VALUES (10248, 11)",
        "UPDATE Products SET UnitPrice = UnitPrice + 1 WHERE CategoryID = 2",
        "UPDATE [Order Details] SET Discount = 2 WHERE OrderID = 10249",
        "DELETE FROM Orders WHERE OrderID = 10248",
        "DELETE FROM [Order Details] WHERE OrderID = 10248",
        "DELETE FROM Orders WHERE OrderID = 10248",
        "DELETE FROM Shippers WHERE ShipperID = 1",
        "UPDATE Employees SET ReportsTo = 42 WHERE EmployeeID = 9",
        "INSERT INTO Categories (CategoryName, Description) VALUES ('Software', 'Databases')",
        "SELECT * FROM sqlite_sequence ORDER BY name",
        "SELECT * FROM [Current Product List] ORDER BY ProductID LIMIT 5",
        "SELECT * FROM [Products Above Average Price] ORDER BY UnitPrice DESC, ProductName LIMIT 3",
        "SELECT OrderID, ProductID, Quantity FROM [Order Details] WHERE OrderID = (SELECT max(OrderID) FROM Orders)",
    ])
    assert_same_files(mini_path, lite_path)
