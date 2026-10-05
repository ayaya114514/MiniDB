"""JSON functions, compared value by value with SQLite (results, JSONB bytes,
error messages)."""

import pytest

from minidb import jsonb
from sqlcompare import Pair


@pytest.fixture
def pair():
    return Pair(check_messages=True)


def run(pair, script):
    for sql in script:
        pair.run(sql)


def test_parse_and_render(pair):
    run(pair, [
        "SELECT json(' [1, 2] '), json('{\"a\" : [1, 2.50, \"x\\u0041\", true, false, null]}')",
        "SELECT json('{a:1,b:[1,2,],c:\"x\",}'), json('''a\\''b'''), json('\"\\x41\\v\\0\"')",
        "SELECT json('[0x7fffffffffffffff, 0xffffffffffffffff1]'), json('-.5e3'), json('[+1, -Infinity, NaN, +inf]')",
        "SELECT json('0x1F'), json('5.'), json('.5'), json('+5'), json('Infinity'), json('-0x1F'), json('1e5')",
        "SELECT json('// c' || char(10) || '[1 /* x */ , 2]'), json('[1,2] /* x */')",
        "SELECT json('\"\\uD83D\\uDE00\"'), json('\"é\\t\"'), json('\"a' || char(9) || 'b\"')",
        "SELECT json('[1,2] x')", "SELECT json('')", "SELECT json(' ')", "SELECT json('[1,,2]')",
        "SELECT json('{\"a\"}')", "SELECT json('{\"a\":1')", "SELECT json('[01]')", "SELECT json('\"abc')",
        "SELECT json(NULL), json(1), json(2.5), json('null')",
        "SELECT json_pretty('{\"a\":[1,2]}'), json_pretty('[]'), json_pretty('{}'), json_pretty('[1,{\"a\":[]}]', '  ')",
        "SELECT json_pretty('[1]', ''), json_pretty(NULL), json_pretty('[1,[2]]', NULL)",
        "SELECT json_error_position('{\"a\":1,'), json_error_position('[1,2] x'), json_error_position('{} x')",
        "SELECT json_error_position(''), json_error_position('{\"a\":01}'), json_error_position('\"abc')",
        "SELECT json_error_position('[1e]'), json_error_position('[é,1]'), json_error_position(NULL)",
        "SELECT json_error_position(x'00'), json_error_position('[1]'), json_error_position(jsonb('[1]'))",
    ])


def test_jsonb(pair):
    run(pair, [
        "SELECT hex(jsonb('{\"a\":1}')), hex(jsonb('[1,\"a\",{\"b\":null}]')), hex(jsonb('{a:0x10,b:.5,c:\"\\x41\"}'))",
        "SELECT hex(jsonb_array(1, 'x', json('{\"a\":1}'))), hex(jsonb_object('a', 1)), json(jsonb('[1,2]'))",
        "SELECT json_extract(jsonb('{\"a\":[1,2]}'), '$.a'), hex(jsonb_extract('{\"a\":[1,2]}', '$.a'))",
        "SELECT jsonb_extract('{\"a\":\"x\"}', '$.a'), hex(jsonb_extract('[1,2]', '$[0]', '$[1]'))",
        "SELECT hex(jsonb_set('{}', '$.a', 1)), hex(jsonb_insert('[]', '$[0]', 'x')), hex(jsonb_remove('[1,2]', '$[0]'))",
        "SELECT hex(jsonb_patch('{}', '{\"a\":1}')), hex(jsonb_replace('[1]', '$[0]', 2))",
        "SELECT json(x'7b7d'), json(x'0c'), hex(jsonb(x'0c')), json(x'13'), json(x'1331'), json_type(x'1331')",
        "SELECT json(x'313233'), json_valid(x'313233'), json(x'0b')",
        "SELECT hex(jsonb(json('[' || printf('%.*c', 300, 'x') || ']')))",
        "SELECT typeof(json_group_array(1)), typeof(jsonb_group_array(1)), hex(jsonb_group_array(1))",
        "SELECT hex(jsonb_group_object('a', 1))",
    ])


def test_constructors(pair):
    run(pair, [
        "SELECT json_array(1, json('[2]'), 'x', NULL, 2.5, json_quote('y')), json_array(), json_object()",
        "SELECT json_array(1.5, 0.1, 1e-7, 100.0, -3e15, 2.5e16, 9.223372036854776e18, 1e20, 1e300*1e300)",
        "SELECT json_array(x'01')", "SELECT json_array(x'00')", "SELECT json_array(jsonb('[1]'))",
        "SELECT json_object('a', 1, 'b', json('[2]'), 'c', 'x'), json_object('a', 1, 'a', 2)",
        "SELECT json_object('a', NULL, 'b', 1.0, 'c', json_object('d', 'e'))",
        "SELECT json_object('a')", "SELECT json_object(1, 2)", "SELECT json_object('a', x'00')",
        "SELECT json_quote('é'), json_quote(char(1, 31, 127)), json_quote(NULL), json_quote(1e300*1e300)",
        "SELECT json_quote(-0.0), json_quote(1.0), json_quote(123456789012345678), json_quote('a\"b\\c'), json_quote(json('[1]'))",
        "SELECT json_array(json_array(1), json('2'), '[3]', json_quote('x'))",
    ])


