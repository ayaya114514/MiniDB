# 进度日志

## 阶段 1：骨架（完成）
- 项目结构：`minidb/`（`parser.py`、`executor.py`、`repl.py`、`__main__.py`）、`tests/`、`pyproject.toml`（pytest 配置）、git 仓库。
- 固定单表 `users(id, name, age)` 存在内存，支持 `insert <id> <name> <age>` 和 `select`；重复 id 报错。
- REPL：`python -m minidb`，`.exit` 退出，未知元命令和语句错误都会报 `Error:` 后继续。
- 测试：16 个，全部通过（含与 sqlite3 的对照测试、命令行入口测试）。
- 已知问题：数据只在内存里；语法是临时的，阶段 4 替换成 SQL。

## 阶段 2：存储层（完成）
- `pager.py`：4096 字节固定页，页对象缓存 + dirty 集合，关闭时写回；文件头（magic、页数、空闲链表）；页分配/释放复用空闲页；`None` 路径为内存库。
- `record.py`：行的序列化与反序列化（NULL/INTEGER/REAL/TEXT）。
- users 表改为存储在页链上（阶段 3 会换成 B+ 树）；`python -m minidb file.db` 打开持久化数据库。
- 测试：38 个，全部通过（含关闭重开后数据完整、跨多页 3000 行、坏文件拒绝打开）。
- 已知问题：单行不能超过一页；重复 id 检查是全表扫描（阶段 3 由 B+ 树解决）。

## 阶段 3：B+ 树（完成）
- `btree.py`：叶子/内部节点的页布局；查找、插入、叶子/内部/根分裂（任意层数）；叶子兄弟指针与范围扫描（开/闭区间）；删除含合并与借位、根收缩；overflow 页存大值；`destroy`/`clear` 回收页；`check()` 校验不变量（有序、分隔键边界、叶子同深度、节点大小上下限、孩子数、兄弟链）；`dump()` 输出树结构。
- users 表改存 B+ 树（根页 1，key = id），新增 `delete <id>`，REPL 支持 `.btree`。
- 测试：70 个，全部通过。包括：升序/降序/随机插入 1 万条（容量 128/256/4096，最深 5 层以上）；随机插入 1.2 万再删除 1 万条并定期校验不变量；3 万步随机增删查与 dict 模型对照；随机范围扫描；删除全部后页全部回收；overflow 值；重开后结构完整；2 万步随机增删与 sqlite3 对照（中途关闭重开）。
- 已知问题：key 不支持 overflow（超长索引 key 会报错）；数据库文件不会收缩。

## 阶段 4：SQL 解析器（完成）
- `tokenizer.py`：关键字（大小写不敏感）、标识符（含 `"x"`、`` `x` ``、`[x]` 引号形式）、整数/浮点数（超出 64 位的整数转 REAL）、字符串（`''` 转义）、运算符、`--` 和 `/* */` 注释；错误带行列号。
- `parser.py`：语法树 dataclass；`CREATE TABLE [IF NOT EXISTS]`、`DROP TABLE [IF EXISTS]`、`INSERT`（多行 VALUES、指定列）、`SELECT`（`*`、`t.*`、别名、`DISTINCT`、无 FROM）、`UPDATE`、`DELETE`；表达式含比较、`AND/OR/NOT`、括号、算术、`||`、`IS [NOT]`、`[NOT] IN`、`[NOT] LIKE`、`[NOT] BETWEEN`、函数调用（含 `COUNT(*)`、`DISTINCT`）；多语句脚本。
- 测试：123 个，全部通过（新增 tokenizer 14 个、parser 39 个，覆盖优先级、各语句的语法树和带位置的报错）。
- 已知问题：REPL 仍然走临时命令语法，阶段 5 接上 SQL 执行器。

