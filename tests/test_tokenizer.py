import pytest

from minidb.tokenizer import SQLSyntaxError, tokenize


def kinds_and_values(text):
    return [(t.kind, t.value) for t in tokenize(text)][:-1]


def test_keywords_are_case_insensitive():
    assert kinds_and_values("select FROM Where") == [
        ("KEYWORD", "SELECT"), ("KEYWORD", "FROM"), ("KEYWORD", "WHERE"),
    ]


def test_identifiers():
    assert kinds_and_values('users _x a1$ "my table" `b` [c d] "a""b"') == [
        ("IDENT", "users"), ("IDENT", "_x"), ("IDENT", "a1$"), ("IDENT", "my table"),
        ("IDENT", "b"), ("IDENT", "c d"), ("IDENT", 'a"b'),
    ]


def test_non_reserved_words_are_identifiers():
    assert kinds_and_values("key text integer count") == [
        ("IDENT", "key"), ("IDENT", "text"), ("IDENT", "integer"), ("IDENT", "count"),
    ]


def test_numbers():
    assert kinds_and_values("0 42 3.5 .5 1e3 2E-2 7. 9223372036854775808") == [
        ("INTEGER", 0), ("INTEGER", 42), ("FLOAT", 3.5), ("FLOAT", 0.5), ("FLOAT", 1000.0),
        ("FLOAT", 0.02), ("FLOAT", 7.0), ("FLOAT", 9223372036854775808.0),
    ]


def test_strings():
    assert kinds_and_values("'hello' '' 'it''s' '日本'") == [
        ("STRING", "hello"), ("STRING", ""), ("STRING", "it's"), ("STRING", "日本"),
    ]


def test_operators():
    ops = "<> <= >= == != || < > = + - * / % ( ) , ; ."
    assert [v for _, v in kinds_and_values(ops)] == ops.split()
    assert [v for _, v in kinds_and_values("a<=b")] == ["a", "<=", "b"]


def test_comments_are_skipped():
    assert kinds_and_values("SELECT -- comment\n 1 /* multi\nline */ + 2") == [
        ("KEYWORD", "SELECT"), ("INTEGER", 1), ("OP", "+"), ("INTEGER", 2),
    ]


def test_token_positions():
    tokens = tokenize("SELECT a\n  FROM t")
    assert [(t.text, t.pos) for t in tokens] == [
        ("SELECT", 0), ("a", 7), ("FROM", 11), ("t", 16), ("", 17),
    ]


@pytest.mark.parametrize(
    "text, message, line, column",
    [
        ("SELECT 'abc", "unterminated string", 1, 8),
        ("SELECT\n  \"abc", "unterminated identifier", 2, 3),
        ("SELECT 1 /* x", "unterminated comment", 1, 10),
        ("SELECT a # b", "unrecognized character '#'", 1, 10),
        ("SELECT 12abc", "unrecognized token: \"12abc\"", 1, 8),
    ],
)
def test_errors_have_positions(text, message, line, column):
    with pytest.raises(SQLSyntaxError) as info:
        tokenize(text)
    assert info.value.message == message
    assert (info.value.line, info.value.column) == (line, column)
    assert str(info.value) == f"{message} (line {line}, column {column})"


def test_caret():
    with pytest.raises(SQLSyntaxError) as info:
        tokenize("SELECT 1,\n  2 # 3")
    assert info.value.caret() == "  2 # 3\n    ^"


def test_non_ascii_digits_are_identifier_characters():
    # As in SQLite, only ASCII digits make numbers ("²" used to crash int()).
    assert [(t.kind, t.value) for t in tokenize("² x² 2")][:3] == [("IDENT", "²"), ("IDENT", "x²"), ("INTEGER", 2)]


def test_characters_and_numbers_agree_with_sqlite():
    # SQLite's tokenizer: white space is " \t\n\f\r" only, every character
    # from U+0080 up is an identifier character (NBSP, U+2028, emoji...),
    # and numbers take '_' digit separators between two digits (3.46+),
    # except where the grammar wants a plain number (type sizes, PRAGMA).
    from sqlcompare import Pair

    pair = Pair()
    try:
        for sql in [
            "SELECT 1 AS €", "SELECT 1 AS a€b", "SELECT 1 AS \U0001f600", "SELECT 1 AS  x", "SELECT 1 + 2",
            "SELECT 1 AS  ", "SELECT 1²", "SELECT\v1", "SELECT\f1", "SELECT\x1c1", "SELECT　1",
            "SELECT 1_0", "SELECT 1__0", "SELECT 1_", "SELECT 0x1_F", "SELECT 0x_1", "SELECT 0x1_", "SELECT 1.5_5e1_0",
            "SELECT 1_.5", "SELECT 1._5", "SELECT 1_e5", "SELECT 1e_5", "SELECT .5_5", "SELECT 1.€", "SELECT 1$",
            "SELECT -9_223_372_036_854_775_808", "SELECT 9_223_372_036_854_775_808", "SELECT 0x7FFF_FFFF_FFFF_FFFF",
            "SELECT 0x1_0000_0000_0000_0000", "SELECT 1 LIMIT 1_0", "CREATE TABLE t(a VARCHAR(1_0))",
            "PRAGMA user_version = 1_0", "CREATE TABLE u(a DEFAULT 1_0, b DEFAULT -1_0)", "INSERT INTO u DEFAULT VALUES",
            "SELECT * FROM u", "CREATE TABLE v(a\x85, b)", "INSERT INTO v VALUES (1, 2)", "SELECT a\x85, a\x85 + 1 FROM v",
            "PRAGMA foreign_keys = ²",
        ]:
            pair.run(sql)
        for sql, parameters in [("SELECT ?²", (1,)), ("SELECT :a€", {"a€": 1}), ("SELECT $$, :1", {"$": 2, "1": 3})]:
            pair.run(sql, parameters=parameters)
        assert pair.mini.execute("SELECT 1 AS  x").columns == [" x"]
    finally:
        pair.close()
