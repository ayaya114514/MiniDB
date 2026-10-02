# MiniDB

[![tests](https://github.com/ayaya114514/MiniDB/actions/workflows/tests.yml/badge.svg)](https://github.com/ayaya114514/MiniDB/actions/workflows/tests.yml)

用 Python 从零实现的小型关系数据库，行为以 SQLite 为标准答案：解析并执行 SQL，数据按 4 KB
页存在单个文件里，表和索引都是 B+ 树，提交通过预写日志（WAL）保证原子性和崩溃恢复。

只依赖 Python 标准库（3.11–3.14），测试用 pytest。约 1.5 万行实现代码（不含空行、注释和文档字符串），
全部带类型注解。

**Playground**：<https://ayaya114514.github.io/MiniDB/> —— 浏览器里（Pyodide）运行 MiniDB，
每条查询显示计划，并画出所选表或索引的 B+ 树；也能打开（选择或拖入）本地的 SQLite 文件，查询、修改后导出，
看它真实的页面：每一页属于谁、一页里的页头、单元格指针、单元格、空闲块和碎片。数据只在本机内存里，不上传。

## 功能

**SQL**

- `CREATE TABLE [IF NOT EXISTS]`、`DROP TABLE [IF EXISTS]`；任意类型名，按 SQLite 规则得到
  INTEGER / REAL / NUMERIC / TEXT / BLOB 亲和性；约束 `PRIMARY KEY`、`NOT NULL`、`UNIQUE`、`CHECK`、
  `DEFAULT`、`COLLATE`（列级和表级，带 `ON CONFLICT`）、`AUTOINCREMENT`。`INTEGER PRIMARY KEY` 是 rowid
  的别名；其他表有隐藏的 `rowid`（也可写 `oid`、`_rowid_`）。
- 外键：`REFERENCES` / `FOREIGN KEY`、`PRAGMA foreign_keys`、`ON DELETE / ON UPDATE`（CASCADE / SET NULL /
  SET DEFAULT / RESTRICT）、`DEFERRABLE INITIALLY DEFERRED`、`PRAGMA defer_foreign_keys`。
- 触发器：`CREATE TRIGGER` / `DROP TRIGGER`，`BEFORE` / `AFTER` / `INSTEAD OF`（视图上）、
  `INSERT` / `UPDATE [OF ...]` / `DELETE`、`WHEN`、`NEW` / `OLD`、`RAISE(...)`、`PRAGMA recursive_triggers`。
- 排序规则 BINARY / NOCASE / RTRIM：比较、`ORDER BY`、`GROUP BY` / `DISTINCT`、索引。
- PRAGMA：`table_info` / `table_xinfo`、`index_list` / `index_info` / `index_xinfo`、`foreign_key_list`、
  `foreign_key_check`、`integrity_check` / `quick_check`、`user_version`、`application_id`、`schema_version`、
  `page_size` / `page_count` / `freelist_count`、`journal_mode` 等，以及表值函数形式 `pragma_xxx(...)`。
- `ALTER TABLE ... RENAME TO / RENAME COLUMN / ADD COLUMN / DROP COLUMN`（视图里的引用一并改写）；
  `CREATE [TEMP] VIEW` / `DROP VIEW`；`REINDEX`；`VACUUM`（重写紧凑文件、收缩文件）与 `VACUUM INTO 'file'`。
- `INSERT`（多行 `VALUES`、`DEFAULT VALUES`、`INSERT ... SELECT`、`(rowid, ...)` 列）、`UPDATE`、`DELETE`；
  冲突处理 `INSERT OR REPLACE/IGNORE/ABORT/FAIL/ROLLBACK`、`REPLACE`、UPSERT
  （`ON CONFLICT (...) DO UPDATE SET ... WHERE / DO NOTHING`，多个子句）、`RETURNING`。
- `SELECT`：`*` / `t.*`、别名（也可在 WHERE/GROUP BY/HAVING/ORDER BY 的表达式里引用）、`DISTINCT`、
  `WHERE`、`GROUP BY`、`HAVING`、`ORDER BY`（多列、`ASC`/`DESC`、`NULLS FIRST/LAST`、列序号）、
  `LIMIT`/`OFFSET`、不带 `FROM` 的 `SELECT`、`VALUES (...), (...)`。
- `WITH [RECURSIVE]`（CTE，递归 CTE 支持 `UNION` 去重、`ORDER BY` 队列和 `LIMIT`）。
- 连接：`,`、`[INNER] JOIN`、`CROSS JOIN`、`LEFT` / `RIGHT` / `FULL [OUTER] JOIN ... ON / USING (...)`、
  `NATURAL ... JOIN`、带括号的连接（外连接右侧的除外），任意多张表；FROM 里可以用子查询、视图和 CTE。
- 子查询：标量子查询、`[NOT] IN (SELECT ...)`、`[NOT] EXISTS (...)`，可以嵌套、可以引用外层查询的
  列和聚合（`(SELECT count(t.a) FROM u)` 属于外层查询），能出现在各子句和 INSERT/UPDATE/DELETE 里。
- 复合查询：`UNION [ALL]`、`INTERSECT`、`EXCEPT`，带整体的 `ORDER BY` / `LIMIT`。
- 窗口函数：`row_number`、`rank`、`dense_rank`、`percent_rank`、`cume_dist`、`ntile`、`lag`、`lead`、
  `first_value`、`last_value`、`nth_value`，以及全部聚合函数 `OVER (PARTITION BY ... ORDER BY ...
  {ROWS | RANGE | GROUPS} BETWEEN ... [EXCLUDE ...])`、`WINDOW` 子句；聚合的 `FILTER (WHERE ...)`。
- 表达式：比较、`AND`/`OR`/`NOT`（三值逻辑）、`+ - * / %`、`||`、`->` / `->>`、位运算 `& | ~ << >>`、`IS [NOT]`、
  `[NOT] IN (...)`、`[NOT] BETWEEN`、`[NOT] LIKE / GLOB ... [ESCAPE]`、`CASE`、`CAST(x AS type)`、
  BLOB 字面量 `x'..'`、十六进制整数、参数 `?` / `?NNN` / `:name`。
- 函数：SQLite 的核心标量函数（`substr`、`replace`、`trim`、`instr`、`printf`/`format`、`round`、
  `hex`/`unhex`、`quote`、`char`/`unicode`、`concat`、`iif` ……）、数学函数、日期时间函数
  （`date`、`time`、`datetime`、`julianday`、`unixepoch`、`strftime`、`timediff`，全部修饰符）；
  聚合 `count`、`sum`、`avg`、`min`、`max`、`total`、`group_concat`、`string_agg`（支持 `DISTINCT`）。
- JSON：`json`、`json_valid`、`json_type`、`json_extract` 与 `->` / `->>`、`json_array` / `json_object`、
  `json_insert` / `json_replace` / `json_set` / `json_array_insert` / `json_remove` / `json_patch`、`json_quote`、`json_array_length`、
  `json_pretty`、`json_error_position`，对应的 `jsonb_*` 二进制版本（与 SQLite 的 JSONB 字节相同），聚合
  `json_group_array` / `json_group_object`（也可作窗口函数），表值函数 `json_each` / `json_tree`；支持 JSON5 输入。
- 索引：`CREATE [UNIQUE] INDEX [IF NOT EXISTS]`、`DROP INDEX [IF EXISTS]`，UNIQUE 列自动建索引；
  执行器对“索引列前缀等值 + 下一列范围”使用索引，也用于连接的内层表。
  `EXPLAIN [QUERY PLAN]` 显示每张表的访问路径。
- 事务：`BEGIN [DEFERRED|IMMEDIATE|EXCLUSIVE]`、`COMMIT`/`END`、`ROLLBACK`；不在事务中时每条语句
  自动提交；每条语句都是原子的（多行 `INSERT` 中途违反约束，整条语句不生效）。
- 并发（WAL 模式）：多个连接、多个进程可以同时打开同一个文件；读者按快照读，不阻塞写者，写者也
  不等读者；同一时刻一个写者，拿不到写锁时忙等到超时报 `database is locked`。读者标记让 checkpoint
  在有读者时也能拷贝到最老快照为止，拷完后写者从头重用日志，持续有读者时日志也不会无限增长。
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

### SQLite 文件格式

MiniDB 也能直接读写 SQLite 自己的数据库文件——sqlite3 建的文件 MiniDB 可以打开，MiniDB 建的 sqlite3 也可以：

```sh
python -m minidb --sqlite app.sqlite                 # 新建的文件用 SQLite 格式；已有文件按文件头自动识别
```

```python
db = minidb.Database("app.sqlite", format="sqlite")   # 或 minidb.connect("app.sqlite", format="sqlite")
```

和 Python 的 sqlite3 一样有 `serialize()` / `deserialize(data)`：把 SQLite 格式的库取成文件字节（含未提交的改动），
或让连接换成一个从这些字节开始的内存库（原来的文件不动）。

用的是 SQLite 的 rollback journal（`-journal`，崩溃后 sqlite3 和 MiniDB 都能回放对方留下的日志）和 SQLite 的文件锁，
所以另一个进程里的 sqlite3 可以同时打开同一个文件。ANALYZE 写 `sqlite_stat1`，VACUUM 照 SQLite。页大小 512–65536
（`PRAGMA page_size` 对新库立即生效、对已有的库在下一次 VACUUM 时生效）；只支持 UTF-8、非 WAL 模式、无 auto_vacuum
（其他文件会明确拒绝并说明怎样用 sqlite3 转换）；SQLite 写下而 MiniDB 不支持的
对象（表达式索引、部分索引、`WITHOUT ROWID` 表等）原样保留，用到时报 `NotSupportedError`。设计见 DECISIONS.md 的 D100。
SQLite 格式下查询与 MiniDB 格式相差 10–45%，逐行插入慢 1.6–2.4 倍（benchmark 见 PROGRESS.md 阶段 20）。

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
  │  jsonb.py       JSON 文本 ⇄ JSONB 的解析、渲染与路径编辑（照 SQLite 的 json.c）
  │  jsonfuncs.py   JSON 标量/聚合函数与 json_each / json_tree
  │  catalog.py     schema（表、索引）存在第 1 页的 B+ 树里，打开时重建
  ▼
  │  btree.py       B+ 树：按字节大小分裂/合并/借位，叶子兄弟链，范围扫描，overflow 页
  ▼
  │  pager.py       4 KB 页（带 CRC32）的读写与缓存，空闲页链表，语句级 journal，WAL 提交与恢复
  │  locking.py     跨进程字节锁（fcntl / LockFileEx，进程内共享一个文件描述符）与忙等超时
  │  sqlite_*.py    SQLite 文件格式：字节布局、B 树、rollback journal 与 SQLite 的锁
  ▼
数据库文件 app.db + 预写日志 app.db-wal + 读者标记与锁 app.db-shm
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
- **WAL 模式**：提交向 `-wal` 追加页帧（最后一帧为提交帧）并 fsync；读者取“最后一个提交帧”为快照，
  并在 `-shm` 的读槽里登记（读者标记）；checkpoint 只把日志拷到最老的读者标记为止。日志全部拷完后，
  写者开一个新 generation 从头覆盖日志；读者总是跨越提交时写者最多等 0.1 秒让旧读者结束。
  大事务的脏页可提前作为未提交帧写入日志，回滚时截断。

## Playground

`playground/` 是一个静态页面：`worker.js` 在 Web Worker（ES module）里加载 Pyodide 314（CDN），
解压 `minidb.zip`（`minidb` 包 + `bridge.py`），之后页面通过 `bridge.py` 的 `run` / `objects` / `tree`
拿 JSON 结果；打开的 SQLite 文件经 `deserialize()` 成为内存库，`file_map` / `page` 从页面的原始字节画出文件和单页的布局，
`export` 用 `serialize()` 交回文件。本地构建与预览：

```sh
python tools/build_playground.py && python -m http.server -d site    # http://localhost:8000
```

`.github/workflows/pages.yml` 在推送到 master 时构建并部署到 GitHub Pages。

## 测试

```sh
eval "$(.venv/bin/python tools/reference_sqlite.py)"              # 编译并启用参考 SQLite（见下）
.venv/bin/python -m pytest                                        # 全部测试（860+ 个）
.venv/bin/python tests/fuzz.py --seeds 0-999 --statements 600     # 大规模模糊对照
.venv/bin/python tests/metamorphic.py --seeds 0-399 --queries 300 # 变形测试（不需要 sqlite3）
.venv/bin/python tools/sqllogictest.py --fetch                    # 下载 SQLite 官方 sqllogictest 语料
.venv/bin/python tools/sqllogictest.py --jobs 8                   # 跑语料，输出通过率与失败根因
.venv/bin/python tests/benchmark.py --rows 100000                 # 性能测试
.venv/bin/python tools/coverage.py                                # 行覆盖率（标准库 trace）
```

GitHub Actions 在 Linux 上编译参考 SQLite，用 Python 3.11–3.14 跑全部测试（警告视为错误），并跑三段 fuzz：
固定种子、数据库文件模式、以及每次运行都换一批的新种子（每周定时运行一次）；另外跑变形测试和
sqllogictest 全量语料（通过数低于基线即失败）。`windows-latest` 上跑存储、并发与崩溃测试（Python 3.13，不跑与参考 SQLite 的版本对照）。

- **与 sqlite3 对照**（`tests/sqlcompare.py`）：同一条 SQL 在 MiniDB 和 sqlite3 上执行，要求都成功
  且结果相同（区分 1 和 1.0），或者都失败且异常类别相同，部分用例逐字比较报错。
- **参考 SQLite**：标准答案是 sqlite.org 发布的 SQLite 3.53.4、默认编译选项。各发行版给 Python
  链接的 SQLite 版本和选项不同（例如 conda-forge 开了 ICU，`upper('é')` 会变成 `'É'`），
  `tools/reference_sqlite.py` 下载源码（校验 SHA3-256）、编译，并输出让 `sqlite3` 模块加载它的
  环境变量；链接的 SQLite 带 ICU 时对照测试直接报错。需要 C 编译器。
- **模糊测试**（`tests/fuzz.py`）：随机 schema（约束、单列/多列/唯一索引）+ 随机增删改查、
  嵌套表达式、聚合、连接、事务、建删索引、VACUUM，每个种子结束时做 `integrity_check`；文件模式下
  另有一个连接持有读快照，主连接频繁 checkpoint、重启日志，快照必须始终不变。
- **sqllogictest**（`tools/sqllogictest.py`）：SQLite 官方的引擎无关测试集，约 594 万条记录。
  语料按固定版本下载、逐文件校验 SHA3-256，不入库；报告按每个文件的第一个失败归类根因（缺功能时
  后面的记录会连锁失败），错误结果和崩溃单独列出。当前通过率见 PROGRESS.md。
- **变形测试**（`tests/metamorphic.py`）：SQLancer 的 TLP（WHERE / DISTINCT / 聚合 / HAVING 按
  `p`、`NOT p`、`p IS NULL` 三分）和 NoREC（优化器可利用的 WHERE 与逐行求值的 CASE 比较），
  不依赖 sqlite3，专找优化器 bug；条件里用表中实际存在的值，打在 rowid 和索引范围的边界上。
- **B+ 树**：上万次随机插入删除后校验不变量（有序、分隔键边界、同深度、填充率、兄弟链）。
- **类型注解**：`tests/test_annotations.py` 要求每个函数和方法的参数与返回值都有注解，
  并用 `typing.get_type_hints` 解析一遍（写错的名字会失败）。
- **覆盖率**：`tools/coverage.py` 只用标准库 `trace` + `ast` 统计语句覆盖率，目前 99.2%；
  没覆盖的主要是防御性分支（Windows 无 `fcntl`、不可能的内部状态）。
- **崩溃恢复**：在提交的每一步（写 WAL 帧、写提交帧、fsync 日志）和 checkpoint 的每一步
  （拷页、fsync、截断日志）模拟崩溃，包括子进程里真实的 `os._exit`，以及截断/损坏的 WAL，
  重开后数据必须是事务前或事务后的完整状态。`tests/test_crash_model.py` 模拟断电：未 fsync 的写入
  可能丢失、乱序或按 512 字节扇区撕裂，截断也可能丢失，重开后必须完整且是已确认的提交或进行中的那个。

## 性能

100,000 行（`id, name, age, city`），数据库文件，Apple Silicon，Python 3.12，与 sqlite3 同样的 SQL
（`tests/benchmark.py`）：

| 操作 | MiniDB | sqlite3 |
|---|---:|---:|
| 插入 10 万行，每行一条 INSERT（`?` 参数），一个事务 | 0.98 s | 0.07 s |
| 插入 10 万行，每行一条 INSERT（拼字面量），一个事务 | 2.7 s | 0.19 s |
| 自动提交插入 1000 行（每行一次 commit + fsync） | 0.14 s | 0.2–0.4 s |
| 1 万次主键点查（`?` 参数） | 0.11 s | 0.04 s |
| 全表扫描 `count(*) WHERE age > 50` | 0.06 s | 0.002 s |
| 全表扫描 `SELECT *` | 0.07 s | 0.04 s |
| `GROUP BY city` 三个聚合 | 0.09 s | 0.03 s |
| `ORDER BY age, name LIMIT 10` | 0.08 s | 0.003 s |
| `CREATE INDEX` on age | 0.39 s | 0.02 s |
| 10 万行与小表连接（每行一次索引查找） | 0.15 s | 0.006 s |
| 10 万行与 200 行无索引表的等值连接（自动哈希） | 0.12 s | 0.01 s |
| 数据库文件大小 | 14.8 MB | 9.3 MB |

纯 Python 实现，点查和提交接近 SQLite，扫描/聚合慢 3–25 倍（逐行求值的开销；运算符、循环和聚合已生成
Python 源码编译执行，见 DECISIONS.md D90）。
反复执行的语句请用 `?` 参数：同一条 SQL 只解析和编译一次。详细历史见 [PROGRESS.md](PROGRESS.md)。

## 已知限制

- 不支持临时表和 TEMP 触发器、虚表（表值函数只有 `pragma_xxx()`、`json_each()` / `json_tree()`）、`WITHOUT ROWID`、`STRICT`、生成列、
  表达式索引和部分索引、`UPDATE ... FROM`；`localtime` 修饰符只在一个时区的机器上对照过。
- 大小写转换和比较只认 ASCII 字母（与不带 ICU 扩展的 SQLite 相同）：`upper('é')` 仍是 `'é'`。
- 当 SQLite 的结果取决于它的查询计划时（相等的 1 和 1.0 中 DISTINCT/GROUP BY 保留哪一个、
  多行 UPDATE 先处理哪一行导致 UNIQUE 冲突、聚合查询里裸列取自哪一行、常量传播 / 常量折叠
  决定的出错时机），MiniDB 不保证选择相同。
- 一个始终不结束的读事务仍会让日志变长（写者等 0.1 秒后放弃，之后日志每增长 4000 帧才再等一次）。
- Windows 上用 `LockFileEx`（ctypes）加锁，与 SQLite 的 win32 VFS 相同，可以和 sqlite3 进程共享读锁。CI 在
  `windows-latest` 上只跑存储、并发、崩溃与 SQLite 文件格式这部分测试，其余（与参考 SQLite 的对照）只在 Linux 和 macOS 上跑。
- 删除空闲页不会自动收缩文件，需要 `VACUUM`；目录里会保留（可能为空的）`-wal` 与 `-shm` 文件。
  连接不能跨 `fork()` 使用。
- 与 sqlite3 对照时，Python 3.11 的 sqlite3 没有 `autocommit` 参数，两组事务行为对照测试在
  3.11 上跳过（MiniDB 自身行为不随 Python 版本变化）。
- 优化器是启发式代价模型，不支持 LEFT JOIN 的重排；只有第一张表的升序 ORDER BY 能利用
  索引/rowid 顺序免排序。

## License

[MIT](LICENSE)
