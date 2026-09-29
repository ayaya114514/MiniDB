"""SQLite's core scalar, math and printf functions, and its conversions
between REAL and text, compared with sqlite3."""

import itertools
import random

import pytest

from sqlcompare import Pair

VALUES = [
    "NULL", "0", "1", "-1", "3", "-3", "2.5", "-2.5", "0.5", "1e20", "-0.0", "'abc'", "'ABC'", "''",
    "'héllo'", "' x '", "'5'", "'2.7'", "'a''b'", "x''", "x'61'", "x'00ff'", "'日本語'", "'xx  '",
    "9223372036854775807", "-9223372036854775808",
]
UNARY = [
    "hex({v})", "quote({v})", "unistr_quote({v})", "unicode({v})", "octet_length({v})", "sign({v})",
    "trim({v})", "ltrim({v})", "rtrim({v})", "round({v})", "typeof(round({v}))", "ceil({v})",
    "floor({v})", "trunc({v})", "typeof(ceil({v}))", "sqrt({v})", "exp({v})", "ln({v})", "log({v})",
    "log2({v})", "log10({v})", "sin({v})", "cos({v})", "tan({v})", "asin({v})", "acos({v})",
    "atan({v})", "degrees({v})", "radians({v})", "sinh({v})", "cosh({v})", "tanh({v})", "asinh({v})",
    "acosh({v})", "atanh({v})", "unhex({v})", "char({v})", "concat({v}, 'z', {v})",
    "concat_ws(',', {v}, NULL, {v})", "iif({v}, 'yes', 'no')", "iif({v}, 'yes')", "if({v}, 1, 0)",
    "replace({v}, '', 'z')", "replace('banana', {v}, 'o')", "instr('hello', {v})", "instr({v}, 'l')",
    "trim({v}, 'x')", "trim('xxhixx', {v})", "{v} GLOB '*'", "{v} GLOB 'a*'", "{v} GLOB '[a-c]*'",
    "glob('*b*', {v})", "{v} NOT GLOB '?'", "{v} LIKE 'a%' ESCAPE '!'", "like('%', {v})",
    "like('a%', {v}, '#')", "mod({v}, 3)", "pow({v}, 2)", "atan2({v}, 1)", "log(2, {v})",
    "printf('%d|%s|%.3f', {v}, {v}, {v})", "format('%5.2e', {v})", "CAST({v} AS TEXT)",
]


@pytest.fixture
def pair():
    p = Pair()
    yield p
    p.close()


@pytest.mark.parametrize("value", VALUES)
def test_scalar_functions(pair, value):
    for template in UNARY:
        pair.run("SELECT " + template.format(v=value))
    for start, length in itertools.product(["NULL", "0", "1", "2", "-1", "-2", "10", "'2'"],
                                           ["NULL", "0", "1", "2", "-1", "-2"]):
        pair.run(f"SELECT substr({value}, {start}, {length}), substr({value}, {start}), "
                 f"typeof(substr({value}, {start}))")
    for digits in ["NULL", "0", "1", "2", "-1", "3", "'2'", "40"]:
        pair.run(f"SELECT round({value}, {digits})")


