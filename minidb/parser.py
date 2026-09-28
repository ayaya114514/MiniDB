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

from dataclasses import dataclass, field
from typing import Union

from minidb.errors import NotSupportedError, OperationalError
from minidb.tokenizer import SQLSyntaxError, Token, tokenize
from minidb.values import ascii_upper

# ---- expressions -------------------------------------------------------


@dataclass(frozen=True)
class Literal:
    value: object


@dataclass(frozen=True)
class Column:
    name: str
    table: str | None = None


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
class Call:
    name: str  # upper case
    args: tuple
    distinct: bool = False


# ---- statements --------------------------------------------------------


@dataclass
class ColumnDef:
    name: str
    type: str  # the declared type in upper case, e.g. "INTEGER", "VARCHAR(30)", "" for none
    primary_key: bool = False
    not_null: bool = False
    unique: bool = False


@dataclass
class CreateTable:
    name: str
    columns: list
    if_not_exists: bool = False


@dataclass
class CreateIndex:
    name: str
    table: str
    columns: list  # column names
    unique: bool = False
    if_not_exists: bool = False


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


@dataclass
class Insert:
    table: str
    columns: list | None
    rows: list  # list of lists of expressions (VALUES)
    query: object = None  # or a Select / Compound (INSERT ... SELECT)
    conflict: str = "ABORT"  # INSERT OR <conflict>: ABORT, FAIL, IGNORE, REPLACE or ROLLBACK
    upsert: list = field(default_factory=list)  # Upsert clauses, in order
    returning: list | None = None  # SelectItems of RETURNING


@dataclass
class SelectItem:
    expr: object
    alias: str | None = None
    text: str = field(default="", compare=False)  # source text, for the column name


@dataclass
class TableRef:
    name: str
    alias: str | None = None
    indexed_by: str | None = None  # INDEXED BY <index>: must exist; only checked


@dataclass
class DerivedTable:
    """A subquery in FROM: ``(SELECT ...) [AS] alias``."""

    query: object
    alias: str | None = None


@dataclass
class Join:
    """One table of a FROM clause and how it joins to the tables before it."""

    table: object  # TableRef or DerivedTable
    kind: str = "INNER"  # INNER (also for "," and CROSS JOIN) or LEFT
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

    selects: list
    operators: list  # "UNION", "UNION ALL", "INTERSECT" or "EXCEPT", one per join
    order_by: list = field(default_factory=list)
    limit: object = None
    offset: object = None


@dataclass
class Update:
    table: str
    assignments: list  # (column name, expression) pairs
    where: object = None
    conflict: str = "ABORT"
    returning: list | None = None
    indexed_by: str | None = None


@dataclass
class Delete:
    table: str
    where: object = None
    returning: list | None = None
    indexed_by: str | None = None


@dataclass
class Reindex:
    name: str | None = None  # an index, a table or a collation; None: every index