## 阶段 5：多表与执行器（完成）
- `catalog.py`：schema 存在第 1 页的 B+ 树里（type, name, tbl_name, rootpage, sql），重开时重新解析 SQL；任意多张表；`CREATE TABLE [IF NOT EXISTS]` / `DROP TABLE [IF EXISTS]`（释放整棵树的页）。
- `executor.py`：表达式编译成闭包；`INSERT`（多行、指定列、亲和性、自动 rowid）、`SELECT`（`*`、`t.*`、别名、`DISTINCT`、无 FROM）、`UPDATE`（含修改主键）、`DELETE`；约束 PRIMARY KEY / NOT NULL / UNIQUE / datatype mismatch；主键等值、`IN`、范围条件走 B+ 树（`EXPLAIN` 可查看），其他全表扫描。
- `values.py`：SQLite 值语义与标量函数（abs/length/lower/upper/coalesce/ifnull/nullif/typeof/多参 min/max）。
- `database.py`：`Database(path).execute(sql)`，语句级原子性 + 自动提交。Shell 改为真正的 SQL（多行语句、带位置和 `^` 的报错、元命令）。
- 测试：192 个，全部通过。与 sqlite3 对照：17 种二元运算 × 19 种字面量两两组合（含 typeof）、一元运算与函数、3000 个随机优先级表达式、LIKE 模式、列亲和性存储与比较、CRUD 流程、约束错误、语句原子性、语义错误（部分核对报错文本）、rowid 各种访问路径、1500 步随机 CRUD。另有规划器测试（冷缓存下点查只读 ≤4 页）。
- 已知问题：REAL→TEXT 在少数值上与 sqlite 末位数字不同（见 D20）；UNIQUE 检查暂为全表扫描（阶段 6 改索引）。

## 阶段 6：查询功能扩展（完成）
- `ORDER BY`（多列、ASC/DESC、NULLS FIRST/LAST、列序号、别名）、`LIMIT`/`OFFSET`（含 `LIMIT a, b`）。
- 聚合：COUNT(*)/COUNT/SUM/AVG/MIN/MAX/TOTAL/GROUP_CONCAT、DISTINCT 聚合、`GROUP BY`（表达式/序号/别名）、`HAVING`；SUM/AVG 移植 sqlite 的补偿求和，浮点结果逐位一致。
- 二级索引：`CREATE [UNIQUE] INDEX [IF NOT EXISTS]`、`DROP INDEX [IF EXISTS]`；UNIQUE/非整数主键自动建索引；增删改自动维护；等值前缀 + 范围走索引；`EXPLAIN` 显示所用索引；`.schema` 显示索引。
- 连接：`,`/`JOIN`/`CROSS JOIN`/`LEFT JOIN`，任意多表嵌套循环，谓词下推，内层表用 rowid/索引查找。
- `Database.integrity_check()`。
- 测试：236 个，全部通过。新增对照测试覆盖 ORDER BY/LIMIT 组合、每列全部聚合、随机浮点求和、GROUP BY/HAVING、类型相等分组、18 种连接查询、600 条随机查询；索引测试含 2500 步随机增删改（三个索引 + UNIQUE 列，定期 integrity_check）、类型混合的索引查询、索引错误信息逐字比对、持久化与删表回收。
- 已知问题：不做连接重排和基于索引的排序；索引 key 长度受限（约 512 字节）。

## 阶段 7：事务与崩溃恢复（完成）
- `BEGIN [DEFERRED|IMMEDIATE|EXCLUSIVE] [TRANSACTION]`、`COMMIT`/`END`、`ROLLBACK`；自动提交；事务内语句失败只回滚该语句；关闭时回滚未提交事务。
- `pager.py`：WAL 重做日志（页镜像 + CRC32 提交记录 + fsync 顺序），启动时恢复/丢弃；无缓冲文件 I/O；提交各步骤的崩溃钩子。
- 测试：270 个，全部通过。新增：事务语句与 sqlite 逐字对照报错；3000 步随机事务（BEGIN/COMMIT/ROLLBACK 混合增删改）与 sqlite 对照并重开校验；大事务（页分裂、新表、overflow、删索引）回滚后页数与空闲页完全复原；未提交数据绝不进文件；9 个崩溃点 × 显式事务/自动提交语句；WAL 截断/翻转字节后被丢弃；恢复幂等；新库首次提交崩溃；6 个崩溃点的真实子进程 `os._exit` 崩溃。另做了变异测试：把“先写 WAL 再写数据页”顺序颠倒后崩溃测试失败。
- 已知问题：大事务的脏页全部驻留内存；没有并发控制（单连接）；未 fsync 目录项（删除 WAL 的持久性依赖文件系统）。

