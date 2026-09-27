"""The public entry point: a connection to one database file."""

from minidb.catalog import Catalog
from minidb.executor import Executor, Result
from minidb.pager import Pager
from minidb.parser import parse_script


class Database:
    """A MiniDB database stored in the file ``path`` (``None`` keeps it in memory).

    ``execute`` runs one or more SQL statements and returns the result of the
    last one.  Every statement is atomic: if it fails, none of its changes
    remain.  Outside an explicit transaction every statement commits at once.
    """

    def __init__(self, path=None):
        self.pager = Pager(path)
        self.catalog = Catalog(self.pager)
        self.pager.commit()
        self.executor = Executor(self.catalog)

    def execute(self, sql):
        result = Result()
        for result in self.execute_each(sql):
            pass
        return result

    def execute_each(self, sql):
        """Run the statements in ``sql`` one by one, yielding each result."""
        for stmt in parse_script(sql):
            yield self.execute_statement(stmt)

    def execute_statement(self, stmt):
        self.pager.begin_statement()
        try:
            result = self.executor.execute(stmt)
        except BaseException:
            self.pager.rollback_statement()
            self.catalog.load()
            raise
        self.pager.end_statement()
        self.pager.commit()
        self.pager.shrink_cache()
        return result

    def close(self):
        self.pager.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()


def connect(path=None):
    return Database(path)
