"""SQL syntax analysis: turns tokens into a syntax tree.

Operator precedence, lowest first (as in SQLite):

    OR
    AND
    NOT
    =  ==  !=  <>  IS [NOT]  [NOT] IN  [NOT] LIKE  [NOT] BETWEEN
    <  <=  >  >=
    +  -
    *  /  %
    ||
    unary -  +
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Union

from minidb.errors import NotSupportedError, OperationalError
from minidb.tokenizer import SQLSyntaxError, Token, tokenize
from minidb.values import ascii_lower, ascii_upper

# ---- expressions -------------------------------------------------------


@dataclass(frozen=True)
class Literal:
    value: object


@dataclass(frozen=True)
class Column:
    name: str
    table: str | None = None
    # Where the name and the table qualifier are in the SQL text (for ALTER
    # TABLE's rewriting of views); not part of the node's value.
    pos: int = field(default=-1, compare=False)
    table_pos: int = field(default=-1, compare=False)


@dataclass(frozen=True)
class Parameter:
    """A placeholder: ``?``, ``?NNN``, ``:name``, ``@name`` or ``$name``.

    ``index`` is 1-based and numbered like SQLite: ``?NNN`` is NNN, a bare
    ``?`` is one more than the largest index so far, and a name gets the next
    index the first time it appears."""

    index: int
    name: str | None = None


@dataclass(frozen=True)
class Star:
    """``*`` or ``table.*`` in a select list, or the argument of ``COUNT(*)``."""

    table: str | None = None


@dataclass(frozen=True)
class Unary:
    op: str  # "-", "+", "~" or "NOT"
    operand: object


@dataclass(frozen=True)
class Binary:
    op: str  # OR AND = != < <= > >= IS "IS NOT" + - * / % || & | << >>
    left: object
    right: object


@dataclass(frozen=True)
class Between:
    expr: object
    low: object
    high: object
    negated: bool = False


@dataclass(frozen=True)
class InList:
    expr: object
    items: tuple
    negated: bool = False


@dataclass(frozen=True)
class Like:
    expr: object
    pattern: object
    negated: bool = False
    escape: object = None  # LIKE ... ESCAPE <expr>
    op: str = "LIKE"  # or "GLOB"


@dataclass(frozen=True)
class Collate:
    """``expr COLLATE name``: the value of expr, compared with that collation."""

    expr: object
    collation: str  # as written


@dataclass(frozen=True)
class Raise:
    """``RAISE(IGNORE)`` or ``RAISE(ROLLBACK | ABORT | FAIL, message)``, for trigger programs."""

    kind: str  # IGNORE, ROLLBACK, ABORT or FAIL
    message: object = None  # an expression (None for IGNORE)


@dataclass(frozen=True)
class Case:
    """``CASE [base] WHEN a THEN b ... [ELSE c] END``."""

    base: object  # None for a searched CASE
    whens: tuple  # (condition or value, result) pairs
    else_: object = None


@dataclass(frozen=True)
class Cast:
    expr: object
    type_name: str  # as written, e.g. "INTEGER" or "VARCHAR(10)"


@dataclass(frozen=True)
class Subquery:
    """A scalar subquery ``(SELECT ...)``: the first column of the first row."""

    query: object  # Select or Compound


@dataclass(frozen=True)
class InSelect:
    expr: object
    query: object
    negated: bool = False


@dataclass(frozen=True)
class Exists:
    query: object


@dataclass(frozen=True)
class Frame:
    """``{ROWS | RANGE | GROUPS} BETWEEN <start> AND <end> [EXCLUDE ...]``.
    A bound is UNBOUNDED, PRECEDING, CURRENT or FOLLOWING (with an offset
    expression for PRECEDING and FOLLOWING)."""

    unit: str
    start: str
    start_offset: object = None
    end: str = "CURRENT"
    end_offset: object = None
    exclude: str | None = None  # NO OTHERS, CURRENT ROW, GROUP or TIES


@dataclass(frozen=True)
class WindowDef:
    """``OVER (...)`` or ``WINDOW name AS (...)``: an optional base window
    name, PARTITION BY expressions, ORDER BY (expr, descending, nulls_first)
    triples and a frame (None: the default one)."""

    base: str | None = None
    partition: tuple = ()
    order_by: tuple = ()
    frame: Frame | None = None


@dataclass(frozen=True)
class Call:
    name: str  # upper case
    args: tuple
    distinct: bool = False
    defer_affinity: bool = False  # has its first argument's affinity (SQLite's AFF_DEFER)
    filter: object = None  # FILTER (WHERE <expr>)
    over: object = None  # OVER (<WindowDef>) or OVER <window name>


# ---- statements --------------------------------------------------------


@dataclass
class IndexedColumn:
    """A column of a PRIMARY KEY, UNIQUE or index: ``name [COLLATE c] [ASC|DESC]``."""

    name: str
    collation: str | None = None
    descending: bool = False
    pos: int = field(default=-1, compare=False)  # of the name in the SQL text


@dataclass
class KeyConstraint:
    """PRIMARY KEY or UNIQUE, of a column or of the table."""

    primary: bool
    columns: list  # IndexedColumns
    conflict: str | None = None  # ON CONFLICT <resolution>
    autoincrement: bool = False
    name: str | None = None  # CONSTRAINT <name>
    column_level: bool = False


@dataclass
class CheckConstraint:
    expr: object
    text: str  # the expression as written: the error message names it (or the constraint)
    name: str | None = None


@dataclass
class ForeignKey:
    """``[FOREIGN KEY (columns)] REFERENCES parent [(columns)] [actions]``."""

    columns: list  # the child columns' names
    parent: str
    parent_columns: list  # empty: the parent's primary key
    on_delete: str = "NO ACTION"  # SET NULL, SET DEFAULT, CASCADE, RESTRICT or NO ACTION
    on_update: str = "NO ACTION"
    deferred: bool = False  # DEFERRABLE INITIALLY DEFERRED
    name: str | None = None
    match: str = "NONE"
    # Where the names are in the SQL text (for ALTER TABLE's rewriting).
    column_pos: list = field(default_factory=list, compare=False)
    parent_pos: int = field(default=-1, compare=False)
    parent_column_pos: list = field(default_factory=list, compare=False)


@dataclass
class ColumnDef:
    name: str
    type: str  # the declared type in upper case, e.g. "INTEGER", "VARCHAR(30)", "" for none
    primary_key: bool = False
    not_null: bool = False
    unique: bool = False
    default: object = None  # DEFAULT expression, or None
    default_text: str | None = None  # its SQL text
    collation: str | None = None  # COLLATE <name>
    not_null_conflict: str | None = None  # NOT NULL ON CONFLICT <resolution>
    constraints: list = field(default_factory=list)  # its KeyConstraints, CheckConstraints and ForeignKeys
    declared: str = field(default="", compare=False)  # the type as written
    pos: int = field(default=-1, compare=False)  # of the name in the SQL text
    end: int = field(default=-1, compare=False)  # where the definition ends in the SQL text


@dataclass
class AlterTable:
    table: str
    action: str  # "rename", "rename column", "add" or "drop"
    column: str | None = None  # the column renamed or dropped
    new_name: str | None = None
    definition: object = None  # ColumnDef of ADD COLUMN
    definition_text: str = ""  # its SQL text
    new_quoted: bool = False  # whether the new name was written quoted


@dataclass
class CreateTable:
    name: str
    columns: list
    if_not_exists: bool = False
    constraints: list = field(default_factory=list)  # table constraints, in order
    sql: str = field(default="", compare=False)  # what the schema stores, as SQLite does
    name_pos: int = field(default=-1, compare=False)
    columns_end: int = field(default=-1, compare=False)  # where ADD COLUMN inserts (see Catalog)


@dataclass
class CreateIndex:
    name: str
    table: str
    columns: list  # column names
    unique: bool = False
    if_not_exists: bool = False
    descending: list = field(default_factory=list)  # per column: DESC?
    collations: list = field(default_factory=list)  # per column: COLLATE name or None
    sql: str = field(default="", compare=False)  # what the schema stores, as SQLite does
    table_pos: int = field(default=-1, compare=False)
    column_pos: list = field(default_factory=list, compare=False)


@dataclass
class CreateView:
    name: str
    columns: list | None  # the optional column names: CREATE VIEW v(a, b) AS ...
    query: object  # Select or Compound
    if_not_exists: bool = False
    sql: str = ""  # the statement's text, stored in the schema as SQLite does
    temp: bool = False  # CREATE TEMP VIEW: kept in memory by this connection only


@dataclass
class DropView:
    name: str
    if_exists: bool = False


@dataclass
class DropIndex:
    name: str
    if_exists: bool = False


@dataclass
class DropTable:
    name: str
    if_exists: bool = False


@dataclass
class Upsert:
    """``ON CONFLICT [(columns)] DO NOTHING | DO UPDATE SET ... [WHERE ...]``."""

    columns: list | None  # the conflict target; None matches any uniqueness constraint
    assignments: list | None = None  # (column name, expression) pairs; None for DO NOTHING
    where: object = None
    target_where: object = None  # would name a partial index (MiniDB has none)
    collations: list | None = field(default=None, compare=False)  # the target's COLLATE names (None: none given)


@dataclass
class Insert:
    table: str
    columns: list | None
    rows: list  # list of lists of expressions (VALUES)
    query: object = None  # or a Select / Compound (INSERT ... SELECT)
    conflict: str | None = None  # INSERT OR <conflict>: ABORT, FAIL, IGNORE, REPLACE or ROLLBACK
    upsert: list = field(default_factory=list)  # Upsert clauses, in order
    returning: list | None = None  # SelectItems of RETURNING
    ctes: list | None = None  # WITH ...
    # Where the table name and the column names are in the SQL text (for ALTER TABLE in triggers).
    table_pos: int = field(default=-1, compare=False)
    column_pos: list | None = field(default=None, compare=False)


@dataclass
class SelectItem:
    expr: object
    alias: str | None = None
    text: str = field(default="", compare=False)  # source text, for the column name


@dataclass
class TableRef:
    name: str
    alias: str | None = None
    indexed_by: str | None = None  # INDEXED BY <index>: the only index the planner may use
    pos: int = field(default=-1, compare=False)  # of the name in the SQL text
    not_indexed: bool = False  # NOT INDEXED: the planner uses no index


@dataclass
class DerivedTable:
    """A subquery in FROM: ``(SELECT ...) [AS] alias``."""

    query: object
    alias: str | None = None


@dataclass
class Join:
    """One table of a FROM clause and how it joins to the tables before it."""

    table: object  # TableRef or DerivedTable
    kind: str = "INNER"  # INNER (also for "," and CROSS JOIN), LEFT, RIGHT or FULL
    on: object = None
    using: list | None = None  # column names of USING (...)
    natural: bool = False


@dataclass
class OrderItem:
    expr: object
    descending: bool = False
    nulls_first: bool | None = None  # None: NULLs first for ASC, last for DESC


@dataclass
class Select:
    items: list
    source: list = field(default_factory=list)  # Join items; empty without FROM
    where: object = None
    distinct: bool = False
    group_by: list = field(default_factory=list)
    having: object = None
    order_by: list = field(default_factory=list)
    limit: object = None
    offset: object = None
    ctes: list | None = None  # WITH ...
    windows: list = field(default_factory=list)  # WINDOW name AS (...): (name, WindowDef) pairs


@dataclass
class Analyze:
    """``ANALYZE [table or index]``: gather statistics for the planner."""

    name: str | None = None


@dataclass
class Begin:
    mode: str = "DEFERRED"  # DEFERRED, IMMEDIATE or EXCLUSIVE


@dataclass
class Commit:
    pass


@dataclass
class Rollback:
    pass


@dataclass
class Explain:
    """``EXPLAIN [QUERY PLAN] stmt``: describe how a statement would read its tables."""

    statement: object


@dataclass
class Compound:
    """``select UNION [ALL] | INTERSECT | EXCEPT select ...`` evaluated left to
    right; ORDER BY and LIMIT apply to the whole result."""

    selects: list  # Select or Values
    operators: list  # "UNION", "UNION ALL", "INTERSECT" or "EXCEPT", one per join
    order_by: list = field(default_factory=list)
    limit: object = None
    offset: object = None
    ctes: list | None = None  # WITH ...


@dataclass
class Values:
    """``VALUES (...), (...)`` as a query: columns column1, column2, ..."""

    rows: list  # lists of expressions
    ctes: list | None = None


@dataclass
class Cte:
    """``name [(columns)] AS (query)`` of a WITH clause."""

    name: str
    columns: list | None
    query: object


@dataclass
class Update:
    table: str
    assignments: list  # (column name, expression) pairs
    where: object = None
    conflict: str | None = None
    returning: list | None = None
    indexed_by: str | None = None
    ctes: list | None = None
    not_indexed: bool = False
    table_pos: int = field(default=-1, compare=False)
    assignment_pos: list | None = field(default=None, compare=False)  # of the names SET assigns


@dataclass
class Delete:
    table: str
    where: object = None
    returning: list | None = None
    indexed_by: str | None = None
    ctes: list | None = None
    not_indexed: bool = False
    table_pos: int = field(default=-1, compare=False)


@dataclass
class Reindex:
    name: str | None = None  # an index, a table or a collation; None: every index


@dataclass
class Pragma:
    """``PRAGMA [schema.]name [= value | (value)]``; ``value`` is the text of
    a name or number (a number keeps its sign), a string's value, or None."""

    name: str  # lower case
    value: object = None
    schema: str | None = None


