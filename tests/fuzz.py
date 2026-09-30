"""Random SQL generator for differential fuzzing against sqlite3.

Run a campaign from the command line:

    .venv/bin/python tests/fuzz.py --seeds 0-199 --statements 400

Every statement is executed on both MiniDB and sqlite3; any difference in
results (or in whether an error happens) is reported with the seed and the
statements that led to it.

The generator stays inside the supported language and avoids the places
where the result depends on choices SQLite leaves to its query plan:

* Row order without a total ORDER
  BY, bare columns in aggregates, GROUP_CONCAT order, and which row an
  UPDATE touching several rows processes first when that decides a UNIQUE
  conflict (UPDATEs of UNIQUE columns or the row id touch a single row).
  Numbers are compared by value (``Pair(loose_numbers=True)``), because which
  of two equal values 1 and 1.0 DISTINCT, GROUP BY or MIN/MAX returns also
  depends on the order rows are visited in.
"""

import argparse
import os
import random
import sys

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

FUNCTIONS = [
    "abs", "length", "lower", "upper", "coalesce", "ifnull", "nullif", "typeof", "min", "max",
    "substr", "replace", "trim", "ltrim", "rtrim", "instr", "round", "hex", "quote", "unicode",
    "sign", "octet_length", "char", "iif", "concat", "concat_ws", "printf", "glob", "ceil", "floor",
    "trunc", "sqrt", "ln", "exp", "mod", "pow", "atan2", "date", "datetime", "julianday", "strftime",
    "unixepoch",
]
DATE_MODIFIERS = ["'+1 day'", "'-3 months'", "'start of month'", "'weekday 2'", "'+1.5 hours'", "'unixepoch'",
                  "'floor'", "'+1-01-01'", "'subsec'"]
PRINTF_FORMATS = ["'%d'", "'%5.2f'", "'%s|%x'", "'%.3e'", "'%-6s|'", "'%q'", "'%c'", "'%,d'", "'%g'",
                  "'%!.17g'", "'%05.1f'", "'%X-%o'"]
TEXTS = ["", "a", "b", "abc", "B", "ab%", "x_y", "1", "10", "2.5", " 7", "0x1", "-3", "é", "Zz"]


def no_max_rowid(value):
    """An expression for an INTEGER PRIMARY KEY that is never a large number:
    once row id 2**63-1 exists (a row id near it, then a new row, does it),
    SQLite picks later row ids at random.  Text that looks like a number
    counts too (the INTEGER PRIMARY KEY converts it), so the test is on
    CAST(v AS NUMERIC); x < '' holds exactly for numbers (they sort before
    all text).  The value is written several times, so it must not contain
    parameters; CASE and CAST call no function (a function call would change
    SQLite's statement journal decision)."""
    number = f"CAST(({value}) AS NUMERIC)"
    return f"(CASE WHEN {number} > 1000000 AND {number} < '' THEN NULL ELSE ({value}) END)"


class Table:
    def __init__(self, name, columns, rowid_alias):
        self.name = name
        self.columns = columns  # [(name, type, constraints)]
        self.rowid_alias = rowid_alias  # column name or None
        self.unique_columns = {c[0] for c in columns if "UNIQUE" in c[2]}
        # Column tuples of uniqueness constraints: valid ON CONFLICT targets.
        self.unique_targets = [(c[0],) for c in columns if "UNIQUE" in c[2] or "PRIMARY KEY" in c[2]]
        self.derived = False  # a subquery in FROM (no rowid)

    def column_names(self):
        return [c[0] for c in self.columns]


