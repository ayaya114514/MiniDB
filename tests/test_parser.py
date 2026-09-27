import pytest

from minidb.parser import (
    Between, Binary, Call, Column, ColumnDef, CreateTable, Delete, DropTable, InList, Insert,
    Join, Like, Literal, OrderItem, Select, SelectItem, Star, TableRef, Unary, Update, parse,
    parse_script,
)
from minidb.tokenizer import SQLSyntaxError


def where(sql):
    return parse("SELECT * FROM t WHERE " + sql).where


def a(name):
    return Column(name)


def lit(value):
    return Literal(value)


# ---- statements ----------------------------------------------------------


def test_create_table():
    assert parse(
        "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT NOT NULL, age integer, email text UNIQUE NULL)"
    ) == CreateTable("users", [
        ColumnDef("id", "INTEGER", primary_key=True),
        ColumnDef("name", "TEXT", not_null=True),
        ColumnDef("age", "INTEGER"),
        ColumnDef("email", "TEXT", unique=True),
    ])


def test_create_table_if_not_exists_and_quoted_names():
    stmt = parse('create table if not exists "my table" ("key" text, [value] integer)')
    assert stmt == CreateTable(
        "my table", [ColumnDef("key", "TEXT"), ColumnDef("value", "INTEGER")], if_not_exists=True
    )


def test_drop_table():
    assert parse("DROP TABLE t") == DropTable("t")
    assert parse("DROP TABLE IF EXISTS t;") == DropTable("t", if_exists=True)


def test_insert():
    assert parse("INSERT INTO t VALUES (1, 'a', NULL), (-2, 'b''c', 3.5)") == Insert(
        "t", None, [
            [lit(1), lit("a"), lit(None)],
            [Unary("-", lit(2)), lit("b'c"), lit(3.5)],
        ],
    )
    assert parse("insert into t (b, a) values (1, 2)") == Insert("t", ["b", "a"], [[lit(1), lit(2)]])


def test_select():
    assert parse("SELECT * FROM t") == Select([SelectItem(Star())], [Join(TableRef("t"))])
    assert parse("SELECT a, b AS x, c y, t.d, t.* FROM t AS u WHERE a = 1") == Select(
        [
            SelectItem(a("a")),
            SelectItem(a("b"), "x"),
            SelectItem(a("c"), "y"),
            SelectItem(Column("d", "t")),
            SelectItem(Star("t")),
        ],
        [Join(TableRef("t", "u"))],
        Binary("=", a("a"), lit(1)),
    )


def test_order_by_limit_offset():
    stmt = parse("SELECT a FROM t ORDER BY a DESC, 2, b ASC NULLS LAST, c nulls first LIMIT 5 OFFSET 2")
    assert stmt.order_by == [
        OrderItem(a("a"), True), OrderItem(lit(2)), OrderItem(a("b"), False, False),
        OrderItem(a("c"), False, True),
    ]
    assert (stmt.limit, stmt.offset) == (lit(5), lit(2))
    stmt = parse("SELECT a FROM t LIMIT 3, 10")
    assert (stmt.limit, stmt.offset) == (lit(10), lit(3))


def test_group_by_having():
    stmt = parse("SELECT a, count(*) FROM t WHERE b > 0 GROUP BY a, b HAVING count(*) > 1")
    assert stmt.group_by == [a("a"), a("b")]
    assert stmt.having == Binary(">", Call("COUNT", (Star(),)), lit(1))


def test_joins():
    stmt = parse(
        "SELECT * FROM a, b AS x JOIN c ON c.id = x.id INNER JOIN d ON 1 "
        "LEFT JOIN e ON e.k = a.k LEFT OUTER JOIN f CROSS JOIN g"
    )
    assert stmt.source == [
        Join(TableRef("a")),
        Join(TableRef("b", "x")),
        Join(TableRef("c"), "INNER", Binary("=", Column("id", "c"), Column("id", "x"))),
        Join(TableRef("d"), "INNER", lit(1)),
        Join(TableRef("e"), "LEFT", Binary("=", Column("k", "e"), Column("k", "a"))),
        Join(TableRef("f"), "LEFT"),
        Join(TableRef("g")),
    ]