def test_extract_and_operators(pair):
    run(pair, [
        "SELECT json_extract('{\"a\":[1,{\"b\":2}]}', '$.a[1].b'), json_extract('{\"a\":[1,2]}', '$.a', '$.b')",
        "SELECT json_extract('[1,2]', '$[#-1]'), json_extract('[1,2,3]', '$[#-0]'), json_extract('[1,2,3]', '$[#-4]')",
        "SELECT json_extract('[1,2,3]', '$[#]'), json_extract('{\"a\":1}', '$.a', NULL), json_extract('{\"a\":1}', NULL)",
        "SELECT json_extract(NULL, '$'), json_extract('[1]', '$[0]', '$[1]'), json_extract('{\"a\":{\"b\":[1]}}', '$.a', '$.a.b[0]')",
        "SELECT json_extract('\"\\uD83D\\uDE00\"', '$'), json_extract('\"\\u00e9\\n\\t\"', '$'), json_extract('{\"a\\u0062\":1}', '$.ab')",
        "SELECT json_extract('{\"ab\":1}', '$.\"ab\"'), json_extract('{\"a.b\":1}', '$.\"a.b\"'), typeof(json_extract('{\"a\":1.5}', '$.a'))",
        "SELECT json_extract('{\"a\":9223372036854775808}', '$.a'), json_extract('[0x10, -0x10]', '$[1]'), json_extract('[-9223372036854775808]', '$[0]')",
        "SELECT json_extract('{\"a\":1}', '$.a.')", "SELECT json_extract('{\"a\":1}', '$a')", "SELECT json_extract('[1]', 'x')",
        "SELECT json_extract('[1,2,3]', '$[ 1]')", "SELECT json_extract('[1]', '$[')", "SELECT json_extract('{\"a\":1')",
        "SELECT '{\"a\":[1,2]}' -> '$.a', '{\"a\":[1,2]}' ->> '$.a[1]', '[1,2,3]' -> 1, '[1,2,3]' -> -1",
        "SELECT '{\"a\":{\"b\":1}}' -> 'a', '{\"a b\":1}' ->> 'a b', '{\"a\":1}' -> 'b', '[1,2]' -> '[1]', '[1,2]' ->> 0",
        "SELECT 1 -> '$', NULL -> '$', '{\"1\":2}' -> '1', '{\"1\":2}' ->> '\"1\"', '{\"a\":\"b\"}' -> 'a', '{\"a\":\"b\"}' ->> 'a'",
        "SELECT '{\"a\":2.50}' -> 'a', '{\"a\":2.50}' ->> 'a', '{\"a\":1}' -> 1.5, '[1,2]' -> 1.0, x'7b7d' -> '$', '[[1]]' -> 0 -> 0",
        "SELECT 'x' || '{\"a\":1}' -> 'a', '{\"a\":1}' -> 'a' || 'x', 2 * '[3]' ->> 0, '[1]' -> 0 = 1, '[1]' -> 0 COLLATE nocase",
        "SELECT json_array(json_extract('{\"a\":[1]}', '$.a')), json_array('{\"a\":[1]}' -> '$.a')",
        "SELECT json_array('{\"a\":[1]}' ->> '$.a'), json_array(json_extract('{\"a\":\"x\"}', '$.a'))",
    ])


def test_edit_functions(pair):
    run(pair, [
        "SELECT json_set('{\"a\":1}', '$.b', 2, '$.c[0]', 3, '$.a', json('[1]')), json_insert('[1,2]', '$[#]', 9)",
        "SELECT json_replace('{\"a\":1}', '$.a', 'x', '$.z', 1), json_set('{\"a\":{\"b\":1}}', '$.a.c.d', 5)",
        "SELECT json_set('[1]', '$[1]', 2, '$[5]', 3), json_insert('{\"a\":1}', '$.a', 9), json_set('1', '$', 2)",
        "SELECT json_set('1', '$.a', 2), json_set('{}', '$.\"x y\"', 1), json_set('{}', '$.a[0]', 1), json_set('{}', '$.a[#]', 1)",
        "SELECT json_set('{\"a\":1}', NULL, 2), json_set(NULL, '$.a', 1), json_set('[1]', '$[0]', NULL)",
        "SELECT json_set('{\"a\":1}', 'a', 2)", "SELECT json_set('{\"a\":1}', '$.a')", "SELECT json_set('{\"a\":1}', '$[', 2)",
        "SELECT json_set('[1]', '$[0]', x'01')", "SELECT json_insert('[1]', '$[0]', x'00')",
        "SELECT json_set('{\"a\":[1,2]}', '$.a[1]', json_object('x', json_array(1, 2)))",
        "SELECT json_set('{\"a\":\"" + "x" * 300 + "\"}', '$.b', 1), json_set('[1]', '$[1]', '" + "y" * 300 + "')",
        "SELECT json_remove('[1,2,3]', '$[1]', '$[0]'), json_remove('{\"a\":1,\"b\":2}', '$.a'), json_remove('{\"a\":1}', '$')",
        "SELECT json_remove('[1,2]', NULL), json_remove('[1,[2,3]]', '$[1][0]', '$[#-1]'), json_remove('{\"a\":{\"b\":1,\"c\":2}}', '$.a.b')",
        "SELECT json_remove('[1]'), json_remove('{\"a\":1}', '$.b'), json_remove('[1]', 'x')",
        "SELECT json_patch('{\"a\":1,\"b\":{\"c\":2}}', '{\"b\":{\"c\":null,\"d\":4},\"e\":5}'), json_patch('[1]', '{\"a\":1}')",
        "SELECT json_patch('{\"a\":1}', '[2]'), json_patch('{\"a\":{\"b\":1}}', '{\"a\":{\"b\":{\"c\":2}}}')",
        "SELECT json_patch('{\"a\":1}', '{\"a\":null,\"a\":2}'), json_patch(NULL, '{}'), json_patch('{}', NULL)",
        "SELECT json_patch('{\"a\":[1]}', '{\"a\":{\"b\":{\"c\":null}}}'), json_patch('1', '{\"a\":{\"b\":2}}')",
        "SELECT json_patch('{\"a\":1}', '{\"a\":1')",
    ])


def test_array_insert(pair):
    run(pair, [
        "SELECT json_array_insert('[1,2,3]', '$[1]', 9), json_array_insert('[1,2]', '$[#]', 9, '$[0]', 'x')",
        "SELECT json_array_insert('{\"a\":[1]}', '$.a[0]', json('[7]')), jsonb_array_insert('[1]', '$[1]', 2)",
        "SELECT json_array_insert('{}', '$.c.d[0]', 9), json_array_insert('[1]', '$[5]', 9), json_array_insert('[1]', '$', 9)",
        "SELECT json_array_insert('[1]', '$.a', 9), json_array_insert(NULL, '$[0]', 1), json_array_insert('[1]', NULL, 1)",
        "SELECT json_array_insert('{}', '$.a', 9)", "SELECT json_array_insert('[[1]]', '$[0].x', 9)",
        "SELECT json_array_insert('{\"a]\":1}', '$.a]', 9)", "SELECT json_array_insert('[1]', '$[0]')",
        "SELECT json_array_insert('[1]', '$[1')", "SELECT json_array_insert('{a:[0x10]}', '$.a[0]', 1)",
    ])


def test_nesting_limits(pair):
    # JSON_MAX_DEPTH (1000) levels: in the parser, lookups, edits that build
    # substructure, the renderers (compact and pretty) and json_patch.
    def nested(n, leaf="1"):
        return "[" * n + leaf + "]" * n
    run(pair, [
        f"SELECT length(json('{nested(1000)}')), json_valid('{nested(1001)}'), json_error_position('{nested(1001)}')",
        f"SELECT json('{nested(1001)}')", f"SELECT json_pretty('{nested(999)}') IS NOT NULL",
        f"SELECT json_pretty('{nested(1000)}')", f"SELECT json_extract('{nested(999)}', '$' || '{'[0]' * 999}')",
        f"SELECT json_extract('{nested(1000)}', '$' || '{'[0]' * 1000}')",
        f"SELECT length(json_set('{{}}', '$' || '{'.a' * 1000}', 1))", f"SELECT json_set('{{}}', '$' || '{'.a' * 1001}', 1)",
        f"SELECT json_set('[1]', '$' || '{'[0]' * 1001}', 1)",
        f"SELECT json(json_set(jsonb('{nested(600)}'), '$' || '{'[0]' * 599}', jsonb('{nested(500)}')))",
        f"SELECT length(json_patch('{{}}', '{'{"a":' * 1000 + '1' + '}' * 1000}'))",
        f"SELECT count(*) FROM json_tree('{nested(1000)}')",
    ])