@dataclass
class TableFunction:
    """A table-valued function in FROM, such as ``pragma_table_info('t')``."""

    name: str  # lower case
    args: list
    alias: str | None = None
    pos: int = field(default=-1, compare=False)


@dataclass
class CreateTrigger:
    """``CREATE TRIGGER name [BEFORE | AFTER | INSTEAD OF] event ON table
    [FOR EACH ROW] [WHEN expr] BEGIN statement; ... END``."""

    name: str
    table: str
    timing: str  # BEFORE, AFTER or INSTEAD OF
    event: str  # INSERT, UPDATE or DELETE
    columns: list | None  # UPDATE OF columns
    when: object
    body: list  # INSERT / UPDATE / DELETE / SELECT statements
    if_not_exists: bool = False
    sql: str = ""  # as SQLite stores it: "CREATE TRIGGER " and the text from the name to END
    table_pos: int = field(default=-1, compare=False)  # of the table name in ``sql``
    column_pos: list | None = field(default=None, compare=False)  # of the UPDATE OF columns, in ``sql``


@dataclass
class DropTrigger:
    name: str
    if_exists: bool = False


@dataclass
class Vacuum:
    """``VACUUM [schema] [INTO <file name>]``."""

    schema: str = "main"
    into: object = None  # the expression naming the file to write a compacted copy to


# Any expression node, and any statement.
Expr = Union[
    Literal, Parameter, Column, Star, Unary, Binary, Between, InList, Like, Case, Cast,
    Subquery, InSelect, Exists, Call, Collate,
]
Statement = Union[
    CreateTable, CreateIndex, CreateView, DropTable, DropIndex, DropView, Reindex, Vacuum, Values, AlterTable, Insert, Select, Compound, Update, Delete,
    Begin, Commit, Rollback, Analyze, Explain, Pragma, CreateTrigger, DropTrigger,
]


# ---- parser ------------------------------------------------------------

# Words that start a column constraint and so end a type name.
CONSTRAINT_WORDS = {"CONSTRAINT", "CHECK", "DEFAULT", "REFERENCES", "COLLATE", "GENERATED"}
MAX_PARAMETER_INDEX = 32_766  # SQLITE_MAX_VARIABLE_NUMBER's default


def parse(text: str) -> Statement:
    """Parse a single SQL statement (a trailing ``;`` is optional)."""
    statements = parse_script(text)
    if len(statements) != 1:
        raise SQLSyntaxError(
            "expected exactly one statement" if statements else "empty statement", text, 0
        )
    return statements[0]


def parse_script(text: str) -> list[Statement]:
    """Parse zero or more statements separated by ``;``."""
    return Parser(text).parse_script()


_LIST_ENDS = frozenset((",", ")", ";"))
_CURRENT_WORDS = frozenset(("CURRENT_DATE", "CURRENT_TIME", "CURRENT_TIMESTAMP"))


