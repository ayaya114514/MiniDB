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

from dataclasses import dataclass, field

from minidb.tokenizer import SQLSyntaxError, tokenize

# ---- expressions -------------------------------------------------------


@dataclass(frozen=True)
class Literal:
    value: object


@dataclass(frozen=True)
class Column:
    name: str
    table: str | None = None


@dataclass(frozen=True)
class Star:
    """``*`` or ``table.*`` in a select list, or the argument of ``COUNT(*)``."""

    table: str | None = None


@dataclass(frozen=True)
class Unary:
    op: str  # "-", "+" or "NOT"
    operand: object


@dataclass(frozen=True)
class Binary:
    op: str  # OR AND = != < <= > >= IS "IS NOT" + - * / % ||
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


@dataclass(frozen=True)
class Call:
    name: str  # upper case
    args: tuple
    distinct: bool = False


# ---- statements --------------------------------------------------------


@dataclass
class ColumnDef:
    name: str
    type: str  # "INTEGER" or "TEXT"
    primary_key: bool = False
    not_null: bool = False
    unique: bool = False


@dataclass
class CreateTable:
    name: str
    columns: list
    if_not_exists: bool = False


@dataclass
class DropTable:
    name: str
    if_exists: bool = False


@dataclass
class Insert:
    table: str
    columns: list | None
    rows: list  # list of lists of expressions


@dataclass
class SelectItem:
    expr: object
    alias: str | None = None
    text: str = field(default="", compare=False)  # source text, for the column name


@dataclass
class TableRef:
    name: str
    alias: str | None = None


@dataclass
class Select:
    items: list
    source: TableRef | None = None
    where: object = None
    distinct: bool = False


@dataclass
class Explain:
    """``EXPLAIN [QUERY PLAN] stmt``: describe how a statement would read its tables."""

    statement: object


@dataclass
class Update:
    table: str
    assignments: list  # (column name, expression) pairs
    where: object = None


@dataclass
class Delete:
    table: str
    where: object = None


# ---- parser ------------------------------------------------------------

TYPE_NAMES = {"INTEGER", "TEXT"}


def parse(text):
    """Parse a single SQL statement (a trailing ``;`` is optional)."""
    statements = parse_script(text)
    if len(statements) != 1:
        raise SQLSyntaxError(
            "expected exactly one statement" if statements else "empty statement", text, 0
        )
    return statements[0]


def parse_script(text):
    """Parse zero or more statements separated by ``;``."""
    return Parser(text).parse_script()


