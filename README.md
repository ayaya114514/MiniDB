# MiniDB

[![tests](https://github.com/ayaya114514/MiniDB/actions/workflows/tests.yml/badge.svg)](https://github.com/ayaya114514/MiniDB/actions/workflows/tests.yml)

用 Python 从零实现的小型关系数据库，行为以 SQLite 为标准答案：解析并执行 SQL，数据按 4 KB
页存在单个文件里，表和索引都是 B+ 树，提交通过预写日志（WAL）保证原子性和崩溃恢复。

只依赖 Python 标准库（3.11–3.14），测试用 pytest。约 5,400 行实现代码（不含空行和注释），
全部带类型注解。

## 功能

**SQL**

- `CREATE TABLE [IF NOT EXISTS]`、`DROP TABLE [IF EXISTS]`；列类型 `INTEGER`、`TEXT`；
  约束 `PRIMARY KEY`、`NOT NULL`、`UNIQUE`。`INTEGER PRIMARY KEY` 是 rowid 的别名；
  其他表有隐藏的 `rowid`（也可写 `oid`、`_rowid_`）。
- `INSERT`（多行 `VALUES`、指定列）、`UPDATE`（可改主键）、`DELETE`。
- `SELECT`：`*` / `t.*`、别名、`DISTINCT`、`WHERE`、`GROUP BY`、`HAVING`、
  `ORDER BY`（多列、`ASC`/`DESC`、`NULLS FIRST/LAST`、列序号、别名）、`LIMIT`/`OFFSET`、
  不带 `FROM` 的 `SELECT`。
- 连接：`,`、`[INNER] JOIN`、`CROSS JOIN`、`LEFT [OUTER] JOIN ... ON / USING (...)`、
  `NATURAL [LEFT] JOIN`，任意多张表；FROM 里可以用子查询（derived table）。
- 子查询：标量子查询、`[NOT] IN (SELECT ...)`、`[NOT] EXISTS (...)`，可以嵌套、可以引用外层查询的
  列（相关子查询），能出现在 SELECT/WHERE/HAVING/ORDER BY/LIMIT 和 INSERT/UPDATE/DELETE 里。
- 复合查询：`UNION [ALL]`、`INTERSECT`、`EXCEPT`，带整体的 `ORDER BY` / `LIMIT`。
- 表达式：比较、`AND`/`OR`/`NOT`（三值逻辑）、`+ - * / %`、`||`、`IS [NOT]`、
  `[NOT] IN (...)`、`[NOT] BETWEEN`、`[NOT] LIKE`、`CASE`、`CAST(x AS type)`、参数 `?` / `:name`。
- 函数：`abs`、`length`、`lower`、`upper`、`coalesce`、`ifnull`、`nullif`、`typeof`、多参数
  `min`/`max`；聚合 `count`、`sum`、`avg`、`min`、`max`、`total`、`group_concat`（均支持 `DISTINCT`）。
- 索引：`CREATE [UNIQUE] INDEX [IF NOT EXISTS]`、`DROP INDEX [IF EXISTS]`，UNIQUE 列自动建索引；
  执行器对“索引列前缀等值 + 下一列范围”使用索引，也用于连接的内层表。
  `EXPLAIN [QUERY PLAN]` 显示每张表的访问路径。
- 事务：`BEGIN [DEFERRED|IMMEDIATE|EXCLUSIVE]`、`COMMIT`/`END`、`ROLLBACK`；不在事务中时每条语句
  自动提交；每条语句都是原子的（多行 `INSERT` 中途违反约束，整条语句不生效）。
- 并发（WAL 模式）：多个连接、多个进程可以同时打开同一个文件；读者按快照读，不阻塞写者，写者也
  不等读者；同一时刻一个写者，拿不到写锁时忙等到超时报 `database is locked`。
- 可靠性：每页带 CRC32 校验和，WAL 帧带链式校验和；损坏报 `DatabaseError` 而不是返回错误数据；
  任何时刻崩溃，重开后都是某次提交之后的完整状态。大事务的脏页会溢出到日志，内存有界。
- 优化器：`ANALYZE` 收集统计；按代价选择访问路径（rowid、索引、覆盖索引、`IN`/`OR` 多路索引）；
  内连接按代价重排顺序；`EXPLAIN` 查看计划。

**与 SQLite 一致的语义**（都有对照测试）：类型亲和性（`INTEGER` 列把 `'12'` 存成 12）、
比较时的亲和性转换、NULL 三值逻辑、64 位整数溢出转 REAL、整数除法、`SUM`/`AVG` 的补偿求和
（浮点结果逐位一致）、约束报错的文字、`ORDER BY 2` 这类列序号规则等。

## 使用

```sh
python -m minidb app.db          # 打开（或创建）数据库文件；不带参数则是内存数据库
```

```text
minidb> CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT NOT NULL, age INTEGER);
minidb> INSERT INTO users (name, age) VALUES ('alice', 30), ('bob', 25);
minidb> SELECT name, age + 1 FROM users WHERE age > 26;
alice|31
minidb> .btree users
- leaf (page 2, 2 keys): 1, 2
minidb> .exit
```

语句以 `;` 结束，可以跨多行。元命令：`.tables`、`.schema [TABLE]`、`.btree TABLE`（打印 B+ 树
结构）、`.help`、`.exit`。语法错误会给出行号、列号和 `^` 标记。

Python API（PEP 249 / DB-API 2.0，用法与标准库 `sqlite3` 相同）：

```python
import minidb

with minidb.connect("app.db") as conn:             # ":memory:" 为内存数据库
    conn.execute("CREATE TABLE t (a INTEGER, b TEXT)")
    conn.executemany("INSERT INTO t VALUES (?, ?)", [(1, "x"), (2, "y")])
    cur = conn.execute("SELECT b, a * 10 FROM t WHERE a >= :low ORDER BY a DESC", {"low": 1})
    print(cur.fetchall(), [d[0] for d in cur.description])   # [('y', 20), ('x', 10)] ['b', 'a * 10']
# 离开 with 块时提交；抛异常则回滚
```

- 占位符：`?`、`?NNN`、`:name`、`@name`、`$name`；参数用序列（按位置）或字典（按名字）传入。
  命名占位符只能用字典传参（与 Python 3.14 的 sqlite3 相同，更早的版本只是警告）。
  同一条 SQL 文本只解析一次（语句缓存），所以反复执行的语句请用参数而不是拼字符串。
- 事务：`connect(..., autocommit=False)`（默认）时总有一个打开的事务，需要 `commit()`；
  `autocommit=True` 时每条语句自动提交，也可以在 SQL 里写 `BEGIN ... COMMIT`。
- 更底层的 `minidb.Database(path)` 提供 `execute(sql, parameters)`（可执行多语句脚本，返回带
  `columns`/`rowcount` 的结果列表）和 `integrity_check()`（检查所有 B+ 树和索引是否一致）。

异常层次与 PEP 249 相同：`Error` → `InterfaceError`、`DatabaseError` → `OperationalError`
（含语法错误 `SQLSyntaxError`）、`IntegrityError`、`ProgrammingError` 等。

## 架构

```text
SQL 文本
  │  tokenizer.py   词法分析：关键字、标识符、数字、字符串、运算符，错误带位置
  ▼
  │  parser.py      递归下降语法分析 → 语法树（dataclass），运算符优先级与 SQLite 相同
  ▼
  │  executor.py    名字解析、表达式编译成闭包、访问路径规划、嵌套循环连接、
  │                 聚合/排序/限制、INSERT/UPDATE/DELETE 与约束检查
  │  values.py      SQL 值语义：亲和性、比较、算术、文本转换、标量和聚合函数
  │  catalog.py     schema（表、索引）存在第 1 页的 B+ 树里，打开时重建
  ▼
  │  btree.py       B+ 树：按字节大小分裂/合并/借位，叶子兄弟链，范围扫描，overflow 页
  ▼
  │  pager.py       4 KB 页（带 CRC32）的读写与缓存，空闲页链表，语句级 journal，WAL 提交与恢复
  │  locking.py     跨进程文件锁（flock）与忙等超时
  ▼
数据库文件 app.db + 预写日志 app.db-wal + 锁文件 app.db-lock
```

- **dbapi.py** 是 PEP 249 接口；**database.py** 的 `Database.execute()` 负责参数绑定、语句缓存、语句原子性、自动提交和事务状态。
- **record.py** 负责行的序列化（类型标签 + 负载）。
- **repl.py** 是命令行界面。

几个关键设计（完整理由见 [DECISIONS.md](DECISIONS.md)）：

- **页对象缓存**：pager 缓存解码后的页对象，B+ 树直接在 Python 列表上 `bisect`，写回时才序列化。
- **B+ 树**：节点满/欠满按字节数判断，同一套代码服务整数 key 的表树和变长 key 的索引树；
  根页号终生不变；顺序追加时不均匀分裂，页填充率约 75%；过长的 key 和 value 放溢出页链。
- **执行**：语句编译成闭包组成的计划并缓存复用；覆盖索引、索引顺序免排序、LIMIT 时 top-k。
- **记录格式**：每列一个类型码的紧凑编码，按头部缓存 `struct` 解码器。
- **索引 key**：索引列值的 SQLite 排序键元组 + rowid，保证唯一且顺序与 SQL 比较一致。
- **WAL 模式**：提交向 `-wal` 追加页帧（最后一帧为提交帧）并 fsync；读者取“最后一个提交帧”为快照；
  没有读者时 checkpoint 把日志拷回数据文件。大事务的脏页可提前作为未提交帧写入日志，回滚时截断。

## 测试

```sh
.venv/bin/python -m pytest                                        # 全部测试（580+ 个）
.venv/bin/python tests/fuzz.py --seeds 0-999 --statements 600     # 大规模模糊对照
.venv/bin/python tests/benchmark.py --rows 100000                 # 性能测试
.venv/bin/python tools/coverage.py                                # 行覆盖率（标准库 trace）
```

GitHub Actions 在 Linux 上用 Python 3.11–3.14 跑全部测试（警告视为错误），并跑三段 fuzz：
固定种子、数据库文件模式、以及每次运行都换一批的新种子（每周定时运行一次）。

- **与 sqlite3 对照**（`tests/sqlcompare.py`）：同一条 SQL 在 MiniDB 和 sqlite3 上执行，要求都成功
  且结果相同（区分 1 和 1.0），或者都失败且异常类别相同，部分用例逐字比较报错。
- **模糊测试**（`tests/fuzz.py`）：随机 schema（约束、单列/多列/唯一索引）+ 随机增删改查、
  嵌套表达式、聚合、连接、事务、建删索引，每个种子结束时做 `integrity_check`。
- **B+ 树**：上万次随机插入删除后校验不变量（有序、分隔键边界、同深度、填充率、兄弟链）。
- **类型注解**：`tests/test_annotations.py` 要求每个函数和方法的参数与返回值都有注解，
  并用 `typing.get_type_hints` 解析一遍（写错的名字会失败）。
- **覆盖率**：`tools/coverage.py` 只用标准库 `trace` + `ast` 统计语句覆盖率，目前 99.2%；
  没覆盖的主要是防御性分支（Windows 无 `fcntl`、不可能的内部状态）。
- **崩溃恢复**：在提交的每一步（写 WAL 帧、写提交帧、fsync 日志）和 checkpoint 的每一步
  （拷页、fsync、截断日志）模拟崩溃，包括子进程里真实的 `os._exit`，以及截断/损坏的 WAL，
  重开后数据必须是事务前或事务后的完整状态。

## 性能

100,000 行（`id, name, age, city`），数据库文件，Apple Silicon，Python 3.12，与 sqlite3 同样的 SQL
（`tests/benchmark.py`）：

| 操作 | MiniDB | sqlite3 |
|---|---:|---:|
| 插入 10 万行，每行一条 INSERT（`?` 参数），一个事务 | 0.94 s | 0.08 s |
| 插入 10 万行，每行一条 INSERT（拼字面量），一个事务 | 3.2 s | 0.25 s |
| 自动提交插入 1000 行（每行一次 commit + fsync） | 0.13 s | 0.21 s |
| 1 万次主键点查（`?` 参数） | 0.12 s | 0.04 s |
| 全表扫描 `count(*) WHERE age > 50` | 0.12 s | 0.003 s |
| `GROUP BY city` 三个聚合 | 0.16 s | 0.03 s |
| `ORDER BY age, name LIMIT 10` | 0.13 s | 0.004 s |
| `CREATE INDEX` on age | 0.65 s | 0.02 s |
| 10 万行与小表连接（每行一次索引查找） | 0.26 s | 0.006 s |
| 数据库文件大小 | 15.7 MB | 9.3 MB |

纯 Python 实现，点查和提交接近 SQLite，扫描/聚合慢 5–40 倍（逐行求值的开销）。
反复执行的语句请用 `?` 参数：同一条 SQL 只解析和编译一次。详细历史见 [PROGRESS.md](PROGRESS.md)。

## 已知限制

- 列类型只有 `INTEGER`、`TEXT`（以及运算或 `CAST` 产生的 REAL）；没有 BLOB、视图、触发器、
  `ALTER TABLE`、`RIGHT/FULL JOIN`、窗口函数、CTE（`WITH`）；子查询里不能使用外层查询的聚合函数。
- REAL 转文本时，少数没有短十进制表示的值与 SQLite 在最后几位数字上不同（SQLite 用自己的近似
  转换算法），见 DECISIONS.md D20。
- 当 SQLite 的结果取决于它的查询计划时（相等的 1 和 1.0 中 DISTINCT/GROUP BY 保留哪一个、
  多行 UPDATE 先处理哪一行导致 UNIQUE 冲突、常量表达式出错的求值时机），MiniDB 不保证选择相同。
- 一直有读者时 checkpoint 做不成，日志会持续变长；Windows 上没有 `fcntl`，不加锁。
- 数据库文件不会收缩（空闲页只复用）；目录里会保留（可能为空的）`-wal` 与 `-lock` 文件。
- 与 sqlite3 对照时，Python 3.11 的 sqlite3 没有 `autocommit` 参数，两组事务行为对照测试在
  3.11 上跳过（MiniDB 自身行为不随 Python 版本变化）。
- 优化器是启发式代价模型，不支持 LEFT JOIN 的重排；只有第一张表的升序 ORDER BY 能利用
  索引/rowid 顺序免排序。