class Parser:
    def __init__(self, text: str) -> None:
        self.text = text
        self.tokens = tokenize(text)
        self.i = 0
        self.seen_with = False
        self.in_trigger = False  # parsing a trigger program (its statements have restrictions)
        self.tok = self.tokens[0]  # the current token (only advance() moves on)

    # ---- token helpers ------------------------------------------------

    def advance(self) -> Token:
        token = self.tok
        if token.kind != "EOF":
            self.i += 1
            self.tok = self.tokens[self.i]
        return token

    def error(self, expected: str, token: Token | None = None) -> SQLSyntaxError:
        token = token or self.tok
        where = "at end of input" if token.kind == "EOF" else f'near "{token.text}"'
        return SQLSyntaxError(f"syntax error {where}: expected {expected}", self.text, token.pos)

    def at_keyword(self, *words: str) -> bool:
        tok = self.tok
        return tok.kind == "KEYWORD" and tok.value in words

    def at_op(self, *ops: str) -> bool:
        tok = self.tok
        return tok.kind == "OP" and tok.value in ops

    def accept_keyword(self, word: str) -> Token | None:
        tok = self.tok
        if tok.kind == "KEYWORD" and tok.value == word:
            return self.advance()
        return None

    def accept_op(self, op: str) -> Token | None:
        tok = self.tok
        if tok.kind == "OP" and tok.value == op:
            return self.advance()
        return None

    def expect_keyword(self, word: str) -> Token:
        if not self.at_keyword(word):
            raise self.error(word)
        return self.advance()

    def expect_op(self, op: str) -> Token:
        if not self.at_op(op):
            raise self.error(f'"{op}"')
        return self.advance()

    def at_word(self, *words: str) -> bool:
        """At a non-reserved word such as VIEW (tokenized as an identifier)?"""
        return self.tok.kind == "IDENT" and ascii_upper(self.tok.text) in words

    def expect_word(self, word: str) -> Token:
        """Expect a non-reserved word such as KEY (tokenized as an identifier)."""
        if self.tok.kind == "IDENT" and ascii_upper(self.tok.text) == word:
            return self.advance()
        raise self.error(word)

    def identifier(self, what: str = "identifier") -> str:
        if self.tok.kind != "IDENT":
            raise self.error(what)
        return self.advance().value

    # ---- statements ---------------------------------------------------

    def parse_script(self) -> list[Statement]:
        """Parse all statements.  Each gets ``param_count`` (the largest
        parameter index it uses) and ``param_names`` ({index: name})."""
        statements = []
        while True:
            while self.accept_op(";"):
                pass
            if self.tok.kind == "EOF":
                return statements
            self.param_count = 0
            self.param_names = {}
            self.seen_with = False  # see value_rows
            stmt = self.statement()
            stmt.param_count = self.param_count
            stmt.param_names = self.param_names
            statements.append(stmt)
            if self.tok.kind != "EOF" and not self.at_op(";"):
                raise self.error('";" or end of statement')

    def statement(self) -> Statement:
        if self.accept_keyword("BEGIN"):
            mode = "DEFERRED"
            if self.tok.kind == "IDENT" and ascii_upper(self.tok.text) in ("DEFERRED", "IMMEDIATE", "EXCLUSIVE"):
                mode = ascii_upper(self.advance().text)
            self.accept_keyword("TRANSACTION")
            return Begin(mode)
        if self.accept_keyword("COMMIT") or self.accept_keyword("END"):
            self.accept_keyword("TRANSACTION")
            return Commit()
        if self.accept_keyword("ROLLBACK"):
            self.accept_keyword("TRANSACTION")
            return Rollback()
        if self.accept_keyword("ANALYZE"):
            return Analyze(self.identifier("table name") if self.tok.kind == "IDENT" else None)
        if self.accept_keyword("EXPLAIN"):
            if self.tok.kind == "IDENT" and ascii_upper(self.tok.text) == "QUERY":
                self.advance()
                self.expect_word("PLAN")
            if not (self.at_query() or self.at_keyword("UPDATE", "DELETE")):
                raise self.error("SELECT, UPDATE or DELETE")
            return Explain(self.statement())
        if self.at_word("WITH") and self._starts_query(self.i):
            ctes = self.with_clause()
            if self.at_keyword("INSERT") or self.at_word("REPLACE"):
                stmt = self.insert()
            elif self.at_keyword("UPDATE"):
                stmt = self.update()
            elif self.at_keyword("DELETE"):
                stmt = self.delete()
            else:
                stmt = self.query(with_allowed=False)
            stmt.ctes = ctes
            return stmt
        if self.at_query():
            return self.query()
        if self.at_keyword("INSERT") or self.at_word("REPLACE"):
            return self.insert()
        if self.at_keyword("UPDATE"):
            return self.update()
        if self.at_keyword("DELETE"):
            return self.delete()
        if self.at_keyword("CREATE"):
            return self.create()
        if self.at_keyword("DROP"):
            return self.drop()
        if self.at_word("ALTER"):
            return self.alter_table()
        if self.at_word("REINDEX"):
            self.advance()
            if self.tok.kind != "IDENT":
                return Reindex()
            name = self.advance().value
            if self.accept_op("."):  # schema.name: only the main schema exists
                name = self.identifier("index or table name")
            return Reindex(name)
        if self.at_word("PRAGMA"):
            return self.pragma()
        if self.at_word("VACUUM"):
            self.advance()
            stmt = Vacuum()
            if self.tok.kind == "IDENT":
                schema = ascii_lower(self.advance().value)
                if schema not in ("main", "temp"):
                    raise OperationalError(f"unknown database {schema}")
                stmt.schema = schema
            if self.accept_keyword("INTO"):
                stmt.into = self.expr()
            return stmt
        raise self.error("a statement")

    def pragma(self) -> Pragma:
        self.advance()  # PRAGMA
        schema = None
        name = self.identifier("pragma name")
        if self.accept_op("."):
            schema, name = name, self.identifier("pragma name")
        stmt = Pragma(ascii_lower(name), schema=schema)
        if self.accept_op("="):
            stmt.value = self.pragma_value()
        elif self.accept_op("("):
            stmt.value = self.pragma_value()
            self.expect_op(")")
        return stmt

    def pragma_value(self) -> str:
        """A signed number, a name (keywords such as ON too) or a string."""
        sign = ""
        if self.at_op("+", "-"):
            sign = self.advance().value
        token = self.tok
        if token.kind in ("INTEGER", "FLOAT"):
            self.advance()
            return ("-" if sign == "-" else "") + token.text
        if sign:
            raise self.error("number")
        if token.kind in ("IDENT", "KEYWORD", "STRING"):
            self.advance()
            return token.value if token.kind != "KEYWORD" else token.text
        raise self.error("pragma value")

    def create(self) -> CreateTable | CreateIndex | CreateView:
        create = self.expect_keyword("CREATE")
        if self.at_keyword("UNIQUE", "INDEX"):
            return self.create_index()
        temp = self.at_word("TEMP") or self.at_word("TEMPORARY")
        if temp:
            self.advance()
        if self.at_word("VIEW"):
            return self.create_view(create.pos, temp)
        if self.at_word("TRIGGER"):
            if temp:
                raise NotSupportedError("temporary triggers are not supported")
            return self.create_trigger()
        if temp:
            raise NotSupportedError("temporary tables are not supported")
        self.expect_keyword("TABLE")
        if_not_exists = self.if_not_exists()
        name_pos = self.tok.pos
        name = self.identifier("table name")
        self.expect_op("(")
        columns = [self.column_def()]
        constraints = []
        while self.accept_op(","):
            if self.at_table_constraint():
                columns_end = self.tokens[self.i - 1].pos
                constraints.append(self.table_constraint())
                while self.accept_op(",") or self.at_table_constraint():
                    constraints.append(self.table_constraint())
                break
            columns.append(self.column_def())
        else:
            columns_end = self.tok.pos
        self.expect_op(")")
        options = []
        while self.at_word("WITHOUT", "STRICT"):
            if self.advance().value.upper() == "WITHOUT":
                self.expect_word("ROWID")
                options.append("WITHOUT ROWID")
            else:
                options.append("STRICT")
            if not self.accept_op(","):
                break
        if options:
            raise NotSupportedError(f"{options[0]} tables are not supported")
        stmt = CreateTable(name, columns, if_not_exists, constraints)
        stmt.sql = "CREATE TABLE " + self.text[name_pos:self.end_of_previous()]
        stmt.name_pos, stmt.columns_end = name_pos, columns_end
        return stmt

    def end_of_previous(self) -> int:
        """Where the token before the current one ends in the SQL text."""
        last = self.tokens[self.i - 1]
        return last.pos + len(last.text)

    def at_table_constraint(self) -> bool:
        return self.at_keyword("PRIMARY", "UNIQUE") or self.at_word("CONSTRAINT", "CHECK", "FOREIGN")

    def table_constraint(self) -> KeyConstraint | CheckConstraint | ForeignKey:
        name = None
        if self.at_word("CONSTRAINT"):
            self.advance()
            name = self.identifier("constraint name")
            if not self.at_table_constraint():
                raise self.error("PRIMARY KEY, UNIQUE, CHECK or FOREIGN KEY")
        if self.accept_keyword("PRIMARY") or self.at_keyword("UNIQUE"):
            primary = self.tokens[self.i - 1].value == "PRIMARY" and not self.at_keyword("UNIQUE")
            if primary:
                self.expect_word("KEY")
            else:
                self.advance()
            self.expect_op("(")
            columns = [self.key_column()]
            while self.accept_op(","):
                columns.append(self.key_column())
            autoincrement = primary and self.accept_word("AUTOINCREMENT")
            self.expect_op(")")
            return KeyConstraint(primary, columns, self.on_conflict(), autoincrement, name)
        if self.at_word("CHECK"):
            check = self.check_constraint(name)
            self.on_conflict()  # (allowed, and ignored, as in SQLite)
            return check
        self.expect_word("FOREIGN")
        self.expect_word("KEY")
        self.expect_op("(")
        columns, positions = [], []
        while True:
            positions.append(self.tok.pos)
            columns.append(self.identifier("column name"))
            if not self.accept_op(","):
                break
        self.expect_op(")")
        self.expect_word("REFERENCES")
        key = self.references(name)
        key.columns, key.column_pos = columns, positions
        if self.at_keyword("NOT") or self.at_word("DEFERRABLE"):
            key.deferred = self.deferrable()
        return key

    def key_column(self) -> IndexedColumn:
        """A column of a table's PRIMARY KEY or UNIQUE: only names, as in SQLite."""
        token = self.tok
        expr = self.expr()
        collation = None
        if isinstance(expr, Collate):
            expr, collation = expr.expr, expr.collation
        if not isinstance(expr, Column) or expr.table is not None:
            raise OperationalError("expressions prohibited in PRIMARY KEY and UNIQUE constraints")
        descending = not self.accept_keyword("ASC") and bool(self.accept_keyword("DESC"))
        return IndexedColumn(expr.name, collation, descending, token.pos)

    def accept_word(self, word: str) -> bool:
        if self.at_word(word):
            self.advance()
            return True
        return False

    def on_conflict(self) -> str | None:
        """``ON CONFLICT <resolution>`` of a constraint, or None."""
        if not self.at_keyword("ON"):
            return None
        self.advance()
        self.expect_word("CONFLICT")
        if self.accept_keyword("ROLLBACK"):
            return "ROLLBACK"
        for word in ("ABORT", "FAIL", "IGNORE", "REPLACE"):
            if self.accept_word(word):
                return word
        raise self.error("ROLLBACK, ABORT, FAIL, IGNORE or REPLACE")

    def check_constraint(self, name: str | None) -> CheckConstraint:
        self.expect_word("CHECK")
        self.expect_op("(")
        start = self.tok.pos
        expr = self.expr()
        text = self.text[start:self.end_of_previous()]
        self.expect_op(")")
        return CheckConstraint(expr, text, name)

    def references(self, name: str | None) -> ForeignKey:
        """After REFERENCES: ``parent [(columns)]`` and the actions."""
        parent_pos = self.tok.pos
        key = ForeignKey([], self.identifier("table name"), [], name=name, parent_pos=parent_pos)
        if self.accept_op("("):
            while True:
                key.parent_column_pos.append(self.tok.pos)
                key.parent_columns.append(self.identifier("column name"))
                if self.at_word("COLLATE"):  # (allowed, and ignored)
                    self.advance()
                    self.identifier("collation name")
                if not self.accept_keyword("ASC"):
                    self.accept_keyword("DESC")
                if not self.accept_op(","):
                    break
            self.expect_op(")")
        while True:
            if self.at_word("MATCH"):
                self.advance()
                key.match = ascii_upper(self.identifier("match type"))
            elif self.at_keyword("ON") and self.tokens[self.i + 1].kind in ("KEYWORD", "IDENT") \
                    and ascii_upper(self.tokens[self.i + 1].text) in ("DELETE", "UPDATE", "INSERT"):
                self.advance()
                event = ascii_upper(self.advance().text)
                action = self.foreign_key_action()
                if event == "DELETE":
                    key.on_delete = action
                elif event == "UPDATE":
                    key.on_update = action
            else:
                return key

    def foreign_key_action(self) -> str:
        if self.accept_keyword("SET"):
            if self.accept_keyword("NULL"):
                return "SET NULL"
            self.expect_word("DEFAULT")
            return "SET DEFAULT"
        if self.accept_word("CASCADE"):
            return "CASCADE"
        if self.accept_word("RESTRICT"):
            return "RESTRICT"
        self.expect_word("NO")
        self.expect_word("ACTION")
        return "NO ACTION"

    def deferrable(self) -> bool:
        """``[NOT] DEFERRABLE [INITIALLY DEFERRED | IMMEDIATE]``: deferred?"""
        negated = bool(self.accept_keyword("NOT"))
        self.expect_word("DEFERRABLE")
        deferred = False
        if self.accept_word("INITIALLY"):
            if self.accept_word("DEFERRED"):
                deferred = True
            else:
                self.expect_word("IMMEDIATE")
        return deferred and not negated

    def if_not_exists(self) -> bool:
        if self.accept_keyword("IF"):
            self.expect_keyword("NOT")
            self.expect_keyword("EXISTS")
            return True
        return False

    def alter_table(self) -> AlterTable:
        self.advance()  # ALTER
        self.expect_keyword("TABLE")
        table = self.identifier("table name")
        if self.at_word("RENAME"):
            self.advance()
            if self.at_word("TO"):
                self.advance()
                return AlterTable(table, "rename", new_name=self.identifier("table name"))
            if self.at_word("COLUMN"):
                self.advance()
            column = self.identifier("column name")
            self.expect_word("TO")
            quoted = self.tok.text[:1] in ('"', "[", "`", "'")
            return AlterTable(table, "rename column", column, self.identifier("column name"), new_quoted=quoted)
        if self.at_word("ADD"):
            self.advance()
            if self.at_word("COLUMN"):
                self.advance()
            definition = self.column_def()
            return AlterTable(table, "add", definition=definition,
                              definition_text=self.text[definition.pos:definition.end])
        if self.accept_keyword("DROP"):
            if self.at_word("COLUMN"):
                self.advance()
            return AlterTable(table, "drop", self.identifier("column name"))
        raise self.error("RENAME, ADD or DROP")

    def create_view(self, start: int, temp: bool = False) -> CreateView:
        self.advance()  # VIEW
        if_not_exists = self.if_not_exists()
        name = self.identifier("view name")
        columns = None
        if self.accept_op("("):
            columns = [self.identifier("column name")]
            while self.accept_op(","):
                columns.append(self.identifier("column name"))
            self.expect_op(")")
        self.expect_keyword("AS")
        parameters = self.param_count
        query = self.query()
        if self.param_count != parameters:
            raise OperationalError("parameters are not allowed in views")
        last = self.tokens[self.i - 1]
        sql = self.text[start:last.pos + len(last.text)]
        return CreateView(name, columns, query, if_not_exists, sql, temp)

    def create_trigger(self) -> CreateTrigger:
        self.advance()  # TRIGGER
        if_not_exists = self.if_not_exists()
        name_pos = self.tok.pos
        name = self.identifier("trigger name")
        if self.accept_op("."):
            self.check_schema(name)
            name_pos = self.tok.pos
            name = self.identifier("trigger name")
        timing = "BEFORE"  # (SQLite's default)
        column_pos = None
        if self.at_word("BEFORE", "AFTER"):
            timing = ascii_upper(self.advance().text)
        elif self.at_word("INSTEAD"):
            self.advance()
            self.expect_word("OF")
            timing = "INSTEAD OF"
        columns = None
        if self.accept_keyword("DELETE"):
            event = "DELETE"
        elif self.accept_keyword("INSERT"):
            event = "INSERT"
        elif self.accept_keyword("UPDATE"):
            event = "UPDATE"
            if self.at_word("OF"):
                self.advance()
                column_pos = [self.tok.pos]
                columns = [self.identifier("column name")]
                while self.accept_op(","):
                    column_pos.append(self.tok.pos)
                    columns.append(self.identifier("column name"))
        else:
            raise self.error("DELETE, INSERT or UPDATE")
        self.expect_keyword("ON")
        table_pos = self.tok.pos
        table = self.identifier("table name")
        if self.accept_op("."):
            self.check_schema(table)
            table_pos = self.tok.pos
            table = self.identifier("table name")
        if self.at_word("FOR"):
            self.advance()
            self.expect_word("EACH")
            self.expect_word("ROW")
        parameters = self.param_count
        when = self.expr() if self.accept_keyword("WHEN") else None
        self.expect_keyword("BEGIN")
        body = []
        self.in_trigger = True
        try:
            while True:
                body.append(self.trigger_statement())
                self.expect_op(";")
                if self.at_keyword("END"):
                    break
        finally:
            self.in_trigger = False
        end = self.advance()
        if self.param_count != parameters:
            raise OperationalError("trigger cannot use variables")
        # (SQLite keeps the text from the trigger's name on, without IF NOT EXISTS or a schema.)
        sql = "CREATE TRIGGER " + self.text[name_pos:end.pos + len(end.text)]
        shift = len("CREATE TRIGGER ") - name_pos
        return CreateTrigger(name, table, timing, event, columns, when, body, if_not_exists, sql, table_pos + shift,
                             None if column_pos is None else [p + shift for p in column_pos])

    def trigger_statement(self) -> Statement:
        """One statement of a trigger program, with SQLite's restrictions."""
        if self.at_keyword("INSERT") or self.at_word("REPLACE"):
            stmt = self.insert()
        elif self.at_keyword("UPDATE"):
            stmt = self.update()
        elif self.at_keyword("DELETE"):
            stmt = self.delete()
        elif self.at_query() or self.at_word("WITH"):
            return self.query()
        else:
            raise self.error("INSERT, UPDATE, DELETE or SELECT")
        if stmt.returning is not None:
            raise OperationalError("cannot use RETURNING in a trigger")
        if isinstance(stmt, (Update, Delete)) and (stmt.indexed_by is not None or stmt.not_indexed):
            clause = "INDEXED BY" if stmt.indexed_by is not None else "NOT INDEXED"
            raise OperationalError(f"the {clause} clause is not allowed on UPDATE or DELETE statements within triggers")
        return stmt

    def check_schema(self, name: str) -> None:
        if ascii_lower(name) not in ("main", "temp"):
            raise OperationalError(f"unknown database {name}")

    def target_table(self) -> str:
        """The table an INSERT, UPDATE or DELETE writes (its position: self.target_pos)."""
        self.target_pos = self.tok.pos
        name = self.identifier("table name")
        if self.in_trigger and self.at_op("."):
            raise OperationalError(
                "qualified table names are not allowed on INSERT, UPDATE, and DELETE statements within triggers")
        return name

    def create_index(self) -> CreateIndex:
        unique = bool(self.accept_keyword("UNIQUE"))
        self.expect_keyword("INDEX")
        if_not_exists = self.if_not_exists()
        name_pos = self.tok.pos
        name = self.identifier("index name")
        self.expect_keyword("ON")
        table_pos = self.tok.pos
        table = self.identifier("table name")
        self.expect_op("(")
        columns = []
        while True:
            token = self.tok
            expr = self.expr()
            collation = None
            if isinstance(expr, Collate):
                expr, collation = expr.expr, expr.collation
            if not isinstance(expr, Column) or expr.table is not None:
                raise NotSupportedError("indexes on expressions are not supported")
            descending = not self.accept_keyword("ASC") and bool(self.accept_keyword("DESC"))
            columns.append(IndexedColumn(expr.name, collation, descending, token.pos))
            if not self.accept_op(","):
                break
        self.expect_op(")")
        if self.accept_keyword("WHERE"):
            raise NotSupportedError("partial indexes are not supported")
        # (DESC only matters in SQLite-format files: MiniDB's own indexes are ascending)
        stmt = CreateIndex(name, table, [c.name for c in columns], unique, if_not_exists,
                           [c.descending for c in columns], [c.collation for c in columns])
        stmt.sql = f"CREATE{' UNIQUE' if unique else ''} INDEX " + self.text[name_pos:self.end_of_previous()]
        stmt.table_pos, stmt.column_pos = table_pos, [c.pos for c in columns]
        return stmt

    def indexed_column(self) -> tuple[str, str | None]:
        """``name [COLLATE c] [ASC | DESC]`` of an upsert target: (name, collation)."""
        name = self.identifier("column name")
        collation = None
        if self.at_word("COLLATE"):
            self.advance()
            collation = self.identifier("collation name")
        if not self.accept_keyword("ASC"):
            self.accept_keyword("DESC")
        return name, collation

    def column_def(self) -> ColumnDef:
        pos = self.tok.pos
        name = self.identifier("column name")
        declared = self.type_name(required=False)
        column = ColumnDef(name, ascii_upper(" ".join(declared.split())), declared=declared, pos=pos)
        name = None  # of the next constraint (CONSTRAINT <name>)
        while True:
            if self.at_word("CONSTRAINT"):
                self.advance()
                name = self.identifier("constraint name")
                continue
            if self.accept_keyword("PRIMARY"):
                self.expect_word("KEY")
                column.primary_key = True
                descending = not self.accept_keyword("ASC") and bool(self.accept_keyword("DESC"))
                conflict = self.on_conflict()
                key = KeyConstraint(True, [IndexedColumn(column.name, None, descending, pos)], conflict,
                                    self.accept_word("AUTOINCREMENT"), name, column_level=True)
                column.constraints.append(key)
            elif self.accept_keyword("NOT"):
                self.expect_keyword("NULL")
                column.not_null = True
                column.not_null_conflict = self.on_conflict()
            elif self.accept_keyword("NULL"):
                self.on_conflict()
            elif self.accept_keyword("UNIQUE"):
                column.unique = True
                column.constraints.append(KeyConstraint(
                    False, [IndexedColumn(column.name, None, False, pos)], self.on_conflict(), name=name,
                    column_level=True))
            elif self.at_word("DEFAULT"):
                self.advance()
                start = self.tok.pos
                column.default = self.default_value(column.name)
                column.default_text = self.text[start:self.end_of_previous()]
            elif self.at_word("CHECK"):
                column.constraints.append(self.check_constraint(name))
            elif self.at_word("COLLATE"):
                self.advance()
                column.collation = self.identifier("collation name")
            elif self.at_word("REFERENCES"):
                self.advance()
                key = self.references(name)
                key.columns, key.column_pos = [column.name], [pos]
                column.constraints.append(key)
            elif self.at_keyword("NOT") and self.tokens[self.i + 1].kind == "IDENT" or self.at_word("DEFERRABLE"):
                key = next((c for c in reversed(column.constraints) if isinstance(c, ForeignKey)), None)
                deferred = self.deferrable()
                if key is not None:
                    key.deferred = deferred
            elif self.at_word("GENERATED") or self.at_keyword("AS"):
                raise NotSupportedError("generated columns are not supported")
            else:
                column.end = self.end_of_previous()
                return column
            name = None

    def default_value(self, column: str) -> Expr:
        """DEFAULT <literal>, <signed number>, <identifier> (as text),
        TRUE/FALSE, CURRENT_TIME/DATE/TIMESTAMP, or (<constant expression>)."""
        token = self.tok
        if self.accept_op("("):
            expr = self.expr()
            self.expect_op(")")
            if any(isinstance(e, (Column, Subquery, InSelect, Exists, Parameter)) for e in walk_expr(expr)):
                raise OperationalError(f"default value of column [{column}] is not constant")
            return expr
        if self.at_op("+", "-"):
            op = self.advance().value
            return Unary(op, self.default_value(column))
        if token.kind in ("INTEGER", "FLOAT", "STRING", "BLOB"):
            self.advance()
            return Literal(token.value)
        if self.accept_keyword("NULL"):
            return Literal(None)
        if token.kind == "IDENT":
            self.advance()
            word = ascii_upper(token.text)
            if word in ("CURRENT_TIME", "CURRENT_DATE", "CURRENT_TIMESTAMP"):
                return Call(word, ())
            if word in ("TRUE", "FALSE"):
                return Literal(int(word == "TRUE"))
            return Literal(token.value)  # a bare identifier is a string
        raise self.error("default value")

    def drop(self) -> DropTable | DropIndex | DropView:
        self.expect_keyword("DROP")
        if self.at_word("TRIGGER"):
            self.advance()
            if_exists = False
            if self.accept_keyword("IF"):
                self.expect_keyword("EXISTS")
                if_exists = True
            name = self.identifier("trigger name")
            if self.accept_op("."):
                self.check_schema(name)
                name = self.identifier("trigger name")
            return DropTrigger(name, if_exists)
        if self.accept_keyword("INDEX"):
            kind = DropIndex
        elif self.at_word("VIEW"):
            self.advance()
            kind = DropView
        else:
            self.expect_keyword("TABLE")
            kind = DropTable
        if_exists = False
        if self.accept_keyword("IF"):
            self.expect_keyword("EXISTS")
            if_exists = True
        what = {DropIndex: "index name", DropView: "view name", DropTable: "table name"}[kind]
        return kind(self.identifier(what), if_exists)

    def conflict_clause(self) -> str | None:
        """``OR <resolution>`` after INSERT or UPDATE (None if absent: each
        constraint's own ON CONFLICT, else ABORT)."""
        if not self.accept_keyword("OR"):
            return None
        if self.accept_keyword("ROLLBACK"):
            return "ROLLBACK"
        for word in ("ABORT", "FAIL", "IGNORE", "REPLACE"):
            if self.at_word(word):
                self.advance()
                return word
        raise self.error("ROLLBACK, ABORT, FAIL, IGNORE or REPLACE")

    def returning(self) -> list[SelectItem] | None:
        if not self.at_word("RETURNING"):
            return None
        self.advance()
        items = [self.select_item()]
        while self.accept_op(","):
            items.append(self.select_item())
        return items

    def upsert_clauses(self) -> list[Upsert]:
        clauses = []
        while self.at_keyword("ON"):
            self.advance()
            self.expect_word("CONFLICT")
            clause = Upsert(None)
            if self.accept_op("("):
                targets = [self.indexed_column()]
                while self.accept_op(","):
                    targets.append(self.indexed_column())
                self.expect_op(")")
                clause.columns = [name for name, _ in targets]
                clause.collations = [collation for _, collation in targets]
                if self.accept_keyword("WHERE"):
                    clause.target_where = self.expr()
            self.expect_word("DO")
            if self.at_word("NOTHING"):
                self.advance()
            else:
                self.expect_keyword("UPDATE")
                self.expect_keyword("SET")
                clause.assignments = [self.assignment()]
                while self.accept_op(","):
                    clause.assignments.append(self.assignment())
                if self.accept_keyword("WHERE"):
                    clause.where = self.expr()
            clauses.append(clause)
            if clause.columns is None:
                break  # only the last clause may omit the target
        return clauses

    def insert(self) -> Insert:
        if self.at_word("REPLACE"):
            self.advance()
            conflict = "REPLACE"
        else:
            self.expect_keyword("INSERT")
            conflict = self.conflict_clause()
        self.expect_keyword("INTO")
        table = self.target_table()
        table_pos = self.target_pos
        columns = None
        column_pos = None
        if self.accept_op("("):
            column_pos = [self.tok.pos]
            columns = [self.identifier("column name")]
            while self.accept_op(","):
                column_pos.append(self.tok.pos)
                columns.append(self.identifier("column name"))
            self.expect_op(")")
        if self.at_word("DEFAULT") and not self.in_trigger:
            self.advance()
            self.expect_keyword("VALUES")
            stmt = Insert(table, [], [[]], None, conflict)
        elif self.at_query() and not self.at_keyword("VALUES"):
            stmt = Insert(table, columns, [], self.query(), conflict)
        else:
            self.expect_keyword("VALUES")
            stmt = Insert(table, columns, self.value_rows(), None, conflict)
        stmt.upsert = self.upsert_clauses()
        stmt.returning = self.returning()
        stmt.table_pos, stmt.column_pos = table_pos, column_pos
        return stmt

    def value_rows(self) -> list[list[Expr]]:
        """The rows after VALUES.

        SQLite (sqlite3MultiValues) codes a row after the first directly,
        without resolving its names, when the statement has had no WITH so
        far, the row is constant, and either the previous row was coded so
        too or it is constant and without affinity (no CAST); otherwise the
        row becomes a resolved SELECT of a UNION ALL.  Unresolved, ``x IS
        TRUE`` is not a truth test but ``x IS 1``: such rows get that."""
        rows = [self.value_row()]
        previous, direct = rows[0], False
        while self.accept_op(","):
            row = self.value_row()
            if not self.seen_with and all(map(is_parse_constant, row)) and (
                direct or (all(map(is_parse_constant, previous))
                           and not any(isinstance(e, Cast) for e in previous))
            ):
                row = [plain_truth_tests(e) for e in row]
                direct = True
            else:
                previous, direct = row, False
            rows.append(row)
        return rows

    def value_row(self) -> list[Expr]:
        self.expect_op("(")
        values = self.expr_list()
        self.expect_op(")")
        return values

    def _starts_query(self, i: int) -> bool:
        """Does a query (SELECT, VALUES or WITH name ...) start at token i?"""
        token = self.tokens[i]
        if token.kind == "KEYWORD":
            return token.value in ("SELECT", "VALUES")
        return (token.kind == "IDENT" and ascii_upper(token.text) == "WITH"
                and self.tokens[i + 1].kind == "IDENT")

    def at_query(self) -> bool:
        return self._starts_query(self.i)

    def with_clause(self) -> list[Cte]:
        """``WITH [RECURSIVE] name [(columns)] AS [[NOT] MATERIALIZED] (query), ...``
        (RECURSIVE is optional: a CTE that names itself is recursive)."""
        self.advance()  # WITH
        self.seen_with = True
        if self.at_word("RECURSIVE"):
            self.advance()
        ctes = []
        while True:
            name = self.identifier("table name")
            columns = None
            if self.accept_op("("):
                columns = [self.identifier("column name")]
                while self.accept_op(","):
                    columns.append(self.identifier("column name"))
                self.expect_op(")")
            self.expect_keyword("AS")
            if self.accept_keyword("NOT"):
                self.expect_word("MATERIALIZED")
            elif self.at_word("MATERIALIZED"):
                self.advance()
            self.expect_op("(")
            ctes.append(Cte(name, columns, self.query()))
            self.expect_op(")")
            if not self.accept_op(","):
                return ctes

    def values_core(self) -> Values:
        self.expect_keyword("VALUES")
        rows = self.value_rows()
        if any(len(row) != len(rows[0]) for row in rows):
            raise OperationalError("all VALUES must have the same number of terms")
        return Values(rows)

    def query(self, with_allowed: bool = True) -> Select | Compound | Values:
        """A SELECT, VALUES or a compound of them, with ORDER BY and LIMIT,
        optionally after WITH."""
        ctes = self.with_clause() if with_allowed and self.at_word("WITH") else None
        stmt = self._compound()
        stmt.ctes = ctes
        return stmt

    def _compound(self) -> Select | Compound | Values:
        selects = [self.values_core() if self.at_keyword("VALUES") else self.select_core()]
        operators = []
        while self.at_keyword("UNION", "INTERSECT", "EXCEPT"):
            operator = self.advance().value
            if operator == "UNION" and self.accept_keyword("ALL"):
                operator = "UNION ALL"
            operators.append(operator)
            selects.append(self.values_core() if self.at_keyword("VALUES") else self.select_core())
        stmt = selects[0] if not operators else Compound(selects, operators)
        if isinstance(selects[-1], Values):
            return stmt  # in SQLite's grammar ORDER BY and LIMIT belong to a last SELECT, not VALUES
        if self.accept_keyword("ORDER"):
            self.expect_keyword("BY")
            stmt.order_by = [self.order_item()]
            while self.accept_op(","):
                stmt.order_by.append(self.order_item())
        if self.accept_keyword("LIMIT"):
            stmt.limit = self.expr()
            if self.accept_keyword("OFFSET"):
                stmt.offset = self.expr()
            elif self.accept_op(","):
                # LIMIT <offset>, <count>
                stmt.offset, stmt.limit = stmt.limit, self.expr()
        return stmt

    def select_core(self) -> Select:
        self.expect_keyword("SELECT")
        distinct = bool(self.accept_keyword("DISTINCT"))
        if not distinct:
            self.accept_keyword("ALL")
        items = [self.select_item()]
        while self.accept_op(","):
            items.append(self.select_item())
        stmt = Select(items, distinct=distinct)
        if self.accept_keyword("FROM"):
            stmt.source = self.from_clause()
        if self.accept_keyword("WHERE"):
            stmt.where = self.expr()
        if self.accept_keyword("GROUP"):
            self.expect_keyword("BY")
            stmt.group_by = self.expr_list()
        if self.accept_keyword("HAVING"):
            stmt.having = self.expr()
        if self.at_word("WINDOW"):
            self.advance()
            while True:
                name = self.identifier("window name")
                self.expect_keyword("AS")
                self.expect_op("(")
                stmt.windows.append((name, self.window_def()))
                self.expect_op(")")
                if not self.accept_op(","):
                    break
        return stmt

    def window_def(self) -> WindowDef:
        """The inside of ``OVER (...)`` or ``WINDOW name AS (...)``."""
        base = None
        if self.tok.kind == "IDENT" and not self.at_word("PARTITION", "RANGE", "ROWS", "GROUPS"):
            base = self.advance().value
        partition = ()
        if self.at_word("PARTITION"):
            self.advance()
            self.expect_keyword("BY")
            partition = tuple(self.expr_list())
        order_by = ()
        if self.accept_keyword("ORDER"):
            self.expect_keyword("BY")
            items = [self.order_item()]
            while self.accept_op(","):
                items.append(self.order_item())
            order_by = tuple((item.expr, item.descending, item.nulls_first) for item in items)
        frame = None
        if self.at_word("RANGE", "ROWS", "GROUPS"):
            unit = ascii_upper(self.advance().text)
            if self.accept_keyword("BETWEEN"):
                start, start_offset = self.frame_bound(True)
                self.expect_keyword("AND")
                end, end_offset = self.frame_bound(False)
            else:
                (start, start_offset), (end, end_offset) = self.frame_bound(True), ("CURRENT", None)
            exclude = None
            if self.at_word("EXCLUDE"):
                self.advance()
                if self.at_word("NO"):
                    self.advance()
                    self.expect_word("OTHERS")
                    exclude = "NO OTHERS"
                elif self.at_word("CURRENT"):
                    self.advance()
                    self.expect_word("ROW")
                    exclude = "CURRENT ROW"
                elif self.accept_keyword("GROUP"):
                    exclude = "GROUP"
                elif self.at_word("TIES"):
                    self.advance()
                    exclude = "TIES"
                else:
                    raise self.error("NO, CURRENT, GROUP or TIES")
            # As SQLite: the start may not come after the end in
            # UNBOUNDED PRECEDING, <n> PRECEDING, CURRENT ROW, <n> FOLLOWING.
            if (start == "CURRENT" and end == "PRECEDING") or (
                    start == "FOLLOWING" and end in ("PRECEDING", "CURRENT")):
                raise OperationalError("unsupported frame specification")
            frame = Frame(unit, start, start_offset, end, end_offset, exclude)
        return WindowDef(base, partition, order_by, frame)

    def frame_bound(self, starting: bool) -> tuple[str, object]:
        """A frame boundary: (UNBOUNDED | PRECEDING | CURRENT | FOLLOWING, offset)."""
        if self.at_word("UNBOUNDED"):
            self.advance()
            self.expect_word("PRECEDING" if starting else "FOLLOWING")
            return "UNBOUNDED", None
        if self.at_word("CURRENT") and self.tokens[self.i + 1].kind == "IDENT" \
                and ascii_upper(self.tokens[self.i + 1].text) == "ROW":
            self.advance()
            self.advance()
            return "CURRENT", None
        offset = self.expr()
        if self.at_word("PRECEDING", "FOLLOWING"):
            return ascii_upper(self.advance().text), offset
        raise self.error("PRECEDING or FOLLOWING")

    def window_suffix(self, call: Call) -> Call:
        """``FILTER (WHERE <expr>)`` and ``OVER (...)`` / ``OVER <name>`` after a call."""
        filter_ = None
        if self.at_word("FILTER") and self.tokens[self.i + 1].text == "(":
            self.advance()
            self.advance()
            self.expect_keyword("WHERE")
            filter_ = self.expr()
            self.expect_op(")")
        over = None
        if self.at_word("OVER") and (self.tokens[self.i + 1].text == "(" or self.tokens[self.i + 1].kind == "IDENT"):
            self.advance()
            if self.accept_op("("):
                over = self.window_def()
                self.expect_op(")")
            else:
                over = self.identifier("window name")
            if self.at_word("FILTER") and self.tokens[self.i + 1].text == "(":
                raise self.error("an operator")  # FILTER goes before OVER
        if filter_ is None and over is None:
            return call
        return dataclasses.replace(call, filter=filter_, over=over)

    def order_item(self) -> OrderItem:
        item = OrderItem(self.expr())
        if self.accept_keyword("DESC"):
            item.descending = True
        else:
            self.accept_keyword("ASC")
        if self.tok.kind == "IDENT" and ascii_upper(self.tok.text) == "NULLS":
            self.advance()
            if self.tok.kind == "IDENT" and ascii_upper(self.tok.text) in ("FIRST", "LAST"):
                item.nulls_first = ascii_upper(self.advance().text) == "FIRST"
            else:
                raise self.error("FIRST or LAST")
        return item

    def select_item(self) -> SelectItem:
        if self.accept_op("*"):
            return SelectItem(Star())
        if (
            self.tok.kind == "IDENT"
            and self.tokens[self.i + 1].value == "."
            and self.tokens[self.i + 2].value == "*"
            and self.tokens[self.i + 2].kind == "OP"
        ):
            table = self.advance().value
            self.advance()
            self.advance()
            return SelectItem(Star(table))
        start = self.tok.pos
        expr = self.expr()
        text = self.text[start:self.tok.pos].strip()
        alias = None
        if self.accept_keyword("AS"):
            alias = self.identifier("alias")
        elif self.tok.kind == "IDENT":
            alias = self.advance().value
        return SelectItem(expr, alias, text)

    def from_clause(self) -> list[Join]:
        first = self.table_or_group()
        joins = first if isinstance(first, list) else [Join(first)]
        while True:
            if self.accept_op(","):
                joins.extend(self.joined(self.table_or_group(), "INNER"))
                continue
            natural = bool(self.accept_keyword("NATURAL"))
            if self.accept_keyword("LEFT"):
                self.accept_keyword("OUTER")
                kind = "LEFT"
            elif self.at_word("RIGHT", "FULL"):
                kind = ascii_upper(self.advance().text)
                self.accept_keyword("OUTER")
            elif self.accept_keyword("INNER") or self.accept_keyword("CROSS"):
                kind = "INNER"
            elif self.at_keyword("JOIN"):
                kind = "INNER"
            elif natural:
                raise self.error("JOIN")
            else:
                return joins
            self.expect_keyword("JOIN")
            source = self.table_or_group()
            if isinstance(source, list):
                if natural or self.at_keyword("ON", "USING"):
                    raise NotSupportedError("a parenthesized join with NATURAL, ON or USING is not supported")
                joins.extend(self.joined(source, kind))
                continue
            join = Join(source, kind, natural=natural)
            if natural:  # NATURAL takes neither ON nor USING
                joins.append(join)
                continue
            if self.accept_keyword("ON"):
                join.on = self.expr()
            elif self.accept_keyword("USING"):
                self.expect_op("(")
                join.using = [self.identifier("column name")]
                while self.accept_op(","):
                    join.using.append(self.identifier("column name"))
                self.expect_op(")")
            joins.append(join)

    def table_or_group(self) -> TableRef | DerivedTable | list[Join]:
        """A table, or a parenthesized join ``(a JOIN b ...)`` as a list of Joins."""
        if self.at_op("(") and not self._starts_query(self.i + 1):
            self.advance()
            group = self.from_clause()
            self.expect_op(")")
            return group[0].table if len(group) == 1 else group
        return self.table_ref()

    @staticmethod
    def joined(source: TableRef | DerivedTable | list[Join], kind: str) -> list[Join]:
        """Joins for ``source`` joined with ``kind`` to the tables before it.

        Joins associate to the left, so a parenthesized join first in FROM is
        the same as without parentheses.  Later, ``x, (a JOIN b ON p)`` is
        ``x, a JOIN b ON p`` as long as the join to the group is an inner one
        without a condition (an inner join's ON is a filter on the result);
        ``x LEFT JOIN (a JOIN b)`` is not, and is refused."""
        if not isinstance(source, list):
            return [Join(source, kind)]
        if kind != "INNER":
            raise NotSupportedError(f"{kind} JOIN of a parenthesized join is not supported")
        return source

    def table_ref(self) -> TableRef | DerivedTable:
        if self.at_op("(") and self._starts_query(self.i + 1):
            self.advance()
            query = self.query()
            self.expect_op(")")
            alias = None
            if self.accept_keyword("AS"):
                alias = self.identifier("alias")
            elif self.tok.kind == "IDENT" and not self.at_word("RIGHT", "FULL", "WINDOW"):
                alias = self.advance().value
            return DerivedTable(query, alias)
        pos = self.tok.pos
        name = self.identifier("table name")
        function = None
        if self.accept_op("("):  # a table-valued function
            function = TableFunction(ascii_lower(name), [] if self.at_op(")") else self.expr_list(), pos=pos)
            self.expect_op(")")
        alias = None
        if self.accept_keyword("AS"):
            alias = self.identifier("alias")
        elif self.tok.kind == "IDENT" and not self.at_word("INDEXED", "RIGHT", "FULL", "WINDOW"):
            alias = self.advance().value  # (RIGHT / FULL start a join, as LEFT does; WINDOW a clause)
        if function is not None:
            function.alias = alias
            return function
        indexed_by, not_indexed = self.index_hint()
        return TableRef(name, alias, indexed_by, pos, not_indexed)

    def index_hint(self) -> tuple[str | None, bool]:
        """(index name, False) for ``INDEXED BY <index>``, (None, True) for
        ``NOT INDEXED``, (None, False) without either."""
        if self.at_word("INDEXED"):
            self.advance()
            self.expect_keyword("BY")
            return self.identifier("index name"), False
        if self.at_keyword("NOT") and self.tokens[self.i + 1].kind == "IDENT" \
                and ascii_upper(self.tokens[self.i + 1].text) == "INDEXED":
            self.advance()
            self.advance()
            return None, True
        return None, False

    def update(self) -> Update:
        self.expect_keyword("UPDATE")
        conflict = self.conflict_clause()
        table = self.target_table()
        table_pos = self.target_pos
        indexed_by, not_indexed = self.index_hint()
        self.expect_keyword("SET")
        assignment_pos = [self.tok.pos]
        assignments = [self.assignment()]
        while self.accept_op(","):
            assignment_pos.append(self.tok.pos)
            assignments.append(self.assignment())
        where = self.expr() if self.accept_keyword("WHERE") else None
        return Update(table, assignments, where, conflict, self.returning(), indexed_by, not_indexed=not_indexed,
                      table_pos=table_pos, assignment_pos=assignment_pos)

    def assignment(self) -> tuple[str, Expr]:
        name = self.identifier("column name")
        self.expect_op("=")
        return name, self.expr()

    def delete(self) -> Delete:
        self.expect_keyword("DELETE")
        self.expect_keyword("FROM")
        table = self.target_table()
        table_pos = self.target_pos
        indexed_by, not_indexed = self.index_hint()
        where = self.expr() if self.accept_keyword("WHERE") else None
        return Delete(table, where, self.returning(), indexed_by, not_indexed=not_indexed, table_pos=table_pos)

    # ---- expressions --------------------------------------------------

    def expr_list(self) -> list[Expr]:
        exprs = [self.expr()]
        while self.accept_op(","):
            exprs.append(self.expr())
        return exprs

    def expr(self) -> Expr:
        # A lone literal or column (as in a VALUES row) needs no trip through
        # every precedence level.
        token = self.tok
        following = self.tokens[self.i + 1] if token.kind != "EOF" else token
        if following.kind == "EOF" or (following.kind == "OP" and following.value in _LIST_ENDS):
            if token.kind in ("INTEGER", "FLOAT", "STRING", "BLOB"):
                self.advance()
                return Literal(token.value)
            if token.kind == "IDENT" and ascii_upper(token.text) not in _CURRENT_WORDS:
                self.advance()
                return Column(token.value, None, token.pos)
        return self.or_expr()

    def or_expr(self) -> Expr:
        left = self.and_expr()
        while self.accept_keyword("OR"):
            left = Binary("OR", left, self.and_expr())
        return left

    def and_expr(self) -> Expr:
        left = self.not_expr()
        while self.accept_keyword("AND"):
            left = fold_and(left, self.not_expr())
        return left

    def not_expr(self) -> Expr:
        if self.accept_keyword("NOT"):
            return Unary("NOT", self.not_expr())
        return self.equality()

    def equality(self) -> Expr:
        left = self.comparison()
        while True:
            if self.at_op("=", "==", "!=", "<>"):
                op = self.advance().value
                op = {"==": "=", "<>": "!="}.get(op, op)
                left = Binary(op, left, self.comparison())
            elif self.accept_keyword("IS"):
                op = "IS NOT" if self.accept_keyword("NOT") else "IS"
                left = Binary(op, left, self.comparison())
            elif self.at_word("ISNULL", "NOTNULL") or (
                self.at_keyword("NOT") and self.tokens[self.i + 1].kind == "KEYWORD"
                and self.tokens[self.i + 1].value == "NULL"
            ):
                # The postfix forms x ISNULL, x NOTNULL and x NOT NULL.
                op = "IS" if ascii_upper(self.advance().text) == "ISNULL" else "IS NOT"
                if op == "IS NOT" and self.at_keyword("NULL"):
                    self.advance()
                left = Binary(op, left, Literal(None))
            elif self.at_word("GLOB") or (
                self.at_keyword("NOT") and self.tokens[self.i + 1].kind == "IDENT"
                and ascii_upper(self.tokens[self.i + 1].text) == "GLOB"
            ):
                negated = bool(self.accept_keyword("NOT"))
                self.advance()
                left = Like(left, self.comparison(), negated, op="GLOB")
            elif self.at_keyword("IN", "LIKE", "BETWEEN") or (
                self.at_keyword("NOT")
                and self.tokens[self.i + 1].kind == "KEYWORD"
                and self.tokens[self.i + 1].value in ("IN", "LIKE", "BETWEEN")
            ):
                negated = bool(self.accept_keyword("NOT"))
                keyword = self.advance().value
                if keyword == "IN":
                    if self.tok.kind == "IDENT":  # x IN table: x IN (SELECT * FROM table)
                        pos = self.tok.pos
                        table = TableRef(self.advance().value, pos=pos)
                        left = InSelect(left, Select([SelectItem(Star())], [Join(table)]), negated)
                        continue
                    self.expect_op("(")
                    if self.at_query():
                        left = InSelect(left, self.query(), negated)
                    elif self.at_op(")"):
                        left = InList(left, (), negated)  # always false (true with NOT)
                    else:
                        left = InList(left, tuple(self.expr_list()), negated)
                    self.expect_op(")")
                elif keyword == "LIKE":
                    pattern = self.comparison()
                    escape = None
                    if self.at_word("ESCAPE"):
                        self.advance()
                        escape = self.comparison()
                    left = Like(left, pattern, negated, escape)
                else:
                    low = self.comparison()
                    self.expect_keyword("AND")
                    left = Between(left, low, self.comparison(), negated)
            else:
                return left

    def comparison(self) -> Expr:
        left = self.bitwise()
        while self.at_op("<", "<=", ">", ">="):
            op = self.advance().value
            left = Binary(op, left, self.bitwise())
        return left

    def bitwise(self) -> Expr:
        left = self.additive()
        while self.at_op("&", "|", "<<", ">>"):
            op = self.advance().value
            left = Binary(op, left, self.additive())
        return left

    def additive(self) -> Expr:
        left = self.multiplicative()
        while self.at_op("+", "-"):
            op = self.advance().value
            left = Binary(op, left, self.multiplicative())
        return left

    def multiplicative(self) -> Expr:
        left = self.concat()
        while self.at_op("*", "/", "%"):
            op = self.advance().value
            left = Binary(op, left, self.concat())
        return left

    def concat(self) -> Expr:
        """``||`` and the JSON operators ``->`` / ``->>`` (one precedence level, as in SQLite)."""
        left = self.collate()
        while self.at_op("||", "->", "->>"):
            op = self.advance().value
            right = self.collate()
            left = Binary("||", left, right) if op == "||" else Call(op, (left, right))
        return left

    def collate(self) -> Expr:
        """``x COLLATE name`` binds tighter than ``||``, looser than unary operators."""
        left = self.unary()
        while self.at_word("COLLATE"):
            self.advance()
            left = Collate(left, self.identifier("collation name"))
        return left

    def unary(self) -> Expr:
        if self.accept_keyword("NOT"):
            # As in SQLite's grammar, NOT may start an operand; it takes
            # everything that binds tighter than NOT.
            return Unary("NOT", self.not_expr())
        if self.at_op("-", "+", "~"):
            op = self.advance().value
            token = self.tok
            if op == "-" and token.kind == "FLOAT" and token.text == "9223372036854775808":
                self.advance()
                return Literal(-(2**63))
            return Unary(op, self.unary())
        return self.primary()

    def primary(self) -> Expr:
        token = self.tok
        if token.kind in ("INTEGER", "FLOAT", "STRING", "BLOB"):
            self.advance()
            return Literal(token.value)
        if self.accept_keyword("NULL"):
            return Literal(None)
        if token.kind == "PARAM":
            return self.parameter()
        if self.accept_op("("):
            if self.at_query():
                expr = Subquery(self.query())
            else:
                expr = self.expr()
            self.expect_op(")")
            return expr
        if self.accept_keyword("EXISTS"):
            self.expect_op("(")
            query = self.query()
            self.expect_op(")")
            return Exists(query)
        if self.accept_keyword("CASE"):
            return self.case()
        if self.accept_keyword("CAST"):
            self.expect_op("(")
            expr = self.expr()
            self.expect_keyword("AS")
            type_name = " ".join(self.type_name().split())
            self.expect_op(")")
            return Cast(expr, type_name)
        if token.kind == "KEYWORD" and token.value in ("LIKE", "IF") and self.tokens[self.i + 1].text == "(":
            self.advance()  # the functions like() and if() are spelled like keywords
            self.advance()
            return self.call(token.value)
        if token.kind == "IDENT" and ascii_upper(token.text) == "RAISE" and self.tokens[self.i + 1].text == "(":
            return self.raise_()
        if token.kind == "IDENT":
            self.advance()
            if self.accept_op("("):
                return self.call(ascii_upper(token.value))
            if ascii_upper(token.text) in ("CURRENT_DATE", "CURRENT_TIME", "CURRENT_TIMESTAMP"):
                return Call(ascii_upper(token.text), ())
            if self.accept_op("."):
                pos = self.tok.pos
                return Column(self.identifier("column name"), token.value, pos, token.pos)
            return Column(token.value, None, token.pos)
        raise self.error("expression")

    def raise_(self) -> Raise:
        self.advance()  # RAISE
        self.advance()  # (
        if self.accept_keyword("ROLLBACK"):
            kind = "ROLLBACK"
        elif self.at_word("IGNORE", "ABORT", "FAIL"):
            kind = ascii_upper(self.advance().text)
        else:
            raise self.error("IGNORE, ROLLBACK, ABORT or FAIL")
        message = None
        if kind != "IGNORE":
            self.expect_op(",")
            message = self.expr()
        self.expect_op(")")
        return Raise(kind, message)

    def case(self) -> Case:
        base = None if self.at_keyword("WHEN") else self.expr()
        whens = []
        while self.accept_keyword("WHEN"):
            condition = self.expr()
            self.expect_keyword("THEN")
            whens.append((condition, self.expr()))
        if not whens:
            raise self.error("WHEN")
        else_ = self.expr() if self.accept_keyword("ELSE") else None
        self.expect_keyword("END")
        return Case(base, tuple(whens), else_)

    def type_name(self, required: bool = True) -> str:
        """A type name as SQLite accepts it: words, then an optional (n) or (n, m).
        Any name is allowed; its affinity follows SQLite's rules
        (values.type_affinity).  A column definition may leave it out."""
        start = self.tok.pos
        if not self.at_type_word():
            if required:
                raise self.error("type name")
            return ""
        while self.at_type_word():
            self.advance()
        if self.accept_op("("):
            for _ in range(2):
                self.accept_op("-") or self.accept_op("+")
                if self.tok.kind not in ("INTEGER", "FLOAT"):
                    raise self.error("number")
                self.advance()
                if not self.accept_op(","):
                    break
            self.expect_op(")")
        return self.text[start:self.end_of_previous()]

    def at_type_word(self) -> bool:
        return self.tok.kind == "IDENT" and ascii_upper(self.tok.text) not in CONSTRAINT_WORDS

    def parameter(self) -> Parameter:
        token = self.advance()
        text = token.value
        if text.startswith("?"):
            if len(text) == 1:
                index = self.param_count + 1
            else:
                index = int(text[1:])
                if not 1 <= index <= MAX_PARAMETER_INDEX:
                    raise SQLSyntaxError(
                        f"variable number must be between ?1 and ?{MAX_PARAMETER_INDEX}",
                        self.text, token.pos,
                    )
            name = None
        else:
            name = text
            index = next((i for i, n in self.param_names.items() if n == name), None)
            if index is None:
                index = self.param_count + 1
                self.param_names[index] = name
        self.param_count = max(self.param_count, index)
        return Parameter(index, name)

    def call(self, name: str) -> Call:
        if self.accept_op("*"):
            self.expect_op(")")
            return self.window_suffix(Call(name, (Star(),)))
        if self.accept_op(")"):
            return self.window_suffix(Call(name, ()))
        distinct = bool(self.accept_keyword("DISTINCT"))
        if not distinct:
            self.accept_keyword("ALL")  # the default: count(ALL x) is count(x)
        args = tuple(self.expr_list())
        self.expect_op(")")
        return self.window_suffix(Call(name, args, distinct))