def test_function_edge_cases(pair):
    for sql in [
        "SELECT instr('', ''), instr('abc', ''), instr(x'', x''), instr(x'616263', x'63'), instr(12345, 34)",
        "SELECT replace('aaa', 'a', 'aa'), replace(NULL, 'a', 'b'), replace('a', NULL, 'b'), replace(5, 5, 6)",
        "SELECT char(), char(65, NULL, 0x10FFFF, 0x110000, -1, 55296), unicode(''), unicode('é')",
        "SELECT unhex('4142'), unhex('41 42', ' '), unhex('41-42', '-'), unhex('4'), unhex('zz'), unhex(NULL)",
        "SELECT unistr('a\\u0041\\\\b\\+01F600\\0042'), unistr('\\U0001F600')",
        "SELECT unistr('\\x')", "SELECT unistr('\\u12')",
        "SELECT zeroblob(3), zeroblob(-1), length(zeroblob(5)), typeof(randomblob(4)), length(randomblob(-5))",
        "SELECT typeof(random()), random() BETWEEN -9223372036854775808 AND 9223372036854775807",
        "SELECT iif(0, 1, 0, 2), iif(0, 1, 0, 2, 3), iif(NULL, 1), if(1, 'a'), iif(0, 1, 1, 2, 3)",
        "SELECT concat(), concat(NULL)", "SELECT concat_ws(NULL, 1, 2), concat_ws('-')",
        "SELECT 'a_c' LIKE 'a!_c' ESCAPE '!', 'abc' LIKE 'a!_c' ESCAPE '!', 'a%' LIKE 'a#%' ESCAPE '#'",
        "SELECT 'a' LIKE 'a' ESCAPE 'xy'", "SELECT 'a' LIKE 'a' ESCAPE NULL",
        "SELECT 'abc' GLOB 'a?c', 'ABC' GLOB 'a*', 'a]' GLOB '[]]*', 'b' GLOB '[^a]', 'x' GLOB '[', '-' GLOB '[a-]'",
        "SELECT pi(), log(100), log(2, 8), ln(exp(2)), pow(2, 10), power(-8, 1.0/3), mod(-7, 3), mod(7.5, 2)",
        "SELECT ceil(-1.5), floor(-1.5), trunc(-1.5), ceil('1.5'), floor('abc'), ceil(9223372036854775807)",
        "SELECT sqrt(-1), ln(0), log(-1), acos(2), exp(1000), pow(0, -1), atanh(1), atanh(-1)",
        "SELECT pow(-13, 1e15 + 1), pow(-13, 1e15), pow(-0.0, -3), pow(-0.0, -2), pow(-2, 0.5), "
        "pow(-1e200, 3), pow(-1e200, -3), pow(-2, 1e20), pow(-13, 9007199254740993)",
        "SELECT sign('5'), sign('-5.5'), sign('abc'), sign(x'35'), sign(0.0), sign(-0.0)",
        "SELECT round(2.675, 2), round(-1.005, 2), round(0.5), round(-0.5), round(1234.5678, -2), "
        "round(1e300, 5), round('abc'), round(x'35')",
        "SELECT quote(0.1), quote(1e300), quote(-0.0), quote(1e999), quote(-1e999), quote(1.0/3), quote('a' || x'00' || 'b')",
        "SELECT hex(1.5), hex('é'), hex(NULL), hex(x'00ff'), quote(x'00ff'), unistr_quote('a' || char(1) || '\\')",
    ]:
        pair.run(sql)


def test_connection_state_functions():
    pair = Pair()
    for sql in [
        "SELECT last_insert_rowid(), changes(), total_changes()",
        "CREATE TABLE t (id INTEGER PRIMARY KEY, u UNIQUE)",
        "INSERT INTO t (u) VALUES (1), (2), (3)",
        "SELECT last_insert_rowid(), changes(), total_changes()",
        "UPDATE t SET u = u + 10 WHERE id < 3", "SELECT changes(), total_changes()",
        "INSERT INTO t VALUES (9, 11)", "SELECT changes(), total_changes(), last_insert_rowid()",
        "INSERT OR FAIL INTO t (u) VALUES (20), (21), (11)", "SELECT changes(), total_changes()",
        "DELETE FROM t WHERE id > 100", "SELECT changes(), total_changes()",
        "INSERT INTO t (u) SELECT last_insert_rowid() * 100", "SELECT u FROM t WHERE id = last_insert_rowid()",
        "SELECT changes(1)",
    ]:
        pair.run(sql)
    pair.close()


PRINTF_VALUES = [None, 0, 1, -42, 123456789, -9223372036854775808, 9223372036854775807, 3.14159, -2.5,
                 0.1, 1e-7, 1e20, 2.675, "abc", "", "héllo", "it's", "12abc", b"\x01'\x02", float("inf"),
                 -0.0, 5e-324]


@pytest.mark.parametrize("conversion", list("diuxXocszqQwfeEgGr%nk"))
def test_printf_matches_sqlite(pair, conversion):
    for flag, width, precision in itertools.product(
        ["", "-", "+", " ", "#", "!", "0", ",", "-0", "#0"], ["", "7", "*"], ["", ".0", ".3", ".*", ".20"],
    ):
        for value in PRINTF_VALUES:
            extra = ([-9] if width == "*" else []) + ([4] if precision == ".*" else [])
            params = [f"[%{flag}{width}{precision}{conversion}]"] + extra + [value]
            pair.run("SELECT printf(" + ", ".join("?" * len(params)) + ")", parameters=params)