## 阶段 8：收尾（完成）
- **模糊对照测试**（`tests/fuzz.py`）：随机 schema（约束、单列/多列/唯一索引，最多 3 张表）+ 随机 INSERT/UPDATE/DELETE/SELECT（嵌套表达式、LIKE/IN/BETWEEN、函数、聚合与 GROUP BY/HAVING、两表 JOIN/LEFT JOIN、DISTINCT、ORDER BY/LIMIT）、BEGIN/COMMIT/ROLLBACK、建删索引；每个种子最后 `integrity_check`。
  - 第一轮 50 种子 × 300 语句有 23 个种子不一致，修正了：ORDER BY/GROUP BY 的常量列序号规则（含 SQLite 解析器的 `IS NULL`、`X AND 0` 折叠）、rowid 到达上限后的随机分配、标量 min/max 的相等值取舍；另修了对照框架两处比较口径问题（见 D39）。
  - 修正后累计跑过：600×600、400×800、150×500（文件模式）、最终一轮 1000×500 + 200×500（文件模式），共约 136 万条语句。其中 400×800 一轮出现 1 个不一致，查明是对照框架在“数字按值比较”模式下多重集排序仍带类型导致的误报，修正框架后该种子通过；最终两轮 1200 个种子 0 不一致。
  - `tests/test_fuzz.py` 在常规测试中跑 30 个种子。
- **性能测试**（`tests/benchmark.py`，Apple Silicon，Python 3.12，数据库文件，同样的 SQL 对比 sqlite3）：

| 操作（100,000 行） | MiniDB | sqlite3 | 倍数 |
|---|---:|---:|---:|
| 插入 10 万行，每行一条 INSERT，一个事务 | 3.532 s | 0.251 s | 14x |
| 插入 10 万行，每条 INSERT 1000 行，一个事务 | 2.209 s | 0.076 s | 29x |
| 自动提交插入 1000 行（每行 commit + fsync） | 0.177 s | 0.214 s | 1x |
| 1 万次主键点查 | 0.333 s | 0.068 s | 5x |
| 100 次主键范围扫描（每次 1000 行） | 0.222 s | 0.023 s | 10x |
| 全表扫描 `count(*) WHERE age > 50` | 0.152 s | 0.003 s | 57x |
| 全表扫描 `SELECT *` | 0.159 s | 0.039 s | 4x |
| `GROUP BY city` + 3 个聚合 | 0.202 s | 0.032 s | 6x |
| `ORDER BY age, name LIMIT 10` | 0.246 s | 0.004 s | 66x |
| `CREATE INDEX` on age | 0.736 s | 0.019 s | 38x |
| 73 次索引等值查询（每次约 1400 行） | 0.283 s | 0.002 s | 151x |
| 10 万行与小表 JOIN（每行一次索引查找） | 0.361 s | 0.007 s | 53x |
| 重新打开并点查一次 | 0.000 s | 0.000 s | - |
| 数据库文件大小 | 17.1 MB | 6.6 MB | 2.6x |

  顺序插入改为不均匀分裂（D38）之前文件是 23.5 MB，插入慢约 10%。
- **README.md**：功能、架构、使用方法、测试、性能、已知限制。
- 测试：302 个，全部通过。
- 已知问题：见 README“已知限制”。