def test_type_length_valid(pair):
    run(pair, [
        "SELECT json_type('{\"a\":[1,2.5,\"x\",null,true]}', '$.a[1]'), json_type('[1]'), json_type('{\"a\":1}', '$.b')",
        "SELECT json_type(NULL), json_type('[1]', NULL), json_type('1.0'), json_type('\"x\"'), json_type('null')",
        "SELECT json_type('false'), json_type('0x10'), json_type('1e5'), json_type('.5')",
        "SELECT json_type('{\"a\":1}', 'a')", "SELECT json_type('{\"a\":1}', '$.a.')",
        "SELECT json_array_length('[1,2,3]'), json_array_length('{\"a\":[1,2]}', '$.a'), json_array_length('{}')",
        "SELECT json_array_length('[]'), json_array_length('[1]', '$[1]'), json_array_length('[[1,2]]', '$[0]')",
        "SELECT json_array_length('[1]', 'x')", "SELECT json_array_length(NULL), json_array_length('[1]', NULL)",
        "SELECT json_valid('[1]'), json_valid('[1'), json_valid(NULL), json_valid('{a:1}'), json_valid('{a:1}', 2)",
        "SELECT json_valid('{a:1}', 6), json_valid(jsonb('[1]')), json_valid(jsonb('[1]'), 1), json_valid(jsonb('[1]'), 8)",
        "SELECT json_valid(x'00', 4), json_valid('', 1), json_valid(1), json_valid(1.5), json_valid('[1]', '3')",
        "SELECT json_valid('[1]', 0)", "SELECT json_valid('[1]', 16)", "SELECT json_valid('[1]', NULL)",
        "SELECT json_valid('[1]', 2.0)", "SELECT json_valid('[1]', 'x')",
    ])


def test_aggregates(pair):
    run(pair, [
        "CREATE TABLE t (id INTEGER PRIMARY KEY, j TEXT)",
        "INSERT INTO t VALUES (1, '{\"a\":1,\"b\":[2,3]}'), (2, '[4,5]')",
        "SELECT json_group_array(id), json_group_object(id, j) FROM t",
        "SELECT json_group_array(x) FROM (SELECT 1 x UNION ALL SELECT 'a' UNION ALL SELECT NULL "
        "UNION ALL SELECT json('{}') UNION ALL SELECT 2.5)",
        "SELECT json_group_object(k, v) FROM (SELECT 'a' k, 1 v UNION ALL SELECT NULL, 2 UNION ALL SELECT 3, json('[1]'))",
        "SELECT json_group_array(x) FROM (SELECT 1 x WHERE 0)", "SELECT json_group_object(x, x) FROM (SELECT 1 x WHERE 0)",
        "SELECT id % 2, json_group_array(j) FROM t GROUP BY 1 ORDER BY 1",
        "SELECT json_array(json_group_array(1))",
        "SELECT json_group_array(x) OVER (ORDER BY x ROWS BETWEEN 1 PRECEDING AND CURRENT ROW) FROM "
        "(SELECT '{\"a\":\"b,c\"}' x UNION ALL SELECT json('[1,{\"x\":[2,3]}]') UNION ALL SELECT 'z')",
        "SELECT json_group_object(k, v) OVER (ORDER BY k ROWS BETWEEN 1 PRECEDING AND CURRENT ROW) FROM "
        "(SELECT 'a' k, 1 v UNION ALL SELECT 'b', 'x,y' UNION ALL SELECT 'c', 3)",
        "SELECT json_array(x) FROM (SELECT json_group_array(1) OVER () x)",
        "SELECT json_group_array(x'01')", "SELECT json_group_array(x'00')",
    ])


def test_each_and_tree(pair):
    run(pair, [
        "CREATE TABLE t (id INTEGER PRIMARY KEY, j TEXT)",
        "INSERT INTO t VALUES (1, '{\"a\":1,\"b\":[2,3]}'), (2, '[4,5]'), (3, NULL), (4, '7')",
        "SELECT * FROM json_each('{\"a\":1,\"b\":[2,{\"c\":3}]}')",
        "SELECT * FROM json_tree('{\"a\":1,\"b\":[2,{\"c\":3}]}')",
        "SELECT t.id, e.key, e.value FROM t, json_each(t.j) e",
        "SELECT t.id, e.* FROM t, json_tree(t.j) e",
        "SELECT key, value, fullkey, path FROM json_each('{\"a\":{\"x\":[1,2]}}', '$.a.x')",
        "SELECT key, value, fullkey, path, id, parent FROM json_tree('{\"a\":{\"x\":[1,2]}}', '$.a')",
        "SELECT key, value, fullkey, path, id, parent FROM json_tree('{\"a\":{\"x\":[1,2]}}', '$.a.x[1]')",
        "SELECT key, value, fullkey, path, id, parent FROM json_each('{\"a\":{\"x\":[1,2]}}', '$.a.x[1]')",
        "SELECT key, value, fullkey, path, id, parent FROM json_each('3')",
        "SELECT key, value, fullkey, path, id, parent FROM json_each('{\"a b\":1,\"c\\\"d\":2,\"1x\":3}')",
        "SELECT key, value, type, atom, id, parent, fullkey, path FROM json_tree('[1,[2,{\"a b\":[3]}],{\"x.y\":{\"z\":null}}]')",
        "SELECT key, value, type, atom, id, parent, fullkey, path FROM json_tree('{\"a\":[1,2]}', '$.a[1]')",
        "SELECT key, value, type, atom, id, parent, fullkey, path FROM json_tree('[1,[2]]', '$[1]')",
        "SELECT key, value, type, atom, id, parent, fullkey, path FROM json_each('{\"a\":{\"b\":1}}', '$.a')",
        "SELECT key, value, type, atom, id, parent, fullkey, path FROM json_each('{\"a\":{\"b\":1}}', '$.a.b')",
        "SELECT key, value, type, atom, id, parent, fullkey, path FROM json_tree('5')",
        "SELECT key, value, type, atom, id, parent, fullkey, path FROM json_each('{\"a\\\"b\":1,\"c\\\\u0041\":2,\"d\":\"e\\n\"}')",
        "SELECT key, value, type, atom, id, parent, fullkey, path FROM json_tree('{\"a\":{}}')",
        "SELECT * FROM json_each('[1,2', '$')", "SELECT * FROM json_each('[1,2]', 'x')", "SELECT * FROM json_each('[1,2]', '$.q')",
        "SELECT * FROM json_each(NULL)", "SELECT json, root FROM json_each('[1]')", "SELECT key FROM json_each('[1]', NULL)",
        "SELECT * FROM json_each('[1]', '$', 3)", "SELECT * FROM json_each()", "SELECT * FROM json_each",
        "SELECT count(*) FROM json_each('[1,2,3]') WHERE value > 1",
        "SELECT j.value FROM json_each('[3,1,2]') j ORDER BY j.value",
        "SELECT a.value, b.value FROM json_each('[1,2]') a JOIN json_each('[2,3]') b ON a.value = b.value",
        "SELECT (SELECT count(*) FROM json_each(t.x)) FROM (SELECT '[1,2]' x UNION ALL SELECT '[3]') t",
        "SELECT e.value FROM (SELECT '[1,2]' x) t LEFT JOIN json_each(t.x) e ON e.value > 5",
        "SELECT json_array(e.value), json_array(e.atom) FROM json_each('[[1],{\"a\":2},3,\"x\"]') e",
        "SELECT key, value, typeof(value), atom, fullkey FROM jsonb_each('{\"a\":[1,{\"b\":2}],\"c\":\"x\"}')",
        "SELECT key, value, type, atom, id, parent, json FROM jsonb_tree('[1,[2,{\"c\":[]}]]')",
        "SELECT json(value), value -> '$[0]', json_array(value) FROM jsonb_each('[[1],{\"a\":2}]')",
        "SELECT * FROM jsonb_tree(jsonb('{\"a\":[1]}'), '$.a')",
        # a subquery in the arguments that uses its own json_each() is not a reference to this one
        "SELECT t.id, e.value FROM t, json_each(json_array(t.id, (SELECT group_concat(key) FROM json_each('[5,6]')))) e",
        "SELECT t.id, e.key FROM t, json_tree(json_object('k', (SELECT max(value) FROM json_each(t.j)))) e",
    ])