def fold_and(left: Expr, right: Expr) -> Expr:
    """``left AND right`` as SQLite's parser builds it (sqlite3ExprAnd): the
    integer 0 if a side is the integer literal 0 and neither side calls a
    function (LIKE and GLOB are functions; subqueries do not count).  This
    happens before names are resolved, so the other side may even name
    columns that do not exist.  TRUE and FALSE are still names here."""
    def is_zero(side: Expr) -> bool:
        return isinstance(side, Literal) and type(side.value) is int and side.value == 0

    if (is_zero(left) or is_zero(right)) and not any(
        isinstance(node, (Call, Like)) for side in (left, right) for node in walk_expr(side)
    ):
        return Literal(0)
    return Binary("AND", left, right)


# Functions sqlite3ExprIsConstant does not take as constant at parse time:
# the non-deterministic ones and aggregates (date and time functions count
# as constant, 'now' or not).
_NONCONSTANT_FUNCTIONS = frozenset((
    "RANDOM", "RANDOMBLOB", "CHANGES", "TOTAL_CHANGES", "LAST_INSERT_ROWID",
    "COUNT", "SUM", "TOTAL", "AVG", "GROUP_CONCAT", "STRING_AGG",
))


def is_parse_constant(expr: Expr) -> bool:
    """Whether SQLite's parser takes ``expr`` as constant (sqlite3ExprIsConstant
    before names are resolved): no columns other than TRUE and FALSE, no
    subqueries, only deterministic scalar functions."""
    for node in walk_expr(expr):
        if isinstance(node, (Subquery, InSelect, Exists)):
            return False
        if isinstance(node, Column) and not is_true_false_name(node):
            return False
        if isinstance(node, Call) and (
            node.name in _NONCONSTANT_FUNCTIONS or node.over is not None or node.filter is not None
            or (node.name in ("MIN", "MAX") and len(node.args) < 2)
        ):
            return False
    return True