class Parser:
    def __init__(self, text):
        self.text = text
        self.tokens = tokenize(text)
        self.i = 0

    # ---- token helpers ------------------------------------------------

    @property
    def tok(self):
        return self.tokens[self.i]

    def advance(self):
        token = self.tokens[self.i]
        if token.kind != "EOF":
            self.i += 1
        return token

    def error(self, expected, token=None):
        token = token or self.tok
        where = "at end of input" if token.kind == "EOF" else f'near "{token.text}"'
        return SQLSyntaxError(f"syntax error {where}: expected {expected}", self.text, token.pos)

    def at_keyword(self, *words):
        return self.tok.kind == "KEYWORD" and self.tok.value in words

    def at_op(self, *ops):
        return self.tok.kind == "OP" and self.tok.value in ops

    def accept_keyword(self, word):
        if self.at_keyword(word):
            return self.advance()
        return None

    def accept_op(self, op):
        if self.at_op(op):
            return self.advance()
        return None

    def expect_keyword(self, word):
        if not self.at_keyword(word):
            raise self.error(word)
        return self.advance()

    def expect_op(self, op):
        if not self.at_op(op):
            raise self.error(f'"{op}"')
        return self.advance()

    def expect_word(self, word):
        """Expect a non-reserved word such as KEY (tokenized as an identifier)."""
        if self.tok.kind == "IDENT" and self.tok.text.upper() == word:
            return self.advance()
        raise self.error(word)

    def identifier(self, what="identifier"):
        if self.tok.kind != "IDENT":
            raise self.error(what)
        return self.advance().value

    # ---- statements ---------------------------------------------------

    def parse_script(self):
        statements = []
        while True:
            while self.accept_op(";"):
                pass
            if self.tok.kind == "EOF":
                return statements
            statements.append(self.statement())
            if self.tok.kind != "EOF" and not self.at_op(";"):
                raise self.error('";" or end of statement')

    def statement(self):
        if self.accept_keyword("EXPLAIN"):
            if self.tok.kind == "IDENT" and self.tok.text.upper() == "QUERY":
                self.advance()
                self.expect_word("PLAN")
            if not self.at_keyword("SELECT", "UPDATE", "DELETE"):
                raise self.error("SELECT, UPDATE or DELETE")
            return Explain(self.statement())
        if self.at_keyword("SELECT"):
            return self.select()
        if self.at_keyword("INSERT"):
            return self.insert()
        if self.at_keyword("UPDATE"):
            return self.update()
        if self.at_keyword("DELETE"):
            return self.delete()
        if self.at_keyword("CREATE"):
            return self.create()
        if self.at_keyword("DROP"):
            return self.drop()
        raise self.error("a statement")

    def create(self):
        self.expect_keyword("CREATE")
        self.expect_keyword("TABLE")
        if_not_exists = False
        if self.accept_keyword("IF"):
            self.expect_keyword("NOT")
            self.expect_keyword("EXISTS")
            if_not_exists = True
        name = self.identifier("table name")
        self.expect_op("(")
        columns = [self.column_def()]
        while self.accept_op(","):
            columns.append(self.column_def())
        self.expect_op(")")
        return CreateTable(name, columns, if_not_exists)

    def column_def(self):
        name = self.identifier("column name")
        type_token = self.tok
        if type_token.kind != "IDENT" or type_token.value.upper() not in TYPE_NAMES:
            raise self.error("column type INTEGER or TEXT")
        self.advance()
        column = ColumnDef(name, type_token.value.upper())
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

    def drop(self):
        self.expect_keyword("DROP")
        self.expect_keyword("TABLE")
        if_exists = False
        if self.accept_keyword("IF"):
            self.expect_keyword("EXISTS")
            if_exists = True
        return DropTable(self.identifier("table name"), if_exists)

    def insert(self):
        self.expect_keyword("INSERT")
        self.expect_keyword("INTO")
        table = self.identifier("table name")
        columns = None
        if self.accept_op("("):
            columns = [self.identifier("column name")]
            while self.accept_op(","):
                columns.append(self.identifier("column name"))
            self.expect_op(")")
        self.expect_keyword("VALUES")
        rows = [self.value_row()]
        while self.accept_op(","):
            rows.append(self.value_row())
        return Insert(table, columns, rows)

    def value_row(self):
        self.expect_op("(")
        values = self.expr_list()
        self.expect_op(")")
        return values

    def select(self):
        self.expect_keyword("SELECT")
        distinct = bool(self.accept_keyword("DISTINCT"))
        items = [self.select_item()]
        while self.accept_op(","):
            items.append(self.select_item())
        stmt = Select(items, distinct=distinct)
        if self.accept_keyword("FROM"):
            stmt.source = self.table_ref()
        if self.accept_keyword("WHERE"):
            stmt.where = self.expr()
        return stmt

    def select_item(self):
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

    def table_ref(self):
        name = self.identifier("table name")
        alias = None
        if self.accept_keyword("AS"):
            alias = self.identifier("alias")
        elif self.tok.kind == "IDENT":
            alias = self.advance().value
        return TableRef(name, alias)

    def update(self):
        self.expect_keyword("UPDATE")
        table = self.identifier("table name")
        self.expect_keyword("SET")
        assignments = [self.assignment()]
        while self.accept_op(","):
            assignments.append(self.assignment())
        where = self.expr() if self.accept_keyword("WHERE") else None
        return Update(table, assignments, where)

    def assignment(self):
        name = self.identifier("column name")
        self.expect_op("=")
        return name, self.expr()

    def delete(self):
        self.expect_keyword("DELETE")
        self.expect_keyword("FROM")
        table = self.identifier("table name")
        where = self.expr() if self.accept_keyword("WHERE") else None
        return Delete(table, where)

    # ---- expressions --------------------------------------------------

    def expr_list(self):
        exprs = [self.expr()]
        while self.accept_op(","):
            exprs.append(self.expr())
        return exprs

    def expr(self):
        return self.or_expr()

    def or_expr(self):
        left = self.and_expr()
        while self.accept_keyword("OR"):
            left = Binary("OR", left, self.and_expr())
        return left

    def and_expr(self):
        left = self.not_expr()
        while self.accept_keyword("AND"):
            left = Binary("AND", left, self.not_expr())
        return left

    def not_expr(self):
        if self.accept_keyword("NOT"):
            return Unary("NOT", self.not_expr())
        return self.equality()

    def equality(self):
        left = self.comparison()
        while True:
            if self.at_op("=", "==", "!=", "<>"):
                op = self.advance().value
                op = {"==": "=", "<>": "!="}.get(op, op)
                left = Binary(op, left, self.comparison())
            elif self.accept_keyword("IS"):
                op = "IS NOT" if self.accept_keyword("NOT") else "IS"
                left = Binary(op, left, self.comparison())
            elif self.at_keyword("IN", "LIKE", "BETWEEN") or (
                self.at_keyword("NOT")
                and self.tokens[self.i + 1].kind == "KEYWORD"
                and self.tokens[self.i + 1].value in ("IN", "LIKE", "BETWEEN")
            ):
                negated = bool(self.accept_keyword("NOT"))
                keyword = self.advance().value
                if keyword == "IN":
                    self.expect_op("(")
                    items = tuple(self.expr_list())
                    self.expect_op(")")
                    left = InList(left, items, negated)
                elif keyword == "LIKE":
                    left = Like(left, self.comparison(), negated)
                else:
                    low = self.comparison()
                    self.expect_keyword("AND")
                    left = Between(left, low, self.comparison(), negated)
            else:
                return left

    def comparison(self):
        left = self.additive()
        while self.at_op("<", "<=", ">", ">="):
            op = self.advance().value
            left = Binary(op, left, self.additive())
        return left

    def additive(self):
        left = self.multiplicative()
        while self.at_op("+", "-"):
            op = self.advance().value
            left = Binary(op, left, self.multiplicative())
        return left

    def multiplicative(self):
        left = self.concat()
        while self.at_op("*", "/", "%"):
            op = self.advance().value
            left = Binary(op, left, self.concat())
        return left

    def concat(self):
        left = self.unary()
        while self.accept_op("||"):
            left = Binary("||", left, self.unary())
        return left

    def unary(self):
        if self.accept_keyword("NOT"):
            # As in SQLite's grammar, NOT may start an operand; it takes
            # everything that binds tighter than NOT.
            return Unary("NOT", self.not_expr())
        if self.at_op("-", "+"):
            op = self.advance().value
            token = self.tok
            if op == "-" and token.kind == "FLOAT" and token.text == "9223372036854775808":
                self.advance()
                return Literal(-(2**63))
            return Unary(op, self.unary())
        return self.primary()

    def primary(self):
        token = self.tok
        if token.kind in ("INTEGER", "FLOAT", "STRING"):
            self.advance()
            return Literal(token.value)
        if self.accept_keyword("NULL"):
            return Literal(None)
        if self.accept_op("("):
            expr = self.expr()
            self.expect_op(")")
            return expr
        if token.kind == "IDENT":
            self.advance()
            if self.accept_op("("):
                return self.call(token.value.upper())
            if self.accept_op("."):
                return Column(self.identifier("column name"), token.value)
            return Column(token.value)
        raise self.error("expression")

    def call(self, name):
        if self.accept_op("*"):
            self.expect_op(")")
            return Call(name, (Star(),))
        if self.accept_op(")"):
            return Call(name, ())
        distinct = bool(self.accept_keyword("DISTINCT"))
        args = tuple(self.expr_list())
        self.expect_op(")")
        return Call(name, args, distinct)