def test_subtype_through_queries(pair):
    """Text from a JSON function keeps the JSON subtype through expressions
    and scalar subqueries, but not through a table or a subquery in FROM."""
    run(pair, [
        "CREATE TABLE u (a, b TEXT)", "INSERT INTO u VALUES (json('[1]'), json('[2]'))",
        "SELECT json_array(a, b) FROM u", "UPDATE u SET a = json('[5]')", "SELECT json_array(a, b) FROM u",
        "CREATE VIEW v AS SELECT json('[1]') x", "SELECT json_array(x) FROM v",
        "SELECT json_array((SELECT json('[1]')))",
        "SELECT json_array(x) FROM (SELECT json('[1]') x)",
        "SELECT json_array(x) FROM (SELECT json('[1]') x ORDER BY 1)",
        "WITH c(x) AS (SELECT json('[1]')) SELECT json_array(x) FROM c",
        "WITH RECURSIVE c(x, n) AS (SELECT json('[1]'), 1 UNION ALL SELECT json_array(x), n + 1 FROM c WHERE n < 3) "
        "SELECT x, json_array(x) FROM c",
        "SELECT json_array(x) FROM (SELECT json('[1]') x UNION ALL SELECT json('[2]'))",
        "SELECT json_array(CAST(json('[1]') AS TEXT)), json_array(json('[1]') COLLATE nocase), json_array(+json('[1]'))",
        # JSONB with the subtype (jsonb_array, jsonb_object, jsonb_group_object, jsonb_each's containers):
        # CAST to TEXT keeps it
        "SELECT hex(json_array(CAST(jsonb_array(1) AS TEXT))), hex(json_array(CAST(jsonb_object('a', 1) AS TEXT)))",
        "SELECT json_array(CAST(jsonb('[1]') AS TEXT)), json_array(CAST(jsonb_extract('[[1]]', '$[0]') AS TEXT))",
        "SELECT hex(json_array(CAST((SELECT jsonb_group_object('a', 1)) AS TEXT))), "
        "json_array(CAST((SELECT jsonb_group_array(1)) AS TEXT))",
        "SELECT hex(json_array(CAST(value AS TEXT))), json_array(CAST(atom AS TEXT)) FROM jsonb_tree('{\"a\":[1],\"b\":2}')",
        "SELECT hex(json_array(CAST(x AS TEXT))) FROM (SELECT value x FROM jsonb_each('[[1]]'))",
        "SELECT hex(json_array(CAST(x AS TEXT))) FROM (SELECT jsonb_array(1) x)",
        "SELECT hex(json_array(CAST(iif(1, jsonb_array(1), 0) AS TEXT))), jsonb_array(1) < x'00', jsonb_array(1) > 5",
        "CREATE TABLE jb (b)", "INSERT INTO jb SELECT jsonb_array(1)", "SELECT json_array(CAST(b AS TEXT)) FROM jb",
        # only a bare column of a subquery SQLite flattens keeps the subtype
        "SELECT json_array(x), json_array(y) FROM (SELECT json('[1]') x, value y FROM json_each('[[2]]'))",
        "SELECT json_array(x) FROM (SELECT coalesce(value, 1) x FROM json_each('[[1]]'))",
        "SELECT json_array(x) FROM (SELECT +value x FROM json_each('[[1]]'))",
        "SELECT json_array(x) FROM (SELECT value x, 0 y FROM json_each('[[1]]') LIMIT 5) WHERE y = 0",
        "SELECT json_array(x) FROM (SELECT max(value) x FROM json_each('[[1]]'))",
        "SELECT json_array(x) FROM (SELECT DISTINCT value x FROM json_each('[[1]]'))",
        "SELECT json_array(value) FROM (SELECT * FROM json_each('[[2]]'))",
        "SELECT json_array(x) FROM (SELECT x FROM (SELECT value x FROM json_each('[[2]]')))",
        "WITH c AS (SELECT value v, json('[1]') w FROM json_each('[[3]]')) SELECT json_array(v), json_array(w) FROM c",
        "SELECT json_array(coalesce(json('[1]'), 1)), json_array(iif(1, json('[1]'), 2)), json_array(CASE WHEN 1 THEN json('[1]') END)",
        "SELECT json_array(max(json('[1]'))), json_array(min(json('[1]'), json('[2]'))), json_array(nullif(json('[1]'), 2))",
        "SELECT json_array(upper(json('[1]'))), json_array(json('[1]') || ''), json_array(trim(json('[1]')))",
        "SELECT json_array(x) FROM (SELECT value x FROM json_each('[[1]]'))",
    ])