def is_true_false_name(expr: object) -> bool:
    return isinstance(expr, Column) and expr.table is None and ascii_lower(expr.name) in ("true", "false")


def plain_truth_tests(expr: object) -> object:
    """``expr`` with ``x IS [NOT] TRUE / FALSE`` as plain ``x IS [NOT] 1 / 0``
    comparisons (see Parser.value_rows)."""
    if isinstance(expr, Binary) and expr.op in ("IS", "IS NOT") and is_true_false_name(expr.right):
        value = int(ascii_lower(expr.right.name) == "true")
        return Binary(expr.op, plain_truth_tests(expr.left), Literal(value))
    if not dataclasses.is_dataclass(expr) or isinstance(expr, (Subquery, InSelect, Exists)):
        return expr
    changes = {}
    for f in dataclasses.fields(expr):
        value = getattr(expr, f.name)
        if isinstance(value, (list, tuple)):
            new = type(value)(
                tuple(plain_truth_tests(part) for part in item) if isinstance(item, tuple) else plain_truth_tests(item)
                for item in value
            )
        else:
            new = plain_truth_tests(value)
        if new != value:
            changes[f.name] = new
    return dataclasses.replace(expr, **changes) if changes else expr


def walk_expr(expr: object) -> Iterator[object]:
    """Every node of an expression tree (not into subqueries' queries)."""
    yield expr
    if isinstance(expr, (Subquery, InSelect, Exists)):
        if isinstance(expr, InSelect):
            yield from walk_expr(expr.expr)
        return
    if dataclasses.is_dataclass(expr):
        for f in dataclasses.fields(expr):
            value = getattr(expr, f.name)
            if isinstance(value, (list, tuple)):
                for item in value:
                    if isinstance(item, tuple):
                        for part in item:
                            yield from walk_expr(part)
                    else:
                        yield from walk_expr(item)
            elif dataclasses.is_dataclass(value):
                yield from walk_expr(value)