# Any expression node, and any statement.
Expr = Union[
    Literal, Parameter, Column, Star, Unary, Binary, Between, InList, Like, Case, Cast,
    Subquery, InSelect, Exists, Call,
]
Statement = Union[
    CreateTable, CreateIndex, CreateView, DropTable, DropIndex, DropView, Reindex, Insert, Select, Compound, Update, Delete,
    Begin, Commit, Rollback, Analyze, Explain,
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


class Parser:
    def __init__(self, text: str) -> None:
        self.text = text
        self.tokens = tokenize(text)
        self.i = 0

    # ---- token helpers ------------------------------------------------

    @property
    def tok(self) -> Token:
        return self.tokens[self.i]

    def advance(self) -> Token:
        token = self.tokens[self.i]
        if token.kind != "EOF":
            self.i += 1
        return token

    def error(self, expected: str, token: Token | None = None) -> SQLSyntaxError:
        token = token or self.tok
        where = "at end of input" if token.kind == "EOF" else f'near "{token.text}"'
        return SQLSyntaxError(f"syntax error {where}: expected {expected}", self.text, token.pos)

    def at_keyword(self, *words: str) -> bool:
        return self.tok.kind == "KEYWORD" and self.tok.value in words

    def at_op(self, *ops: str) -> bool:
        return self.tok.kind == "OP" and self.tok.value in ops

    def accept_keyword(self, word: str) -> Token | None:
        if self.at_keyword(word):
            return self.advance()
        return None

    def accept_op(self, op: str) -> Token | None:
        if self.at_op(op):
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

    def at_word(self, word: str) -> bool:
        """At a non-reserved word such as VIEW (tokenized as an identifier)?"""
        return self.tok.kind == "IDENT" and ascii_upper(self.tok.text) == word

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
            if not self.at_keyword("SELECT", "UPDATE", "DELETE"):
                raise self.error("SELECT, UPDATE or DELETE")
            return Explain(self.statement())
        if self.at_keyword("SELECT"):
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
        if self.at_word("REINDEX"):
            self.advance()
            if self.tok.kind != "IDENT":
                return Reindex()
            name = self.advance().value
            if self.accept_op("."):  # schema.name: only the main schema exists
                name = self.identifier("index or table name")
            return Reindex(name)
        raise self.error("a statement")

    def create(self) -> CreateTable | CreateIndex | CreateView:
        create = self.expect_keyword("CREATE")
        if self.at_keyword("UNIQUE", "INDEX"):
            return self.create_index()
        temp = self.at_word("TEMP") or self.at_word("TEMPORARY")
        if temp:
            self.advance()
        if self.at_word("VIEW"):
            return self.create_view(create.pos, temp)
        if temp:
            raise NotSupportedError("temporary tables are not supported")
        self.expect_keyword("TABLE")
        if_not_exists = self.if_not_exists()
        name = self.identifier("table name")
        self.expect_op("(")
        columns = [self.column_def()]
        while self.accept_op(","):
            columns.append(self.column_def())
        self.expect_op(")")
        return CreateTable(name, columns, if_not_exists)

    def if_not_exists(self) -> bool:
        if self.accept_keyword("IF"):
            self.expect_keyword("NOT")
            self.expect_keyword("EXISTS")
            return True
        return False

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

    def create_index(self) -> CreateIndex:
        unique = bool(self.accept_keyword("UNIQUE"))
        self.expect_keyword("INDEX")
        if_not_exists = self.if_not_exists()
        name = self.identifier("index name")
        self.expect_keyword("ON")
        table = self.identifier("table name")
        self.expect_op("(")
        columns = [self.indexed_column()]
        while self.accept_op(","):
            columns.append(self.indexed_column())
        self.expect_op(")")
        return CreateIndex(name, table, columns, unique, if_not_exists)

    def indexed_column(self) -> str:
        name = self.identifier("column name")
        if not self.accept_keyword("ASC"):
            self.accept_keyword("DESC")  # accepted; the index order is always ascending
        return name

    def column_def(self) -> ColumnDef:
        name = self.identifier("column name")
        column = ColumnDef(name, ascii_upper(self.type_name(required=False)))
        while True:
            if self.accept_keyword("PRIMARY"):
                self.expect_word("KEY")
                column.primary_key = True
            elif self.accept_keyword("NOT"):
                self.expect_keyword("NULL")
                column.not_null = True
            elif self.accept_keyword("NULL"):
                pass
            elif self.accept_keyword("UNIQUE"):
                column.unique = True
            else:
                return column

    def drop(self) -> DropTable | DropIndex | DropView:
        self.expect_keyword("DROP")
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

    def conflict_clause(self) -> str:
        """``OR <resolution>`` after INSERT or UPDATE (ABORT if absent)."""
        if not self.accept_keyword("OR"):
            return "ABORT"
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
                clause.columns = [self.indexed_column()]
                while self.accept_op(","):
                    clause.columns.append(self.indexed_column())
                self.expect_op(")")
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
        return clauses

    def insert(self) -> Insert:
        if self.at_word("REPLACE"):
            self.advance()
            conflict = "REPLACE"
        else:
            self.expect_keyword("INSERT")
            conflict = self.conflict_clause()
        self.expect_keyword("INTO")
        table = self.identifier("table name")
        columns = None
        if self.accept_op("("):
            columns = [self.identifier("column name")]
            while self.accept_op(","):
                columns.append(self.identifier("column name"))
            self.expect_op(")")
        if self.at_keyword("SELECT"):
            stmt = Insert(table, columns, [], self.query(), conflict)
        else:
            self.expect_keyword("VALUES")
            rows = [self.value_row()]
            while self.accept_op(","):
                rows.append(self.value_row())
            stmt = Insert(table, columns, rows, None, conflict)
        stmt.upsert = self.upsert_clauses()
        for clause in stmt.upsert[:-1]:
            if clause.columns is None:
                raise OperationalError("a conflict target is required on all but the last ON CONFLICT clause")
        stmt.returning = self.returning()
        return stmt

    def value_row(self) -> list[Expr]:
        self.expect_op("(")
        values = self.expr_list()
        self.expect_op(")")
        return values

    def query(self) -> Select | Compound:
        """A SELECT or a compound SELECT, with ORDER BY and LIMIT."""
        selects = [self.select_core()]
        operators = []
        while self.at_keyword("UNION", "INTERSECT", "EXCEPT"):
            operator = self.advance().value
            if operator == "UNION" and self.accept_keyword("ALL"):
                operator = "UNION ALL"
            operators.append(operator)
            selects.append(self.select_core())
        stmt = selects[0] if not operators else Compound(selects, operators)
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
        return stmt

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
        if self.at_op("(") and not (self.tokens[self.i + 1].kind == "KEYWORD"
                                    and self.tokens[self.i + 1].value == "SELECT"):
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
            raise NotSupportedError("LEFT JOIN of a parenthesized join is not supported")
        return source

    def table_ref(self) -> TableRef | DerivedTable:
        if self.at_op("(") and self.tokens[self.i + 1].kind == "KEYWORD" and self.tokens[self.i + 1].value == "SELECT":
            self.advance()
            query = self.query()
            self.expect_op(")")
            alias = None
            if self.accept_keyword("AS"):
                alias = self.identifier("alias")
            elif self.tok.kind == "IDENT":
                alias = self.advance().value
            return DerivedTable(query, alias)
        name = self.identifier("table name")
        alias = None
        if self.accept_keyword("AS"):
            alias = self.identifier("alias")
        elif self.tok.kind == "IDENT" and not self.at_word("INDEXED"):
            alias = self.advance().value
        return TableRef(name, alias, self.index_hint())

    def index_hint(self) -> str | None:
        """``INDEXED BY <index>`` (returned) or ``NOT INDEXED`` (None).  They
        are checked but do not steer the planner."""
        if self.at_word("INDEXED"):
            self.advance()
            self.expect_keyword("BY")
            return self.identifier("index name")
        if self.at_keyword("NOT") and self.tokens[self.i + 1].kind == "IDENT" \
                and ascii_upper(self.tokens[self.i + 1].text) == "INDEXED":
            self.advance()
            self.advance()
        return None

    def update(self) -> Update:
        self.expect_keyword("UPDATE")
        conflict = self.conflict_clause()
        table = self.identifier("table name")
        indexed_by = self.index_hint()
        self.expect_keyword("SET")
        assignments = [self.assignment()]
        while self.accept_op(","):
            assignments.append(self.assignment())
        where = self.expr() if self.accept_keyword("WHERE") else None
        return Update(table, assignments, where, conflict, self.returning(), indexed_by)

    def assignment(self) -> tuple[str, Expr]:
        name = self.identifier("column name")
        self.expect_op("=")
        return name, self.expr()

    def delete(self) -> Delete:
        self.expect_keyword("DELETE")
        self.expect_keyword("FROM")
        table = self.identifier("table name")
        indexed_by = self.index_hint()
        where = self.expr() if self.accept_keyword("WHERE") else None
        return Delete(table, where, self.returning(), indexed_by)

    # ---- expressions --------------------------------------------------

    def expr_list(self) -> list[Expr]:
        exprs = [self.expr()]
        while self.accept_op(","):
            exprs.append(self.expr())
        return exprs

    def expr(self) -> Expr:
        return self.or_expr()

    def or_expr(self) -> Expr:
        left = self.and_expr()
        while self.accept_keyword("OR"):
            left = Binary("OR", left, self.and_expr())
        return left

    def and_expr(self) -> Expr:
        left = self.not_expr()
        while self.accept_keyword("AND"):
            left = Binary("AND", left, self.not_expr())
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
                        table = TableRef(self.advance().value)
                        left = InSelect(left, Select([SelectItem(Star())], [Join(table)]), negated)
                        continue
                    self.expect_op("(")
                    if self.at_keyword("SELECT"):
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
        left = self.unary()
        while self.accept_op("||"):
            left = Binary("||", left, self.unary())
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
            if self.at_keyword("SELECT"):
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
            type_name = self.type_name()
            self.expect_op(")")
            return Cast(expr, type_name)
        if token.kind == "KEYWORD" and token.value in ("LIKE", "IF") and self.tokens[self.i + 1].text == "(":
            self.advance()  # the functions like() and if() are spelled like keywords
            self.advance()
            return self.call(token.value)
        if token.kind == "IDENT":
            self.advance()
            if self.accept_op("("):
                return self.call(ascii_upper(token.value))
            if self.accept_op("."):
                return Column(self.identifier("column name"), token.value)
            return Column(token.value)
        raise self.error("expression")

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
        return " ".join(self.text[start:self.tok.pos].split())

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
            return Call(name, (Star(),))
        if self.accept_op(")"):
            return Call(name, ())
        distinct = bool(self.accept_keyword("DISTINCT"))
        if not distinct:
            self.accept_keyword("ALL")  # the default: count(ALL x) is count(x)
        args = tuple(self.expr_list())
        self.expect_op(")")
        return Call(name, args, distinct)