def test_parser_round_trip():
    """Every JSONB the parser makes renders back to the same text SQLite would
    (minimal headers: a re-parse of the rendering gives the same bytes)."""
    for text in ['{"a":[1,2,{"b":null}],"c":"x"}', '[' + ','.join(['"' + 'x' * 20 + '"'] * 20) + ']',
                 '"' + 'y' * 70000 + '"']:
        blob, nonstandard = jsonb.parse_text(text)
        assert not nonstandard
        assert jsonb.to_text(blob) == text
        assert jsonb.parse_text(jsonb.to_text(blob))[0] == blob
        assert jsonb.validity_check(blob, 0, len(blob)) == 0


def test_parse_cache(pair):
    """SQLite's per-statement JSON parse cache: the text an edit function
    returns comes back from the cache with the JSONB it was rendered from,
    so jsonb() and json_valid() of equal text can differ (ParseCache)."""
    run(pair, [
        'SELECT json_valid(json_set(\'{a:0x10}\',\'$.b\',1)), json_valid(\'{"a":16,"b":1}\')',
        'SELECT json_valid(json_set(\'{a:0x10}\',\'$.b\',1)), json_valid(json_set(\'{"a":16}\',\'$.b\',1))',
        'SELECT json_set(\'{a:0x10}\',\'$.b\',1), json_valid(\'{"a":16,"b":1}\')',
        'SELECT json_valid(\'{"a":16,"b":1}\'), json_set(\'{a:0x10}\',\'$.b\',1)',
        'SELECT json_set(\'{a:0x10}\',\'$.b\',1), hex(jsonb(\'{"a":16,"b":1}\')), json_valid(\'{"a":16,"b":1}\', 1), json_valid(\'{"a":16,"b":1}\', 2)',
        'SELECT json_remove(jsonb(\'{a:0x10, b:2}\'), \'$.b\'), hex(jsonb(\'{"a":16}\'))',
        'SELECT json_set(\'{a:0x10}\',\'$.b\',1), hex(jsonb(\'{"a":16,"b":1}\')), hex(jsonb(\'[1]\')), hex(jsonb(\'[2]\')), hex(jsonb(\'[3]\')), hex(jsonb(\'[4]\')), hex(jsonb(\'{"a":16,"b":1}\'))',
        'SELECT json_set(\'{a:0x10}\',\'$.b\',1), hex(jsonb(\'{"a":16,"b":1}\')), hex(jsonb(\'[1]\')), hex(jsonb(\'[2]\')), hex(jsonb(\'[3]\')), hex(jsonb(\'{"a":16,"b":1}\'))',
        'SELECT json_set(\'{a:0x10}\',\'$.b\',1), hex(jsonb(\'[1]\')), hex(jsonb(\'[2]\')), hex(jsonb(\'[3]\')), hex(jsonb(\'{"a":16,"b":1}\'))',
        'SELECT json_set(\'{a:0x10}\',\'$.b\',1), hex(jsonb(\'[1]\')), hex(jsonb(\'[2]\')), hex(jsonb(\'[3]\')), hex(jsonb(\'[4]\')), hex(jsonb(\'{"a":16,"b":1}\'))',
        "SELECT hex(jsonb(x)) FROM (SELECT json_set('{a:0x10}','$.b',1) x)",
        "SELECT hex(jsonb(json_set('{a:0x10}','$.b',1))) FROM (SELECT 1 UNION ALL SELECT 2)",
        'SELECT hex(jsonb_set(\'{"a":16,"b":1}\', \'$.c\', 1)), json_set(\'{a:0x10}\',\'$.b\',1)',
        'SELECT json_set(\'{a:0x10}\',\'$.b\',1), hex(jsonb_set(\'{"a":16,"b":1}\', \'$.c\', 1))',
        'SELECT json_set(\'{a:0x10}\',\'$.b\',1), json_error_position(\'{"a":16,"b":1}\'), hex(jsonb_extract(\'{"a":16,"b":1}\', \'$.a\'))',
        'CREATE TABLE t(x)',
        'INSERT INTO t VALUES (1), (2)',
        'SELECT json_set(\'{a:0x10}\',\'$.b\',x), hex(jsonb(\'{"a":16,"b":1}\')) FROM t',
        'SELECT hex(jsonb(\'{"a":16,"b":1}\')), json_set(\'{a:0x10}\',\'$.b\',x) FROM t',
        'SELECT hex(jsonb(json_patch(\'{}\', \'{a:1,b:0x10,"c":[.5,],}\')))',
        'SELECT hex(jsonb(json_replace(\'{a:1,b:0x10,"c":[.5,],}\', \'$.a.b\', 0)))',
        'SELECT json_patch(\'{}\', \'{a:0x10}\'), json_valid(\'{"a":16}\'), hex(jsonb(\'{"a":16}\'))',
        "SELECT json_remove('[0x1, 2]', '$[1]'), hex(jsonb('[1]')), json_remove('[0x2]'), hex(jsonb('[2]'))",
        "SELECT key, value FROM json_each(json_set('{a:0x10}','$.b',1))",
        # Only a text that left the JsonString's 100 static bytes is cached
        # (a hex integer and a control character reserve room ahead).
        *(f"SELECT hex(jsonb(json_set('{{\"a\":\"{'x' * n}\"}}', '$.b', {v})))"
          for n in (80, 85, 86, 87, 88, 90) for v in ("'it''s'", "'a' || char(10) || 'b'", "json('[0x1F]')")),
        'SELECT json_set(\'{a:0x10}\',\'$.b\',1), (SELECT hex(jsonb(\'{"a":16,"b":1}\')))',
        'SELECT json_set(\'{a:0x10}\',\'$.b\',1) FROM t WHERE hex(jsonb(\'{"a":16,"b":1}\')) LIKE \'BC%\'',
        'SELECT hex(jsonb(\'{"a":16,"b":1}\')) FROM t WHERE json_set(\'{a:0x10}\',\'$.b\',1) IS NOT NULL',
    ])


def test_json_text_compared_under_a_collation(pair):
    """Text with the JSON subtype is text for NOCASE and RTRIM too."""
    for sql in [
        "CREATE TABLE c (n BLOB COLLATE NOCASE, r COLLATE RTRIM)",
        "INSERT INTO c VALUES ('B0', '[1]  ')",
        "SELECT '[' < n, json('[1]') < n, json_extract('[1]', '$') < n, json_extract('[1]', '$', '$') < n, "
        "json_array(1) < n FROM c",
        "SELECT n > json_extract('[1]', '$', '$'), json('[1]') = r, json('[1]') IN (r), r = json_array(1) FROM c",
        "SELECT * FROM c WHERE json('[1]') = r",
        "SELECT max(json_array('A') COLLATE nocase, '[\"a\"]'), json_array('A') COLLATE nocase IN ('[\"a\"]')",
    ]:
        pair.run(sql)