## 阶段 9：API（完成）
- 参数绑定：`?`、`?NNN`、`:name`、`@name`、`$name`，序列按位置、映射按名字；编号、报错信息与 sqlite3 一致；绑定值不做常量折叠。
- `minidb.connect()` / `Connection` / `Cursor`：PEP 249 的模块属性、异常层次、`execute`/`executemany`/`executescript`/`fetchone`/`fetchmany`/`fetchall`/迭代、`rowcount`/`lastrowid`/`description`、`commit`/`rollback`/`close`、`with conn:`，事务语义照 Python 3.12 sqlite3 的 `autocommit`。
- 语句缓存：按 SQL 文本缓存解析结果（LRU 256 条）。
- fuzzer 以 15% 概率把字面量换成 `?` 参数。
- 测试：336 个，全部通过；400 种子 × 500 语句的 fuzz（含参数绑定）0 不一致。新增 `test_dbapi.py`（34 个）：同一段 Python 代码分别对 sqlite3 模块和 minidb 执行，比较观察到的一切（行、rowcount、lastrowid、description、异常类别与原文、事务状态）。
- 性能（10 万行）：逐条 INSERT 用 `?` 参数 1.93 s，拼 SQL 字面量 3.47 s（跳过解析，快 1.8 倍）；1 万次主键点查用参数 0.28 s，字面量 0.37 s。
- 已知问题：同一文件的多个连接之间不同步缓存（D44，阶段 10 解决）；`bind()` 每次执行复制语法树，还有优化空间。

## 阶段 10：健壮性（完成）
- `locking.py`：SHARED / RESERVED / EXCLUSIVE 三种锁（flock），忙等超时报 `database is locked`，SQLite 式死锁规避，`BEGIN IMMEDIATE/EXCLUSIVE`；崩溃留下的 WAL 由后续任意连接在锁保护下恢复。
- 变更计数器：别的连接/进程提交后，本连接下一个事务开始时自动丢弃过期缓存、重载 schema（修复阶段 9 记录的 D44）。
- 每页 CRC32 校验，文件格式升到 2；损坏的页、截断的文件、非数据库文件都报 `DatabaseError`；`integrity_check()` 校验所有已提交页。
- WAL 创建/删除与新建数据库后 fsync 目录。
- 测试：508 个，全部通过。新增 `test_concurrency.py`（同进程多连接可见性与 schema 同步、单写者、提交等读者并可重试、死锁快速失败、BEGIN IMMEDIATE、存活连接替崩溃连接恢复、4 个写进程 × 60 轮 + 1 个并发读进程校验“计数不丢失、转账总额恒定、只看到完整提交”、死进程的锁自动释放）；`test_corruption.py`（150 组随机字节翻转：全部被查询或 integrity_check 发现，数据要么完全正确要么报 DatabaseError；截断、整页清零、空闲页损坏、非数据库文件、旧格式）。
- 变异测试：去掉锁后 5 个并发测试失败（含多进程丢更新）；去掉校验和后约 60 个损坏用例失败（错误数据或 TypeError/IndexError/UnicodeDecodeError 泄漏）。
- fuzz：200 种子 × 500（文件模式）+ 300 种子 × 500，0 不一致。
- 性能（10 万行）：主键点查 0.37 → 0.47 s（每语句 flock + 读头页），自动提交 1000 行 0.18 → 0.29 s（目录 fsync），其余基本不变。
- 已知问题：读者会阻塞写者提交（阶段 13 的 WAL 模式解决）；Windows 上没有 fcntl，不加锁。