def test_select_item_text():
    items = parse("SELECT a+1 , count( * ) AS n, 'x'  FROM t").items
    assert [item.text for item in items] == ["a+1", "count( * )", "'x'"]


def test_select_without_from_and_distinct():
    assert parse("SELECT 1 + 2") == Select([SelectItem(Binary("+", lit(1), lit(2)))])
    assert parse("SELECT DISTINCT a FROM t").distinct


def test_update():
    assert parse("UPDATE t SET a = a + 1, b = 'x' WHERE id = 3") == Update(
        "t",
        [("a", Binary("+", a("a"), lit(1))), ("b", lit("x"))],
        Binary("=", a("id"), lit(3)),
    )
    assert parse("UPDATE t SET a = 1").where is None


def test_delete():
    assert parse("DELETE FROM t") == Delete("t")
    assert parse("DELETE FROM t WHERE a <> 2") == Delete("t", Binary("!=", a("a"), lit(2)))


def test_script():
    statements = parse_script("SELECT 1; ; SELECT 2;\nDELETE FROM t")
    assert len(statements) == 3
    assert parse_script("  -- nothing\n ;") == []


def test_function_calls():
    stmt = parse("SELECT COUNT(*), count(DISTINCT a), sum(a + 1), random() FROM t")
    assert [item.expr for item in stmt.items] == [
        Call("COUNT", (Star(),)),
        Call("COUNT", (a("a"),), distinct=True),
        Call("SUM", (Binary("+", a("a"), lit(1)),)),
        Call("RANDOM", ()),
    ]


# ---- expressions and precedence -------------------------------------------


def test_and_binds_tighter_than_or():
    assert where("a = 1 OR b = 2 AND c = 3") == Binary(
        "OR",
        Binary("=", a("a"), lit(1)),
        Binary("AND", Binary("=", a("b"), lit(2)), Binary("=", a("c"), lit(3))),
    )


def test_parentheses_override_precedence():
    assert where("(a = 1 OR b = 2) AND c = 3") == Binary(
        "AND",
        Binary("OR", Binary("=", a("a"), lit(1)), Binary("=", a("b"), lit(2))),
        Binary("=", a("c"), lit(3)),
    )


def test_not_binds_looser_than_comparison():
    assert where("NOT a = 1 AND b") == Binary(
        "AND", Unary("NOT", Binary("=", a("a"), lit(1))), a("b")
    )
    assert where("NOT NOT a") == Unary("NOT", Unary("NOT", a("a")))


def test_not_as_operand():
    assert where("0 != NOT 1 AND b") == Binary(
        "AND", Binary("!=", lit(0), Unary("NOT", lit(1))), a("b")
    )
    assert where("a + NOT b = c") == Binary(
        "+", a("a"), Unary("NOT", Binary("=", a("b"), a("c")))
    )


def test_arithmetic_precedence():
    assert where("a + b * c - d / 2 % 3") == Binary(
        "-",
        Binary("+", a("a"), Binary("*", a("b"), a("c"))),
        Binary("%", Binary("/", a("d"), lit(2)), lit(3)),
    )


def test_comparison_binds_tighter_than_equality():
    assert where("a < b = c > d") == Binary(
        "=", Binary("<", a("a"), a("b")), Binary(">", a("c"), a("d"))
    )


def test_arithmetic_binds_tighter_than_comparison():
    assert where("a + 1 >= b * 2") == Binary(
        ">=", Binary("+", a("a"), lit(1)), Binary("*", a("b"), lit(2))
    )


def test_concat_and_unary():
    assert where("-a || b * 2") == Binary(
        "*", Binary("||", Unary("-", a("a")), a("b")), lit(2)
    )
    assert where("- -a") == Unary("-", Unary("-", a("a")))


def test_operator_spellings():
    assert where("a == 1") == Binary("=", a("a"), lit(1))
    assert where("a <> 1") == Binary("!=", a("a"), lit(1))
    assert where("a != 1") == Binary("!=", a("a"), lit(1))