def test_damaged_jsonb_agrees_with_sqlite(pair):
    # Random damage to valid JSONB (bytes changed, cut, inserted): what
    # json_valid(x, 8) - the deep check - and the functions reading it say.
    import random

    rng = random.Random(42)
    import json

    documents = [json.dumps(d, ensure_ascii=False) for d in (
        {"a": [1, 2.5, -3e10, "x\u00e9\n\"q\"", True, False, None], "b": {"c": {}}},
        [1, [2, [3, [4, [5]]]], "a longer string, long enough for a 2-byte size", {"k": 0.0001}],
        "plain", 123456789012345678901234567890, {"": [], "z": {"y": 0}},
    )] + ["[1e400,-1e400,0x1F,'s',Infinity]"]
    blobs = [pair.lite.execute("SELECT jsonb(?)", (d,)).fetchone()[0] for d in documents]
    for blob in blobs:
        for _ in range(60):
            damaged = bytearray(blob)
            roll = rng.random()
            if roll < 0.5:
                for _ in range(rng.randint(1, 3)):
                    damaged[rng.randrange(len(damaged))] = rng.randrange(256)
            elif roll < 0.75:
                del damaged[rng.randrange(len(damaged)):]
            else:
                damaged.insert(rng.randrange(len(damaged) + 1), rng.randrange(256))
            for sql in ("SELECT json_valid(?, 8), json_valid(?, 4), json_valid(?, 12)",
                        "SELECT json(?)", "SELECT json_type(?)", "SELECT json_array_length(?)",
                        "SELECT json_type(?, '$.a'), ? -> '$[1]', json_extract(?, '$.a[3]')",
                        "SELECT json_patch(?, '{\"a\":null,\"q\":{\"r\":1}}')", "SELECT json_patch('{\"a\":{}}', ?)"):
                pair.run(sql, parameters=(bytes(damaged),) * sql.count("?"))


def test_json5_inputs_agree_with_sqlite(pair):
    import random

    rng = random.Random(7)
    pieces = ["{", "}", "[", "]", ",", ":", " ", "\t", "\n", "\r", "\u00a0", "\u2028", "\ufeff", "/* c */", "// c\n",
              "1", "-2.5", "+3", ".5", "5.", "0x1f", "0XAB", "-0x10", "1e5", "Infinity", "-Infinity", "NaN", "null",
              "true", "false", "'s'", "\"t\"", "'a\\'b'", "\"\\x41\"", "\"\\v\\0\"", "'\\\n'", "key", "$k_1", "\"k\""]
    for _ in range(400):
        text = "".join(rng.choice(pieces) for _ in range(rng.randint(1, 10)))
        pair.run("SELECT json_valid(?, 2), json_valid(?, 1), json_valid(?)", parameters=(text,) * 3)
        pair.run("SELECT json(?)", parameters=(text,))
        pair.run("SELECT hex(jsonb(?))", parameters=(text,))


def test_damaged_jsonb_fails_cleanly():
    # On damaged JSONB, SQLite's json_each, json_tree and the editing
    # functions may read past the value (undefined); MiniDB must at least
    # fail with an SQL error - no internal exception, no endless loop (as
    # json_patch once had: a target label with a broken header).
    import random
    import signal

    from minidb.database import Database
    from minidb.errors import OperationalError

    db = Database()
    rng = random.Random(3)
    blob = db.execute("""SELECT jsonb('{"a":[1,2.5,"x",true,null],"b":{"c":{},"d":[1,{"e":"f"}]}}')""")[0][0]
    alarm = getattr(signal, "SIGALRM", None)

    def timeout(*_):
        raise TimeoutError("endless loop")

    if alarm is not None:
        signal.signal(alarm, timeout)
    try:
        for _ in range(400):
            damaged = bytearray(blob)
            for _ in range(rng.randint(1, 3)):
                damaged[rng.randrange(len(damaged))] = rng.randrange(256)
            for sql in ("SELECT * FROM json_each(?)", "SELECT * FROM json_tree(?)", "SELECT json_remove(?, '$[0]', '$.a')",
                        "SELECT json_set(?, '$.new', 1)", "SELECT jsonb_insert(?, '$.a[#]', 2)",
                        "SELECT json_patch(?, '{\"a\":null}')", "SELECT json_replace(?, '$.b.d', 0)"):
                if alarm is not None:
                    signal.alarm(10)
                try:
                    db.execute(sql, (bytes(damaged),))
                except OperationalError:
                    pass
                finally:
                    if alarm is not None:
                        signal.alarm(0)
    finally:
        if alarm is not None:
            signal.signal(alarm, signal.SIG_DFL)


def test_damaged_jsonb_corner_cases(pair):
    # Found by the damaged-JSONB comparison above, each a detail of json.c.
    for sql, blob in [
        # jsonReturnFromBlob: null / true / false with a payload are malformed
        ("SELECT json_extract(?, '$.a[3]')", b'\xcc.\x17a\xcb$\x13152.5b\x0e-70000000000.0\xa8x\xc3\xa9\\n\\"q\\"'
                                              b'\x01\x02\x00\x17b<\x17c\x0c'),
        # the pretty printer checks only a label for running past the end
        ("SELECT json_pretty(?)", b'\xcc\x1d\x17a\xcc\x12\x17b\xcb\x0e\x01\xab\x00\xac\x17nxd\\u0H41\x17eE-0.0'),
        # a FLOAT5 of just "-" is malformed
        ("SELECT ? -> '$[1]', json_extract(?, '$[1]')", b'\xcb\x1aU1e400\x16-1e400D0x1F\x17eU9e949'),
        # jsonHexToInt does not check its digit: \u00H1 is U+0011
        ("SELECT ? ->> '$.a.b[2]'", b'\xcc\x1d\x17a\xcc\x12\x17b\xcb\x0e6\x02\x00\xac\x17cxd\\u00H1\x17eE-\xb2.0'),
        # jsonAppendSeparator: no comma after '[' or '{'
        ("SELECT json_extract(?, '$.a', '$[0]', '$.b.c')", b'\xcb\x1aU1e40{e-\te430D0x1F\x17sU9e999'),
        ("SELECT json_array(?, 1)", b'U1e40{'),
        # labels compare as sqlite3Utf8ReadLimited code points: a lone 0xE0 is 0, the end
        ("SELECT json_patch('{\"a\":{}}', ?)", b'\x9c\x07\x0b\x19\xe0L\x17y\x130'),
    ]:
        pair.run(sql, parameters=(blob,) * sql.count("?"))
    for sql in [
        "SELECT json_extract('{\"a\\u0000b\":1}', '$.\"a\\u0000c\"')",
        "SELECT json_set('{\"a\\u0000x\":1}', '$.\"a\\u0000y\"', 2)",
        "SELECT json_extract('{\"\\u00e9\\u0080\":1}', '$.\"\u00e9\\u0080\"')",
        "SELECT json('\"\\x4g\"'), json('{\"\\x41\":1}') -> '$.A'",
    ]:
        pair.run(sql)