def test_printf_formats(pair):
    for sql in [
        "SELECT printf('%s %d %f', 'a'), printf(NULL, 1), printf('%'), printf('abc%'), printf()",
        "SELECT printf('%5.2s|%-8s|%!5.3z|', 'abcdef', 'xy', 'héllo'), format('%d-%d', 3, 4)",
        "SELECT printf('%,d %,d %,d %,.3f', 1234567, -1000, 999, 1234567.891)",
        "SELECT printf('%.3c|%-4c|%c|', 'xyz', 'é', ''), printf('%lld %ld %lf', 5, 6, 7.5)",
        "SELECT printf('%5-d'), printf('%d%', 5), printf('%#.0f %#g %#x %#o', 3, 2, 255, 8)",
        "SELECT printf('%#q|%#Q|%#Q', 'a' || char(10) || '\\', 'plain', 'x' || char(31))",
        "SELECT printf('%.3r %r %r %r %r', 1, 2, 3, 11, 112), printf('%p', 255)",
        "SELECT printf('%010.3f|%-010d|%+.2e|% d', -3.14159, 42, 12345.678, 7)",
        "SELECT printf('%.1000000000d', 1)",
    ]:
        pair.run(sql)


def test_real_to_text_and_back_match_sqlite(pair):
    """REAL -> TEXT is printf('%!.17g') shortened when that round-trips, and
    TEXT -> REAL reads at most about 19 digits, exactly as SQLite 3.53."""
    rng = random.Random(7)
    reals = [0.1, 1 / 3, 2 / 3, 1e20, 1e-5, 2.675, 49.47, 0.30000000000000004, 1e16, 1e17,
             123456789012345678.0, 5e-324, 1.7976931348623157e308, 100.0, 1.5e-10]
    for _ in range(3000):
        kind = rng.random()
        if kind < 0.4:
            reals.append(rng.uniform(-1e6, 1e6))
        elif kind < 0.8:
            reals.append(rng.random() * 10.0 ** rng.randint(-320, 308))
        else:
            reals.append(round(rng.uniform(-1000, 1000), rng.randint(0, 8)))
    for i in range(0, len(reals), 100):
        chunk = reals[i:i + 100]
        pair.run("SELECT " + ", ".join(["CAST(? AS TEXT)"] * len(chunk)), parameters=chunk)
    literals = []
    for _ in range(3000):
        digits = "".join(rng.choice("0123456789") for _ in range(rng.randint(1, 30)))
        literals.append(f"{digits[:rng.randint(1, len(digits))]}.{digits}e{rng.randint(-330, 310)}")
    for i in range(0, len(literals), 100):
        chunk = literals[i:i + 100]
        pair.run("SELECT " + ", ".join(chunk))
        pair.run("SELECT " + ", ".join(f"CAST('{lit}' AS REAL)" for lit in chunk))


TIMES = [
    "'2024-02-29'", "'2023-02-29'", "'2024-01-31 12:34:56'", "'2024-01-31T12:34:56.789'", "'12:34:56'",
    "'12:34'", "'12:34:56.1234567'", "'2024-12-31 23:59:59.999'", "'0000-01-01'", "'-0100-03-01'",
    "'9999-12-31 23:59:59'", "'2024-06-15 10:00:00+05:30'", "'2024-06-15 10:00:00Z'", "2460000.5", "0",
    "1700000000", "-1", "'2460000.25'", "'abc'", "NULL", "'2024-13-01'", "'24:00'", "x'323032342d30312d3031'",
]
MODIFIERS = [
    "'+1 day'", "'-1 days'", "'+1.5 hours'", "'-90 seconds'", "'+1 month'", "'+13 months'", "'-1 year'",
    "'+0.5 year'", "'start of month'", "'start of year'", "'start of day'", "'weekday 0'", "'weekday 3'",
    "'weekday 6.5'", "'unixepoch'", "'julianday'", "'auto'", "'subsec'", "'floor'", "'ceiling'",
    "'+1-02-03'", "'-0001-01-01 12:00'", "'+12:30'", "'-01:15:30.5'", "'bogus'", "NULL", "'+1 DAY'",
    "'+10000 years'", "'+1 days '", "'localtime'", "'utc'",
]


@pytest.mark.parametrize("value", TIMES)
def test_date_and_time_functions(pair, value):
    for template in ["date({t})", "time({t})", "datetime({t})", "julianday({t})", "unixepoch({t})",
                     "strftime('%Y|%m|%d|%H|%M|%S|%f|%j|%J|%s|%w|%u|%W|%U|%V|%G|%g|%e|%k|%l|%I|%p|%P|%F|%R|%T|%%', {t})"]:
        pair.run("SELECT " + template.format(t=value))
    for modifier in MODIFIERS:
        pair.run(f"SELECT datetime({value}, {modifier}), julianday({value}, {modifier}), "
                 f"unixepoch({value}, {modifier}), date({value}, {modifier}, '+1 day')")
    for other in TIMES[:14]:
        pair.run(f"SELECT timediff({value}, {other})")