def test_is_null_and_is_not():
    assert where("a IS NULL") == Binary("IS", a("a"), lit(None))
    assert where("a IS NOT NULL") == Binary("IS NOT", a("a"), lit(None))
    assert where("NOT a IS b") == Unary("NOT", Binary("IS", a("a"), a("b")))


def test_in_like_between():
    assert where("a IN (1, 2, 3)") == InList(a("a"), (lit(1), lit(2), lit(3)))
    assert where("a NOT IN ('x')") == InList(a("a"), (lit("x"),), negated=True)
    assert where("a LIKE 'x%'") == Like(a("a"), lit("x%"))
    assert where("a NOT LIKE 'x%'") == Like(a("a"), lit("x%"), negated=True)
    assert where("a BETWEEN 1 AND 2 + 3 AND b") == Binary(
        "AND", Between(a("a"), lit(1), Binary("+", lit(2), lit(3))), a("b")
    )
    assert where("a NOT BETWEEN 1 AND 2") == Between(a("a"), lit(1), lit(2), negated=True)


def test_min_int_literal():
    assert parse("SELECT -9223372036854775808").items[0].expr == lit(-(2**63))


def test_qualified_column():
    assert where("t.a = u.b") == Binary("=", Column("a", "t"), Column("b", "u"))


# ---- errors -----------------------------------------------------------------


@pytest.mark.parametrize(
    "sql, message, column",
    [
        ("SELEC * FROM t", 'syntax error near "SELEC": expected a statement', 1),
        ("SELECT FROM t", 'syntax error near "FROM": expected expression', 8),
        ("SELECT a FROM", "syntax error at end of input: expected table name", 14),
        ("SELECT a FROM t WHERE", "syntax error at end of input: expected expression", 22),
        ("SELECT (a FROM t", 'syntax error near "FROM": expected ")"', 11),
        ("INSERT t VALUES (1)", 'syntax error near "t": expected INTO', 8),
        ("INSERT INTO t VALUES 1", 'syntax error near "1": expected "("', 22),
        ("CREATE TABLE t (a REAL)", 'syntax error near "REAL": expected column type INTEGER or TEXT', 19),
        ("CREATE TABLE t (a INT)", 'syntax error near "INT": expected column type INTEGER or TEXT', 19),
        ("CREATE TABLE t (a INTEGER PRIMARY)", 'syntax error near ")": expected KEY', 34),
        ("CREATE TABLE t ()", 'syntax error near ")": expected column name', 17),
        ("UPDATE t SET a 1", 'syntax error near "1": expected "="', 16),
        ("DELETE t", 'syntax error near "t": expected FROM', 8),
        ("SELECT a b c FROM t", 'syntax error near "c": expected ";" or end of statement', 12),
        ("SELECT a BETWEEN 1 OR 2", 'syntax error near "OR": expected AND', 20),
        ("SELECT 1 +", "syntax error at end of input: expected expression", 11),
        ("SELECT a FROM t ORDER a", 'syntax error near "a": expected BY', 23),
        ("SELECT a FROM t GROUP a", 'syntax error near "a": expected BY', 23),
        ("SELECT a FROM t LEFT t2", 'syntax error near "t2": expected JOIN', 22),
        ("SELECT a FROM t ORDER BY a NULLS", "syntax error at end of input: expected FIRST or LAST", 33),
        ("SELECT a FROM t JOIN", "syntax error at end of input: expected table name", 21),
    ],
)
def test_syntax_errors(sql, message, column):
    with pytest.raises(SQLSyntaxError) as info:
        parse(sql)
    assert info.value.message == message
    assert info.value.line == 1
    assert info.value.column == column


def test_error_line_numbers():
    with pytest.raises(SQLSyntaxError) as info:
        parse("SELECT a,\n       b\n  FROM t\n WHERE a = = 1")
    assert (info.value.line, info.value.column) == (4, 12)
    assert info.value.caret() == " WHERE a = = 1\n           ^"


def test_parse_requires_one_statement():
    with pytest.raises(SQLSyntaxError):
        parse("SELECT 1; SELECT 2")
    with pytest.raises(SQLSyntaxError):
        parse("  ")