def _jsonb(kind, payload):
    """A JSONB element: the smallest header for the payload, then the payload."""
    n = len(payload)
    if n <= 11:
        return bytes((n << 4 | kind,)) + payload
    if n <= 0xFF:
        return bytes((0xC0 | kind, n)) + payload
    if n <= 0xFFFF:
        return bytes((0xD0 | kind,)) + n.to_bytes(2, "big") + payload
    return bytes((0xE0 | kind,)) + n.to_bytes(4, "big") + payload


def test_json_corner_inputs_agree_with_sqlite(pair):
    # Hand-picked inputs for the branches random documents rarely reach.
    texts = [
        # JSON5 white space: U+1680, U+2000-200A, U+2028/9, U+202F, U+205F, U+3000, U+FEFF, and near misses
        "[1,\u1680 2]", "[\u2000\u200a1]", "[1\u202f]", "[\u205f1]", "[\u30001]", "\ufeff[1]", "[\u2028\u20291]",
        "[\u16811]", "[\u200b1]", "[\u2060 1]", "[\u3001]", "[1\ufeff]", "[\uffef1]",
        # comments
        "[1 /* unterminated", "[1 // to the end", "[1 //c\u2028 ]", "[1 //c\u2029, 2]", "[1 /** a **/ ]", "[1 /",
        # white space before separators, and missing or doubled ones
        '{"a":1 ,"b":2 }', '{"a" : 1 , }', "[1 , 2 ]", "[1 ,]", "[ 1 ]", "{ }", "[ ]", '{"a":1 x}', "[1 x]",
        '{"a":1,,}', "[1,,2]", "{a:1 /*c*/ ,b:2}", '{"a"\t:\n1\r}', "[1\n,\n]",
        # identifiers with escapes
        "{\\u0061b:1}", "{a\\u0062:1}", "{\\u00e9:1}", "{a\\u00:1}", "{$:1,_x:2}",
        # string escapes and line continuations
        "'a\\\r\nb'", "'a\\\rb'", "'a\\\nb'", "'a\\\u2028b'", "'a\\\u2029b'", "'a\\\u2027b'", '"a\\x41"', '"\\x4"',
        '"\\0"', '"\\01"', "'it\"s'", '"\\u12"', '"a\rb"', "'\\v'", '"\\\'"', '"\\u00e9\\ud83d\\ude00"', '"\\ud83d"',
        '"\\uDC00x"', '"tab\there"', "'\\\r\n\\\nx'", '"\\', '"abc',
        # numbers
        "-01", "00", "01.5", "1.e5", "1.", "1.x", "1e5e5", "1e+5", "1e-", "1e", ".e5", "-.5", "+.5", "+1", "-+1", "0x",
        "0xG", "-0x1f", "+0x1f", "1.5x", "1ee5", "1.5.5", "-Infinity", "+Infinity", "-NaN", "NaN", "Infinityx",
        "1E400", "-", "+", ".", "-0", "-0.0e-0", "0X7FFFFFFFFFFFFFFF", "0x10000000000000000", "[.5,5.,-.5e3]",
        # NUL ends the text
        "[1]\x00junk", "[1,\x002]",
        # nesting
        "[" * 1000 + "]" * 1000, "[" * 1001 + "]" * 1001, '{"a":' * 999 + "1" + "}" * 999,
    ]
    for text in texts:
        pair.run("SELECT json_valid(?, 1), json_valid(?, 2), json_error_position(?)", parameters=(text,) * 3)
        pair.run("SELECT json(?)", parameters=(text,))
        pair.run("SELECT hex(jsonb(?))", parameters=(text,))
        pair.run("SELECT json_pretty(?)", parameters=(text,))
        pair.run("SELECT json_extract(?, '$'), json_extract(?, '$[0]')", parameters=(text,) * 2)
    INT, INT5, FLOAT, FLOAT5, TEXT, TEXTJ, TEXT5, TEXTRAW, ARRAY, OBJECT = 3, 4, 5, 6, 7, 8, 9, 10, 11, 12
    deep = _jsonb(ARRAY, b"")
    for _ in range(1001):
        deep = _jsonb(ARRAY, deep)
    blobs = [
        b"\x03", b"\x13-", b"\x131", b"\x23-1", b"\x23-x", b"\x240x", b"\x34-0x", b"\x341x1", b"\x340y1", b"\x340x1",
        b"\x44-0x1", b"\x340xg", b"\x151", b"\x25.5", b"\x26.5", b"\x16.", b"\x36-.5", b"\x25-1", b"\x261.",
        b"\x251e", b"\x351e+", b"\x451e5e", b"\x551.5.5", b"\x361e+", b"\x461.e5", b"\x45-1e5", b"\x46Inf",
        _jsonb(TEXTJ, b'a\\"b'), _jsonb(TEXTJ, b"a\\"), _jsonb(TEXTJ, b"\\u12"), _jsonb(TEXTJ, b"\\u00e9"),
        _jsonb(TEXTJ, b"\\x41"), _jsonb(TEXTJ, b'a"b'), _jsonb(TEXTJ, b"a\x01b"), _jsonb(TEXT, b'a"b\\'),
        _jsonb(TEXT5, b"\\x41"), _jsonb(TEXT5, b"\\x4"), _jsonb(TEXT5, b"a\\\r\nb"), _jsonb(TEXT5, b"a\\\rb"),
        _jsonb(TEXT5, b"a\\\nb"), _jsonb(TEXT5, "a\\\u2028b".encode()), _jsonb(TEXT5, b"a\\\xe2\x80"),
        _jsonb(TEXT5, b"a\\\xe2\x80\xa7b"), _jsonb(TEXT5, b"\\'\\v\\0"), _jsonb(TEXT5, b"\\01"), _jsonb(TEXT5, b'a"b'),
        _jsonb(TEXT5, b"a\x01\x1fb"), _jsonb(TEXT5, b"a\\"), _jsonb(TEXT5, b"\\q"), _jsonb(TEXT5, b"\\\\\\/"),
        _jsonb(TEXT5, b"\\\r\n\\\nx"), _jsonb(TEXTRAW, b'a"\\\x01'),
        _jsonb(OBJECT, _jsonb(TEXT, b"a")), _jsonb(OBJECT, _jsonb(INT, b"1") + _jsonb(INT, b"2")),
        _jsonb(OBJECT, _jsonb(TEXT, b"a") + b"\x13"), _jsonb(ARRAY, b"\x13"), _jsonb(ARRAY, b"\x131\x00\x00"),
        b"\xf3\x00\x00\x00\x00\x00\x00\x00\x011", b"\x240x", b"\x34-0x", b"\xc4\x130x10000000000000000",
        b"\xc4\x130x00000000000000001", b"\xc4\x13-0x8000000000000000", b"\xc4\x120xffffffffffffffff", b"\xf3\x00\x00\x00\x01\x00\x00\x00\x011", deep,
    ]
    for blob in blobs:
        pair.run("SELECT json_valid(?, 4), json_valid(?, 8), json_error_position(?)", parameters=(blob,) * 3)
        pair.run("SELECT json(?)", parameters=(blob,))
        pair.run("SELECT json_pretty(?)", parameters=(blob,))
        pair.run("SELECT json_extract(?, '$')", parameters=(blob,))
    for sql in [
        "SELECT json_extract('{\"a\":1}', '$.\"a')", "SELECT json_extract('[1,2]', '$[#-1x]')",
        "SELECT json_extract('[1,2]', '$[#-3]')", "SELECT json_array_insert('{\"a\":[1]}', '$.a', 2)",
        "SELECT json_array_insert('{\"a\":[1]}', '$.b[0]', 2)", "SELECT json_array_insert('[1]', '$[#]', 2)",
        "SELECT json_extract(?, '" + "$" + ".a" * 1000 + "')",
        "SELECT json_set('{}', '$" + ".a" * 1000 + "', 1)",
        "SELECT json_patch(?, ?)",
    ]:
        doc = '{"a":' * 999 + "1" + "}" * 999
        pair.run(sql, parameters=(doc,) * sql.count("?"))
    for patch in [_jsonb(OBJECT, _jsonb(TEXT, b"a")), _jsonb(OBJECT, _jsonb(TEXT, b"a") + b"\x0c\x00"),
                  _jsonb(OBJECT, _jsonb(INT, b"1") + _jsonb(INT, b"2")), _jsonb(OBJECT, b"\x17a\x13")]:
        pair.run("SELECT json_patch('{\"a\":1}', ?), json_patch(?, '{\"a\":1}')", parameters=(patch, patch))