def test_date_functions_random_and_edge_cases(pair):
    rng = random.Random(9)
    for _ in range(500):
        y, mo, d = rng.randint(-100, 9999), rng.randint(1, 12), rng.randint(1, 31)
        t = (f"'{'-' if y < 0 else ''}{abs(y):04d}-{mo:02d}-{d:02d} {rng.randint(0, 23):02d}:"
             f"{rng.randint(0, 59):02d}:{rng.randint(0, 59):02d}.{rng.randint(0, 999):03d}'")
        mods = "".join(", " + rng.choice(MODIFIERS[:28]) for _ in range(rng.randint(0, 3)))
        pair.run(f"SELECT datetime({t}{mods}), strftime('%j %W %V %G %s %f', {t}), timediff({t}, '2000-01-01')")
    for sql in [
        "SELECT strftime('%', '2024-01-01'), strftime('%Q', '2024-01-01'), strftime('', '2024-01-01')",
        "SELECT strftime(NULL, 'now'), typeof(strftime('%Y')), typeof(date()), typeof(CURRENT_DATE)",
        "SELECT length(CURRENT_TIMESTAMP), length(CURRENT_TIME), date('now') = date(), "
        "julianday('now') = julianday('now'), datetime('now') = CURRENT_TIMESTAMP",
    ]:
        pair.run(sql)


def test_replace_checks_arguments_in_sqlites_order(pair):
    pair.run("SELECT replace('ab', '', NULL), replace('ab', 'a', NULL), replace(NULL, '', 'x'), replace(5, '', NULL)")


def test_trim_compares_bytes_and_stops_the_set_at_nul(pair):
    # trimFunc: the set is a C string, split into characters as SQLITE_SKIP_UTF8.
    pair.run("SELECT ltrim(10, char(0) || '-165'), trim('xxaxx', 'x' || char(0) || 'a'), trim('éaé', 'é'), "
             "hex(rtrim('aé', x'a9')), hex(ltrim(x'c3a9c3', x'c3')), trim(x'ffff41ff', x'ff'), trim(12.5, '15'), "
             "ltrim(x'c38080', x'c38080'), hex(trim(x'c3a9', x'c3')), hex(rtrim(x'41c3a9', x'a9')), "
             "trim('abc', ''), trim(NULL, 'a'), trim('a', NULL), rtrim('xé', 'é' || 'x'), hex(ltrim('é', x'c3'))")


@pytest.mark.parametrize("blob", ["x'ff'", "x'80'", "x'c3'", "x'c328'", "x'e282'", "x'e282ac'", "x'f09f9880'",
                                  "x'eda080'", "x'efbfbe'", "x'c0af'", "x'41ff42'", "x'fe'", "x'f8888080'",
                                  "x'00ff'", "x''", "x'e28241'"])
def test_text_that_is_not_utf8(pair, blob):
    """unicode() decodes as sqlite3Utf8Read (U+FFFD for invalid sequences);
    substr() and length() step through characters as SQLite does."""
    t = f"CAST({blob} AS TEXT)"
    pair.run(f"SELECT unicode({t}), length({t}), hex(substr({t}, 1, 1)), hex(substr({t}, 2)), "
             f"hex(substr({t}, -1)), hex(substr({t}, -2, 1)), instr({t}, 'B'), hex(replace({t}, x'00', 'z'))")


@pytest.mark.parametrize("value", ["'5'", "'5.0'", "'9007199254740993'", "'2251799813685248'", "'2251799813685247'",
                                   "'-2251799813685248'", "'1e2'", "' 5 '", "'abc'", "''", "'99999999999999999999'"])
def test_numbers_in_text_with_nul(pair, value):
    t = f"{value} || x'00'"
    for template in ["sum({t})", "typeof(sum({t}))", "ln({t})", "sqrt({t})", "sign({t})", "ceil({t})",
                     "typeof(ceil({t}))", "avg({t})", "total({t})", "mod({t}, 3)", "pow({t}, 2)"]:
        pair.run("SELECT " + template.format(t=t))
    pair.run("SELECT replace('ab', x'0061', 'z'), replace('a' || x'00', x'00', 'z')")