class Generator:
    def __init__(self, seed):
        self.rng = random.Random(seed)
        self.tables = []
        self.index_count = 0
        self.indexes = []
        self.index_info = {}  # name -> (table, columns, unique)
        self.parameters = []  # values for the "?" placeholders of the current statement
        self.views = []  # Tables describing the views (columns x and y)
        self.no_parameters = False  # views may not contain parameters

    # ---- schema -------------------------------------------------------------

    def create_table(self):
        rng = self.rng
        name = f"t{len(self.tables)}"
        columns = []
        rowid_alias = None
        if rng.random() < 0.6:
            rowid_alias = "id"
            columns.append(("id", "INTEGER", "PRIMARY KEY"))
        for i in range(rng.randint(2, 4)):
            # Every affinity: INTEGER, TEXT, REAL, NUMERIC and BLOB (also no type at all).
            col_type = rng.choice(["INTEGER", "TEXT", "INTEGER", "TEXT", "REAL", "NUMERIC", "",
                                   "BLOB", "VARCHAR(5)", "INT", "FLOAT"])
            constraint = rng.choice(["", "", "", "NOT NULL", "UNIQUE"])
            if rng.random() < 0.3:
                constraint = (constraint + " DEFAULT " + rng.choice(
                    ["0", "'x'", "-1.5", "NULL", "(2 * 3)", "x'61'", "'10'"])).strip()
            columns.append((f"c{i}", col_type, constraint))
        table = Table(name, columns, rowid_alias)
        self.tables.append(table)
        definitions = ", ".join(" ".join(p for p in c if p) for c in columns)
        return f"CREATE TABLE {name} ({definitions})"

    def create_index(self):
        rng = self.rng
        table = rng.choice(self.tables)
        columns = rng.sample(table.column_names(), rng.randint(1, min(2, len(table.columns))))
        self.index_count += 1
        name = f"i{self.index_count}"
        self.indexes.append(name)
        unique = "UNIQUE " if rng.random() < 0.2 else ""
        if unique:
            table.unique_columns.update(columns)
            table.unique_targets.append(tuple(columns))
        self.index_info[name] = (table, tuple(columns), bool(unique))
        return f"CREATE {unique}INDEX {name} ON {table.name} ({', '.join(columns)})"

    def add_column(self):
        """ALTER TABLE ADD COLUMN (old rows read the constant default)."""
        rng = self.rng
        table = rng.choice([t for t in self.tables if not t.derived])
        name = f"c{len(table.columns)}"
        col_type = rng.choice(["INTEGER", "TEXT", "REAL", ""])
        default = rng.choice(["", " DEFAULT 5", " DEFAULT 'y'", " DEFAULT -2.5", " NOT NULL DEFAULT 1"])
        table.columns.append((name, col_type, default.strip()))
        return f"ALTER TABLE {table.name} ADD COLUMN {name} {col_type}{default}"

    def drop_index(self):
        if not self.indexes:
            return self.create_index()
        name = self.rng.choice(self.indexes)
        self.indexes.remove(name)
        table, columns, unique = self.index_info.pop(name)
        if unique:
            table.unique_targets.remove(columns)
        return f"DROP INDEX {name}"

    # ---- values and expressions -------------------------------------------------

    def literal(self, text_safe=False):
        text = self._literal(text_safe)
        if self.rng.random() < 0.15 and not self.no_parameters:
            # Bind the same value through a parameter instead.
            if text.startswith("x'"):
                self.parameters.append(bytes.fromhex(text[2:-1]))
            else:
                self.parameters.append(eval(text.replace("NULL", "None")))  # noqa: S307 - our own literals
            return "?"
        return text

    def _literal(self, text_safe=False):
        rng = self.rng
        kind = rng.random()
        if kind < 0.12:
            return "NULL"
        if kind < 0.55:
            return str(rng.choice([0, 1, 2, 3, 5, 7, 10, -1, -4, rng.randint(-20, 40)]))
        if kind < 0.61:
            return repr(rng.randint(-40, 40) / 4)
        if kind < 0.65:
            return repr(rng.choice([rng.uniform(-100, 100), rng.random() / 3, 1e20, 1.5e-7, 0.1,
                                    round(rng.uniform(-10, 10), rng.randint(1, 4))]))
        if kind < 0.67:
            # -2**63 is left out: abs() of it raises an error, and SQLite's
            # order of evaluating constant expressions decides whether it does.
            return rng.choice(["9223372036854775807", "4611686018427387904", "0x7f", "-0x10"])
        if kind < 0.71:
            return rng.choice(["x''", "x'61'", "x'3130'", "x'00'", "x'ff'", "x'4142'"])
        return "'" + rng.choice(TEXTS).replace("'", "''") + "'"

    def column(self, scope):
        alias, table = self.rng.choice(scope)
        rowid = ["rowid"] if table.rowid_alias is None and not table.derived else []
        name = self.rng.choice(table.column_names() + rowid)
        return f"{alias}.{name}" if len(scope) > 1 else name

    def subquery(self, scope, depth, text_safe):
        """A scalar, IN or EXISTS subquery, correlated with ``scope`` half the time.

        Scalar subqueries always aggregate, so which row comes "first" never
        matters."""
        rng = self.rng
        table = rng.choice([t for t in self.tables if not t.derived])
        inner = [("s", table)]
        condition = self.condition(inner, depth + 1)
        if scope and rng.random() < 0.5:
            alias, outer_table = rng.choice(scope)
            outer = f"{alias}.{rng.choice(outer_table.column_names())}"
            condition = f"({condition}) AND s.{rng.choice(table.column_names())} = {outer}"
        where = f" WHERE {condition}" if rng.random() < 0.8 else ""
        kind = rng.random()
        if kind < 0.45:
            # Not sum(): an integer overflow in a subquery surfaces only if
            # SQLite's plan evaluates it (a known, plan-dependent difference).
            function = rng.choice(["count", "max", "min", "total", "avg"])
            argument = self.expr(inner, depth + 2, text_safe)
            return f"(SELECT {function}({argument}) FROM {table.name} AS s{where})"
        if kind < 0.8:
            value = self.expr(scope, depth + 1, text_safe)
            column = self.column(inner)
            return f"({value} {rng.choice(['', 'NOT '])}IN (SELECT {column} FROM {table.name} AS s{where}))"
        return f"({rng.choice(['', 'NOT '])}EXISTS (SELECT 1 FROM {table.name} AS s{where}))"

    def expr(self, scope, depth=0, text_safe=False):
        """A random expression over the tables in ``scope`` [(alias, Table)]."""
        rng = self.rng
        if depth >= 3 or rng.random() < 0.3:
            return self.column(scope) if scope and rng.random() < 0.6 else self.literal(text_safe)
        kind = rng.random()
        sub = lambda safe=text_safe: self.expr(scope, depth + 1, safe)  # noqa: E731
        if kind < 0.35:
            op = rng.choice(["+", "-", "*", "/", "%", "=", "!=", "<", "<=", ">", ">=", "AND", "OR", "IS",
                             "IS NOT", "&", "|", "<<", ">>"])
            return f"({sub()} {op} {sub()})"
        if kind < 0.45:
            return f"({sub(True)} || {sub(True)})"
        if kind < 0.55:
            return f"{rng.choice(['-', '+', 'NOT ', '~'])}({sub()})"
        if kind < 0.62:
            negated = rng.choice(["", "NOT "])
            return f"({sub()} {negated}BETWEEN {sub()} AND {sub()})"
        if kind < 0.70:
            items = ", ".join(sub() for _ in range(rng.randint(1, 3)))
            return f"({sub()} {rng.choice(['', 'NOT '])}IN ({items}))"
        if kind < 0.76:
            if rng.random() < 0.3:
                pattern = rng.choice(["'a*'", "'*b*'", "'?'", "'*'", "'[0-9]*'", "'[^a]*'", "'*.5'"])
                return f"({sub(True)} {rng.choice(['', 'NOT '])}GLOB {pattern})"
            pattern = rng.choice(["'a%'", "'%b%'", "'_'", "'%'", "'1%'", "'A_C'", "'%.5'"])
            return f"({sub(True)} {rng.choice(['', 'NOT '])}LIKE {pattern})"
        if kind < 0.80:
            return f"({sub()} IS {rng.choice(['', 'NOT '])}NULL)"
        if kind < 0.84:
            if rng.random() < 0.5:
                whens = " ".join(f"WHEN {sub()} THEN {sub()}" for _ in range(rng.randint(1, 3)))
                return f"CASE {sub()} {whens} ELSE {sub()} END"
            whens = " ".join(f"WHEN {sub()} THEN {sub()}" for _ in range(rng.randint(1, 3)))
            return f"CASE {whens}{' ELSE ' + sub() if rng.random() < 0.5 else ''} END"
        if kind < 0.87:
            return f"CAST({sub()} AS {rng.choice(['INTEGER', 'TEXT', 'REAL', 'NUMERIC', 'VARCHAR(5)'])})"
        if kind < 0.90 and depth < 2 and self.tables:
            return self.subquery(scope, depth, text_safe)
        function = rng.choice(FUNCTIONS)
        small = lambda: str(rng.randint(-4, 6))  # noqa: E731
        if function == "abs":
            # abs(-2**63) raises an error; when SQLite evaluates it depends on
            # its plan (a WHERE term "x = constant" puts the constant into the
            # other terms, which then run once, before any row is read).
            args = [f"nullif({sub()}, -9223372036854775807 - 1)"]
        elif function in ("typeof", "hex", "quote", "unicode", "sign", "octet_length", "ceil",
                          "floor", "trunc", "sqrt", "ln", "exp", "length", "lower", "upper"):
            args = [sub()]
        elif function in ("ifnull", "nullif", "instr", "glob", "mod", "pow", "atan2"):
            args = [sub(), sub()]
        elif function in ("trim", "ltrim", "rtrim", "round"):
            args = [sub()] + ([sub() if function != "round" else small()] if rng.random() < 0.5 else [])
        elif function == "substr":
            args = [sub(), small()] + ([small()] if rng.random() < 0.6 else [])
        elif function in ("replace", "iif"):
            args = [sub(), sub(), sub()]
        elif function == "char":
            args = [str(rng.choice([65, 97, 233, 0x10FFFF, 0, 48])) for _ in range(rng.randint(1, 3))]
        elif function in ("date", "datetime", "julianday", "unixepoch"):
            args = [rng.choice([sub(), "'2024-01-31 12:00'", str(rng.randint(0, 2000000000)), "2460000.5"])]
            args += [rng.choice(DATE_MODIFIERS) for _ in range(rng.randint(0, 2))]
        elif function == "strftime":
            args = [rng.choice(["'%Y-%m-%d'", "'%j %W %V'", "'%s %f'", "'%H:%M %p'"]),
                    rng.choice([sub(), "'2024-02-29 23:59:59.5'"])]
        elif function == "printf":
            args = [rng.choice(PRINTF_FORMATS)] + [sub() for _ in range(rng.randint(1, 2))]
        else:  # coalesce, min, max, concat, concat_ws
            args = [sub() for _ in range(rng.randint(2, 3))]
        return f"{function}({', '.join(args)})"

    def condition(self, scope, depth=1):
        rng = self.rng
        parts = [self.expr(scope, depth) for _ in range(rng.randint(1, 2))]
        # Simple comparisons with constants make the planner use rowids and indexes.
        for _ in range(rng.randint(0, 2)):
            op = rng.choice(["=", "=", "<", ">", "<=", ">="])
            parts.append(f"{self.column(scope)} {op} {self.literal()}")
        return " AND ".join(parts) if rng.random() < 0.7 else " OR ".join(parts)

    # ---- statements -------------------------------------------------------------

    def conflict(self):
        """An optional ``OR <resolution>`` for INSERT or UPDATE."""
        if self.rng.random() < 0.7:
            return ""
        return "OR " + self.rng.choice(["IGNORE", "REPLACE", "FAIL", "ABORT", "ROLLBACK"]) + " "

    def returning(self, table):
        """An optional RETURNING clause (no subqueries: nothing to depend on
        the order rows are changed in)."""
        if self.rng.random() < 0.85:
            return ""
        scope = [(table.name, table)]
        items = ["*"] if self.rng.random() < 0.2 else [
            self.expr(scope, 2) for _ in range(self.rng.randint(1, 2))
        ]
        return " RETURNING " + ", ".join(items)

    def upsert(self, table):
        """An optional ON CONFLICT clause (only with a valid target, or none)."""
        rng = self.rng
        if rng.random() < 0.8:
            return ""
        target = ""
        if table.unique_targets and rng.random() < 0.8:
            target = "(" + ", ".join(rng.choice(table.unique_targets)) + ") "
        if rng.random() < 0.4:
            return f" ON CONFLICT {target}DO NOTHING"
        excluded = Table("excluded", table.columns, None)
        excluded.derived = True  # no excluded.rowid
        scope = [(table.name, table), ("excluded", excluded)]
        names = [c for c in table.column_names() if c != table.rowid_alias]
        assignments = [f"{c} = {self.expr(scope, 2, True)}"
                       for c in rng.sample(names, rng.randint(1, min(2, len(names))))]
        where = f" WHERE {self.expr(scope, 2)}" if rng.random() < 0.3 else ""
        return f" ON CONFLICT {target}DO UPDATE SET {', '.join(assignments)}{where}"

    def insert(self):
        rng = self.rng
        table = rng.choice(self.tables)
        if rng.random() < 0.15:
            return self.insert_select(table)
        rows = []
        verb = "REPLACE " if rng.random() < 0.05 else f"INSERT {self.conflict()}"
        if rng.random() < 0.5:
            columns = rng.sample(table.column_names(), rng.randint(1, len(table.columns)))
            if table.rowid_alias not in columns and rng.random() < 0.15:
                columns.insert(rng.randint(0, len(columns)), "rowid")  # the row id by name
            prefix = f"{verb}INTO {table.name} ({', '.join(columns)}) VALUES "
        else:
            columns = table.column_names()
            prefix = f"{verb}INTO {table.name} VALUES "
        for _ in range(rng.randint(1, 4)):
            values = []
            for column in columns:
                if column in (table.rowid_alias, "rowid"):
                    self.no_parameters = True
                    value = no_max_rowid(self.expr([], 2, True))
                    self.no_parameters = False
                else:
                    value = self.expr([], 2, True)
                values.append(value)
            rows.append("(" + ", ".join(values) + ")")
        return prefix + ", ".join(rows) + self.upsert(table) + self.returning(table)

    def insert_select(self, table):
        """INSERT ... SELECT.  New rows get row ids in the order the SELECT
        returns them, so the order is made total with ORDER BY, ending with
        the source row id: equal values such as 0 and 0.0 tie in ORDER BY,
        yet store differently (as '0' and '0.0' in a TEXT column)."""
        rng = self.rng
        source = rng.choice([t for t in self.tables if not t.derived])
        scope = [("s", source)]
        columns = rng.sample(table.column_names(), rng.randint(1, len(table.columns)))
        items = []
        for column in columns:
            if column == table.rowid_alias:
                self.no_parameters = True
                items.append(no_max_rowid(self.expr(scope, 2, True)))
                self.no_parameters = False
            else:
                items.append(self.expr(scope, 2, True))
        where = f" WHERE {self.condition(scope)}" if rng.random() < 0.6 else ""
        order = ", ".join([str(i + 1) for i in range(len(items))] + ["s.rowid"])
        return (f"INSERT {self.conflict()}INTO {table.name} ({', '.join(columns)}) SELECT {', '.join(items)} "
                f"FROM {source.name} AS s{where} ORDER BY {order} LIMIT {rng.randint(0, 6)}")

    def update(self):
        rng = self.rng
        table = rng.choice(self.tables)
        scope = [(table.name, table)]
        names = [c for c in table.column_names() if c != table.rowid_alias]
        assignments = [f"{c} = {self.expr(scope, 1, True)}" for c in rng.sample(names, rng.randint(1, len(names)))]
        where = self.condition(scope)
        if any(a.split(" = ")[0] in table.unique_columns for a in assignments) or any(
            "(SELECT" in a for a in assignments
        ):
            # Which row goes first could decide a UNIQUE conflict, or what a
            # subquery over the table sees: one row only.
            where = f"rowid = {rng.randint(1, 40)}"
        if table.rowid_alias and rng.random() < 0.15:
            # Changing the row id: keep it to one row so the processing order cannot matter.
            assignments = [f"id = {rng.randint(-5, 60)}"]
            where = f"id = {rng.randint(-5, 60)}"
        conflict = self.conflict()
        if conflict:
            # IGNORE / REPLACE / FAIL outcomes depend on which row goes first.
            where = f"rowid = {rng.randint(1, 40)}"
        return f"UPDATE {conflict}{table.name} SET {', '.join(assignments)} WHERE {where}{self.returning(table)}"

    def delete(self):
        table = self.rng.choice(self.tables)
        where = self.condition([(table.name, table)])
        return f"DELETE FROM {table.name} WHERE {where}{self.returning(table)}"

    def derived_table(self):
        """A subquery in FROM with columns x and y, and a Table describing it."""
        rng = self.rng
        base = rng.choice(self.tables)
        inner = [("q", base)]
        where = f" WHERE {self.condition(inner)}" if rng.random() < 0.6 else ""
        sql = (f"(SELECT {self.expr(inner, 1, True)} AS x, {self.expr(inner, 1, True)} AS y "
               f"FROM {base.name} AS q{where})")
        table = Table("derived", [("x", "", ""), ("y", "", "")], None)
        table.derived = True
        return sql, table

    def create_view(self):
        """A view over one table, with columns x and y (like derived_table)."""
        self.no_parameters = True
        try:
            sql, table = self.derived_table()
        finally:
            self.no_parameters = False
        table.name = f"v{len(self.views) + 1}"
        self.views.append(table)
        return f"CREATE VIEW {table.name} AS {sql[1:-1]}"

    def drop_view(self):
        if not self.views:
            return self.create_view()
        view = self.rng.choice(self.views)
        self.views.remove(view)
        return f"DROP VIEW {view.name}"

    def compound_select(self):
        rng = self.rng
        width = rng.randint(1, 2)
        parts = []
        for _ in range(rng.randint(2, 3)):
            table = rng.choice(self.tables)
            scope = [("a", table)]
            items = ", ".join(self.expr(scope, 1) for _ in range(width))
            where = f" WHERE {self.condition(scope)}" if rng.random() < 0.6 else ""
            parts.append(f"SELECT {items} FROM {table.name} AS a{where}")
        sql = parts[0]
        for part in parts[1:]:
            sql += f" {rng.choice(['UNION', 'UNION ALL', 'INTERSECT', 'EXCEPT'])} {part}"
        if rng.random() < 0.5:
            sql += " ORDER BY " + ", ".join(
                f"{i + 1} {rng.choice(['ASC', 'DESC'])}" for i in range(width)
            )
            if rng.random() < 0.5:
                sql += f" LIMIT {rng.randint(0, 4)} OFFSET {rng.randint(0, 2)}"
        return sql

    def cte_select(self):
        """A query over a CTE: a plain one over a table, or a recursive counter."""
        rng = self.rng
        if rng.random() < 0.5:
            table = rng.choice([t for t in self.tables if not t.derived])
            scope = [("q", table)]
            where = f" WHERE {self.condition(scope)}" if rng.random() < 0.6 else ""
            body = (f"SELECT {self.expr(scope, 1, True)} AS x, {self.expr(scope, 1, True)} AS y "
                    f"FROM {table.name} AS q{where}")
        else:
            op = rng.choice(["UNION ALL", "UNION"])
            limit = f" LIMIT {rng.randint(0, 8)}" if rng.random() < 0.3 else ""
            order = f" ORDER BY {rng.choice(['1', '2', '1 DESC', '2 DESC'])}" if rng.random() < 0.3 else ""
            body = (f"SELECT {self.literal()} AS x, 0 AS y {op} SELECT (x {rng.choice(['+', '*', '||'])} "
                    f"{rng.randint(1, 3)}) % 50, y + 1 FROM c WHERE y < {rng.randint(0, 6)}{order}{limit}")
        cte = Table("c", [("x", "", ""), ("y", "", "")], None)
        cte.derived = True
        scope = [("a", cte)]
        where = f" WHERE {self.condition(scope)}" if rng.random() < 0.5 else ""
        items = ", ".join(self.expr(scope, 1) for _ in range(rng.randint(1, 2)))
        return f"WITH c AS ({body}) SELECT {items} FROM c AS a{where}"

    def select(self):
        rng = self.rng
        if rng.random() < 0.05:
            return self.cte_select()
        if rng.random() < 0.08:
            return self.compound_select()
        if rng.random() < 0.1 and self.views:
            view = rng.choice(self.views)
            scope = [("a", view)]
            from_sql = f"{view.name} AS a"
        elif rng.random() < 0.12:
            derived_sql, derived = self.derived_table()
            scope = [("a", derived)]
            from_sql = f"{derived_sql} AS a"
        else:
            scope = [("a", rng.choice(self.tables))]
            from_sql = f"{scope[0][1].name} AS a"
        if rng.random() < 0.3 and len(self.tables) > 1:
            from_sql += self.join(scope, "b")
            if rng.random() < 0.25:
                from_sql += self.join(scope, "c")
        where = f" WHERE {self.condition(scope)}" if rng.random() < 0.7 else ""
        if rng.random() < 0.3:
            return self.aggregate_select(scope, from_sql, where)
        items = [self.expr(scope, 1) for _ in range(rng.randint(1, 3))]
        if len(scope) == 1 and not scope[0][1].derived and rng.random() < 0.15:
            items.insert(rng.randint(0, len(items)), self.window_item(scope))
        distinct = "DISTINCT " if rng.random() < 0.1 else ""
        if rng.random() < 0.1:
            # Result column aliases, which WHERE (and ORDER BY) may use.
            items = [f"{item} AS k{i}" for i, item in enumerate(items)]
            test = f"k{rng.randrange(len(items))} {rng.choice(['=', '<', '>=', 'IS NOT'])} {self.literal()}"
            where = f" WHERE ({where[7:]}) {rng.choice(['AND', 'OR'])} {test}" if where else f" WHERE {test}"
        sql = f"SELECT {distinct}{', '.join(items)} FROM {from_sql}{where}"
        if rng.random() < 0.4:
            terms = [f"{self.expr(scope, 2)} {rng.choice(['ASC', 'DESC'])}" for _ in range(rng.randint(1, 2))]
            if distinct:
                terms = [str(rng.randint(1, len(items)))]
            # All result columns as tie-breakers: the order is then total.
            terms += [str(i + 1) for i in range(len(items))]
            sql += " ORDER BY " + ", ".join(terms)
            if rng.random() < 0.5:
                sql += f" LIMIT {rng.randint(0, 5)} OFFSET {rng.randint(0, 3)}"
        return sql

    def window_item(self, scope):
        """A window function over the rows of one table.  Its ORDER BY ends
        with the row id, so that ties do not depend on the order the rows
        are read in (RANGE with an offset orders by the row id alone)."""
        rng = self.rng
        function = rng.choice([
            "sum({e})", "total({e})", "avg({e})", "count(*)", "count({e})", "min({e})", "max({e})",
            "group_concat({e})", "group_concat({e}, '-')", "row_number()", "rank()", "dense_rank()",
            "percent_rank()", "cume_dist()", "ntile(3)", "first_value({e})", "last_value({e})",
            "nth_value({e}, 2)", "lead({e})", "lag({e}, 2, 0)", "sum({e}) FILTER (WHERE {e})",
        ]).replace("{e}", self.expr(scope, 2))
        parts = []
        if rng.random() < 0.4:
            parts.append(f"PARTITION BY {self.expr(scope, 2)}")
        unit = rng.choice(["", "ROWS", "RANGE", "GROUPS"])
        bounds = ["UNBOUNDED PRECEDING", "1 PRECEDING", "CURRENT ROW", "2 FOLLOWING", "UNBOUNDED FOLLOWING"]
        i = rng.randrange(len(bounds) - 1)
        start, end = bounds[i], bounds[rng.randrange(max(i, 1), len(bounds))]
        if unit == "RANGE" and ("1 " in start or "2 " in end):
            parts.append(f"ORDER BY rowid{rng.choice(['', ' DESC'])}")
        elif rng.random() < 0.85:
            parts.append(f"ORDER BY {self.expr(scope, 2)}{rng.choice(['', ' DESC'])}, rowid")
        else:
            parts.append("ORDER BY rowid")
        if unit:
            frame = f"{unit} BETWEEN {start} AND {end}"
            if rng.random() < 0.2:
                frame += " EXCLUDE " + rng.choice(["CURRENT ROW", "GROUP", "TIES", "NO OTHERS"])
            parts.append(frame)
        return f"{function} OVER ({' '.join(parts)})"

    def join(self, scope, alias):
        """A join of one more table (as ``alias``) to the tables in ``scope``."""
        rng = self.rng
        other = rng.choice(self.tables)
        kind = rng.choice(["", "LEFT ", "RIGHT ", "FULL "])
        join = rng.choice(["JOIN", "JOIN", ",", "USING", "NATURAL"])
        if join == "USING" and scope[0][1].derived:
            join = "JOIN"
        scope.append((alias, other))
        if join == "USING":
            return f" {kind}JOIN {other.name} AS {alias} USING (c0)"
        if join == "NATURAL":
            return f" NATURAL {kind}JOIN {other.name} AS {alias}"
        if join == ",":
            return f", {other.name} AS {alias}"
        if rng.random() < 0.7:
            left, right = self.column(scope[:-1]), self.column(scope[-1:])
            on = f"{alias}.{right} = {left}".replace(f"{alias}.{alias}.", f"{alias}.")
        else:
            on = self.expr(scope, 1)
        return f" {kind}JOIN {other.name} AS {alias} ON {on}"

    def aggregate_select(self, scope, from_sql, where):
        rng = self.rng
        groups = [self.column(scope) for _ in range(rng.randint(0, 2))]
        aggregates = []
        for _ in range(rng.randint(1, 3)):
            function = rng.choice(["count", "sum", "avg", "min", "max", "total", "count"])
            argument = "*" if function == "count" and rng.random() < 0.3 else self.expr(scope, 2)
            distinct = "DISTINCT " if argument != "*" and rng.random() < 0.15 else ""
            if rng.random() < 0.12:
                # An aggregate of this query inside a subquery (its argument
                # uses only this query's columns).
                alias, table = rng.choice(scope)
                argument = f"{alias}.{rng.choice(table.column_names())}"
                other = rng.choice([t for t in self.tables if not t.derived])
                condition = f" WHERE {self.condition([('s', other)], 2)}" if rng.random() < 0.6 else ""
                aggregates.append(f"(SELECT {function}({distinct}{argument}) FROM {other.name} AS s{condition})")
                continue
            aggregates.append(f"{function}({distinct}{argument})")
        sql = f"SELECT {', '.join(groups + aggregates)} FROM {from_sql}{where}"
        if groups:
            sql += f" GROUP BY {', '.join(groups)}"
            if rng.random() < 0.3:
                sql += f" HAVING {rng.choice(aggregates)} > {self.literal()}"
        return sql

    def statement_with_parameters(self):
        """A random statement and the values for its placeholders (or None)."""
        self.parameters = []
        sql = self.statement()
        return sql, (list(self.parameters) if "?" in sql else None)

    def statement(self):
        rng = self.rng
        roll = rng.random()
        if roll < 0.02 and len(self.tables) < 3:
            return self.create_table()
        if roll < 0.05:
            return self.create_index()
        if roll < 0.06:
            return self.drop_index()
        if roll < 0.08:
            return rng.choice(["BEGIN", "COMMIT", "ROLLBACK"])
        if roll < 0.085:
            return "ANALYZE"  # statistics change later plans, never results
        if roll < 0.095:
            return self.create_view() if rng.random() < 0.7 else self.drop_view()
        if roll < 0.098:
            return self.add_column()
        if roll < 0.1:
            return "VACUUM"
        if roll < 0.40:
            return self.insert()
        if roll < 0.50:
            return self.update()
        if roll < 0.56:
            return self.delete()
        return self.select()