def test_json_function_corners_agree_with_sqlite(pair):
    for sql in [
        "SELECT json_array(x'01')", "SELECT json_array(jsonb('[1]'), x'')", "SELECT json_quote(x'01')",
        "SELECT json_quote(jsonb('{\"a\":1}'))", "SELECT json_object('a', x'0102')", "SELECT json_array(9e999, -9e999, 1e15, 2.0)",
        "SELECT json_quote(9e999), json_quote(-0.0), json_quote(1e300)", "SELECT json_array(json('1'), jsonb('2'))",
        "SELECT json_group_object(char(97, 0, 98), 1)", "SELECT json_group_object(NULL, 1), json_group_object('a', x'01')",
        "SELECT json_group_array(x) FROM (SELECT x'01' AS x)",
        "SELECT json_group_array(json(x)) OVER (ROWS BETWEEN 1 PRECEDING AND CURRENT ROW)"
        " FROM (VALUES ('[1,[2,\"]\"]]'), ('{\"a\":\"x,y\",\"b\":[{}]}'), ('3'), ('\"[\"'))",
        "SELECT json_group_object(k, json(v)) OVER (ROWS BETWEEN 1 PRECEDING AND CURRENT ROW)"
        " FROM (VALUES ('a', '{\"x\":[1,2]}'), ('b', '\"}\"'), ('c', '[]'))",
        "SELECT * FROM json_each('[1]', '$.a[')", "SELECT * FROM json_each('{\"a\":1}', 'x')",
        "SELECT * FROM json_tree('{\"a\":{\"b\\\\u0041\":1,\"c d\":[2]}}')",
        "SELECT key, fullpath, path FROM json_tree('{\"a\\\"b\":{\"\":1}, \"$\":[{\"x.y\":2}]}')",
        "SELECT json_valid(1), json_valid(1.5), json_valid(x'00'), json_type(1.5), json_type(x'')",
        "SELECT json_extract('[1,{\"a\":2}]', '$[0]', '$[1].a', '$[9]')",
        "SELECT json_array_length('[1,2]', NULL), json_array_length('{}', '$'), json_type('[]', NULL)",
        "SELECT json_pretty('[1,{\"a\":[]}]', ''), json_pretty('{}', NULL), json_pretty(NULL)",
    ]:
        pair.run(sql)


def test_json_subtype_lost_in_a_sorter(pair):
    # Values that pass through SQLite's sorter or the temporary table of
    # UNION / INTERSECT / EXCEPT are read back from records, without the
    # JSON subtype; an ORDER BY an index gives (no sorter) keeps it.  Seen
    # through a STORED json_quote() of the inserted value (fuzz seed 2030).
    pair.run("CREATE TABLE s(a, j AS (json_array(a)))")
    pair.run("CREATE INDEX sa ON s(a)")
    pair.run("INSERT INTO s(a) VALUES (1), (2)")
    pair.run("CREATE TABLE t(c, g AS (json_quote(c)) STORED)")
    for query in ["SELECT j FROM s", "SELECT j FROM s ORDER BY 1", "SELECT j FROM s ORDER BY rowid", "SELECT j FROM s ORDER BY a",
                  "SELECT j FROM s ORDER BY a, j", "SELECT j FROM s WHERE j > 1 ORDER BY a", "SELECT DISTINCT j FROM s ORDER BY a",
                  "SELECT j FROM s UNION SELECT j FROM s", "SELECT j FROM s UNION ALL SELECT j FROM s",
                  "SELECT j FROM s UNION ALL SELECT j FROM s ORDER BY 1", "SELECT j FROM s INTERSECT SELECT j FROM s",
                  "SELECT j FROM (SELECT j FROM s ORDER BY 1)", "WITH c AS (SELECT j FROM s) SELECT j FROM c ORDER BY 1",
                  "SELECT jsonb_array(a) FROM s ORDER BY 1", "SELECT j FROM s, s AS s2 ORDER BY s.a"]:
        pair.run("DELETE FROM t")
        pair.run(f"INSERT INTO t(c) {query}")
        pair.run("SELECT rowid, g FROM t")
    pair.run("SELECT json_array((SELECT j FROM s ORDER BY 1 LIMIT 1)), json_array((SELECT j FROM s ORDER BY a LIMIT 1))")