## 阶段 11：SQL 扩展（完成）
- `CASE`（简单/搜索）、`CAST`（任意类型名按 SQLite 亲和性规则）；标量子查询、`[NOT] IN (SELECT ...)`、`[NOT] EXISTS`，任意嵌套、可相关（引用外层查询的列），可用于 SELECT/WHERE/HAVING/ORDER BY/LIMIT/INSERT/UPDATE/DELETE；`UNION [ALL]`、`INTERSECT`、`EXCEPT`（带整体 ORDER BY/LIMIT）；`JOIN ... USING`、`NATURAL [LEFT] JOIN`；FROM 子查询（derived table）；`SELECT ALL`。
- 执行器重构：SELECT 编译一次、`run()` 多次；作用域父链支持相关子查询；不相关子查询只算一次。
- fuzzer 新增：CASE、CAST、三种子查询（半数相关）、复合查询、USING/NATURAL、FROM 子查询（60 个种子的统计里每种都有数十到上千条成功执行）。
- 测试：529 个，全部通过。（提交阶段 11 时多进程压力测试间歇失败：原因是测试的不变量写错了——自动提交轮次里“计数 +1”和“写日志”是两次独立提交，读者可以合法地看到计数领先一步；已改为允许每个写进程领先至多一步，最终相等的检查不变，并确认去掉锁的变异版本仍然失败。）新增 `test_compare_subqueries.py`（20 种值 × 15 种类型名的 CAST 矩阵、CASE、标量/IN/EXISTS 子查询（含相关、嵌套、聚合中的相关子查询）、增删改里的子查询、复合查询、USING/NATURAL/derived table、错误信息逐字比对）；parser 新增 12 个测试。
- fuzz：600 种子 × 500 + 150 种子 × 500（文件模式），0 不一致。
- 已知问题：每条语句都要重新编译查询计划（点查比阶段 10 慢约 10%，阶段 12 处理）；外层聚合函数出现在子查询里（如 `(SELECT ... WHERE x = max(outer.col))`）不支持（报错）。

## 阶段 12：性能（完成）
- 预编译计划缓存（参数读数组、按 schema 版本失效）；ORDER BY 单次复合键排序 + LIMIT top-k；访问路径顺序与 ORDER BY 吻合时免排序并提前停止，必要时按索引顺序扫描；覆盖索引；B+ 树 key 溢出页（索引 key 不再有长度上限）；记录格式 3（紧凑类型码 + 按头部缓存的 struct 解码）；修掉语句 journal 页副本重算 size 的老热点；`encoded_size()` 避免为量尺寸而编码。
- 按需解码列未单独实现，理由见 D58。
- 测试：547 个，全部通过。新增：预编译计划复用/失效/子查询缓存重置（含变异测试）；预排序计划与 sqlite 对照（>50 个查询确认走免排序）；覆盖索引；长 key 的 B+ 树随机增删（3 种容量，含链的归属变异测试）、持久化与语句回滚；记录格式边界值与 3000 组随机往返；长索引 key 的 UNIQUE/范围查询与 sqlite 对照。
- fuzz：400 种子 × 500 + 150 种子 × 500（文件模式）。发现 1 处不一致，是已知的 D20（sqlite 的浮点转文本近似、且不能往返），fuzz 宽松模式改为按 13 位有效数字比较这类文本后通过。
- 更正：提交 6447cd3 的说明里写“约 1.5 s”是 profiler 下的时间，不带 profiler 实测为 0.39 s（3 万行带参数插入 + 建索引）。
- 性能（10 万行，阶段 11 → 阶段 12）：

| 操作 | 阶段 11 | 阶段 12 | sqlite3 |
|---|---:|---:|---:|
| 逐条 INSERT（字面量），一个事务 | 3.69 s | 3.18 s | 0.25 s |
| 逐条 INSERT（? 参数），一个事务 | 2.07 s | 0.94 s | 0.08 s |
| 1 万次主键点查（? 参数） | 0.44 s | 0.12 s | 0.04 s |
| 全表扫描 `count(*) WHERE age > 50` | 0.16 s | 0.12 s | 0.003 s |
| 全表扫描 `SELECT *` | 0.16 s | 0.11 s | 0.04 s |
| `GROUP BY city` + 3 个聚合 | 0.21 s | 0.16 s | 0.03 s |
| `ORDER BY age, name LIMIT 10` | 0.24 s | 0.13 s | 0.004 s |
| `CREATE INDEX` on age | 0.69 s | 0.65 s | 0.02 s |
| 73 次索引等值查询（覆盖索引） | 0.29 s | 0.14 s | 0.002 s |
| 10 万行与小表 JOIN | 0.37 s | 0.26 s | 0.006 s |
| 数据库文件大小 | 23.6 MB | 15.7 MB | 9.3 MB |

- 已知问题：字面量 SQL（不用参数）每条都要解析，仍是插入的主要开销；逐行 Python 闭包求值是扫描的主要开销。