def run_seed(seed, statements, path=None, verbose=False):
    """Run one fuzzing session; returns None or a failure description."""
    from sqlcompare import Pair  # only here: the generator itself (metamorphic.py) needs no sqlite3

    generator = Generator(seed)
    pair = Pair(path, loose_numbers=True)
    snapshots = SnapshotReader(pair.mini, path, seed) if path is not None else None
    history = []
    try:
        setup = [(generator.create_table(), None) for _ in range(2)]
        setup.append((generator.create_index(), None))
        for sql, parameters in setup + [generator.statement_with_parameters() for _ in range(statements)]:
            history.append(sql if parameters is None else f"{sql}  -- parameters: {parameters!r}")
            if verbose:
                print(history[-1])
            pair.run(sql, parameters=parameters)
            if snapshots is not None:
                snapshots.step(history)
        if snapshots is not None:
            snapshots.finish()
        problems = pair.mini.integrity_check()
        if problems:
            raise AssertionError(f"integrity check failed: {problems}")
    except AssertionError as exc:
        return f"seed {seed}, statement {len(history)}:\n{exc}\n--- history ---\n" + ";\n".join(history)
    finally:
        if snapshots is not None:
            snapshots.reader.close()
        pair.close()
    return None


class SnapshotReader:
    """A second connection to a database file that holds read snapshots
    while the main connection keeps writing; the main connection checkpoints
    often, so partial checkpoints and log restarts happen under the reader.
    Its snapshot must never change."""

    def __init__(self, main, path, seed):
        from minidb.database import Database

        self.main = main
        main.pager.checkpoint_frames = 8
        main.pager.restart_wait = 0  # the reader is in this thread: waiting cannot help
        self.reader = Database(path)
        self.rng = random.Random(seed * 7919 + 1)
        self.expected = None  # the reader's snapshot, while it holds one

    def dump(self, db, tables):
        return {name: db.execute(f"SELECT * FROM {name} ORDER BY rowid") for name in tables}

    def step(self, history):
        rng, main, reader = self.rng, self.main, self.reader
        if not main.in_transaction and rng.random() < 0.05:
            main.pager.checkpoint()
        if self.expected is None:
            if not main.in_transaction and rng.random() < 0.05:
                reader.execute("BEGIN")
                tables = sorted(reader.catalog.tables)
                self.expected = self.dump(reader, tables)
                if self.expected != self.dump(main, tables):
                    raise AssertionError("a new snapshot differs from the committed state")
                history.append("-- reader: BEGIN")
        elif rng.random() < 0.08:
            self.finish()
            history.append("-- reader: COMMIT")

    def finish(self):
        if self.expected is not None:
            if self.dump(self.reader, sorted(self.expected)) != self.expected:
                raise AssertionError("the reader's snapshot changed")
            self.reader.execute("COMMIT")
            self.expected = None


def parse_range(text):
    if "-" in text:
        start, end = text.split("-")
        return range(int(start), int(end) + 1)
    return [int(text)]


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--seeds", default="0-99")
    parser.add_argument("--statements", type=int, default=400)
    parser.add_argument("--file", action="store_true", help="use database files instead of memory")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    import tempfile

    failures = 0
    seeds = parse_range(args.seeds)
    with tempfile.TemporaryDirectory() as directory:
        for seed in seeds:
            path = os.path.join(directory, f"fuzz{seed}.db") if args.file else None
            failure = run_seed(seed, args.statements, path, args.verbose)
            if failure:
                failures += 1
                print(failure[:4000])
                print("=" * 70)
    print(f"{len(seeds)} seeds x {args.statements} statements: {failures} failing seeds")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
