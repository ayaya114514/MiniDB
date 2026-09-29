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

## 阶段 13：内核（完成）
- WAL 模式：提交只追加校验过的页帧并 fsync 日志；读者按快照读、不阻塞写者；显式事务的快照过期时不能开始写；大事务溢出为未提交帧（内存有界）、回滚截断；无读者时非阻塞 checkpoint（提交后日志达 1000 帧、关闭时）。
- 优化器：`ANALYZE`（统计存 schema、随表/索引删除）；基于代价的访问路径选择；`IN` 列表与 OR 走多路索引并集；内连接按代价重排（≤6 表穷举）。
- 与 SQLite 求值时机对齐：多行 INSERT 先算后插；`X AND 0` 折叠后不解析操作数。
- 测试：554 个，全部通过。崩溃测试改写为 WAL 语义：提交各步骤崩溃、checkpoint 各步骤崩溃（不丢已提交数据）、日志截断/损坏后状态必为某次提交后的完整状态（覆盖 ≥4 种不同的提交点）、checkpoint 幂等、崩溃写者的未提交帧被忽略并被截断、真实子进程 `os._exit`；并发测试新增：读者快照不受并发提交影响、checkpoint 不等待读者、过期快照不能写、大事务溢出且内存有界、自动 checkpoint；优化器测试：ANALYZE 存取与持久化、统计改变计划、IN/OR 计划与 300 组随机 OR 查询对照 sqlite、连接重排与 120 组重排后的连接对照 sqlite。
- 变异测试：把“无提交帧的帧也算数”、“checkpoint 不拿 EXCLUSIVE”、“过期快照也能写”三个变异都被测试发现；“不截断崩溃写者的残帧”起初存活——分析后确认正确性不依赖截断（链式校验和使残帧失效），补充断言“残帧不留在文件里”后该变异也被发现。
- fuzz（含 ANALYZE 语句）：500 种子 × 500 + 200 种子 × 500（文件模式）两轮。第一轮发现 2 处不一致：一处是计划相关（UPDATE 的 SET 子查询读同一张表、SQLite 按索引顺序处理行），fuzz 改为此类 UPDATE 只作用一行；一处是真实差异（`0 AND (子查询引用不存在的表)` SQLite 不报错），已修复。第二轮 0 不一致。
- 性能（10 万行，同样取 5 次最好成绩对比阶段 12）：带参数主键点查 0.117 → 0.104 s；拼字面量点查 0.533 → 0.557 s（每条语句都做代价规划，慢约 4%）；自动提交写入 1000 行 0.28 → 0.12 s（每次提交只 fsync 日志，比 sqlite3 默认的回滚日志模式还快）。
- 已知问题：持续有读者时 checkpoint 一直做不成、日志变长（没有 SQLite 那种基于共享内存读者标记的部分 checkpoint）；目录里会保留大小为 0 的 `-wal` 和 `-lock` 文件。

## 阶段 14：工程化（完成）
- 类型注解：所有模块级函数和方法（143 个在 executor 里）的参数与返回值都有注解；共享别名 `SQLValue`/`Row`/`RowFunction` 等和 `Page`/`KeyCodec`/`AccessPath` 三个 Protocol。`tests/test_annotations.py` 用 `typing.get_type_hints` 解析全部注解（13 个模块）。
- 覆盖率（`tools/coverage.py`，标准库 `trace` + `ast`）：98.9% → 99.2%（4524 条语句，未覆盖 36 条）。修正了工具的一个 bug（`trace` 按模块短名缓存忽略判断，导致所有 `__init__.py` 被跳过）。新增进程内的 CLI 入口测试（子进程里执行的代码不计入覆盖率）和缺参数的对照测试。剩余未覆盖：`__main__.py`（只在子进程测试里运行）；`locking.py` 的 Windows 无 `fcntl` 分支和目录 fsync 的 OSError 分支；其余是防御性分支（不可能的内部状态、未知语句类型、页读取不足等）。
- 多 Python 版本：本地用 mamba 建了 3.11/3.13/3.14 环境逐一跑全部测试。发现并处理：3.11 的 sqlite3 没有 `autocommit` 参数（对照改用 `isolation_level=None`，两组事务对照在 3.11 跳过）；3.14 的 sqlite3 对“命名占位符 + 序列参数”报错（MiniDB 改为同样报错，D68）；3.13+ 的 sqlite3 对未关闭连接发 ResourceWarning。
- 测试把警告视为错误（`filterwarnings = ["error"]`）：修掉测试里遗留的未关闭文件/连接（崩溃测试释放崩溃连接的文件；`Pair` 由 autouse fixture 统一关闭）。
- 第一次 CI 运行失败（Ubuntu 的 SQLite 3.45.1），追查发现本地的标准答案不标准：conda-forge 的 SQLite 开了 ICU、改了参数编号上限，MiniDB 一直对齐的是这些非默认行为（D70）。改为用 `tools/reference_sqlite.py` 编译 sqlite.org 的 SQLite 3.53.4（默认选项）做标准答案，本地和 CI 相同；MiniDB 的大小写规则改为只认 ASCII（`upper`/`lower`/`LIKE`/关键字/标识符/类型名，顺带修掉“表 É 与 é 是同一张表”“`ſelect` 当成 SELECT”），参数上限改为 32766。
- 换参考库后的 fuzz 又发现 1 处真实差异：不引用任何表的 WHERE 项，SQLite 在循环前测试一次、为假就跳过整个循环（包括 FROM 子查询），MiniDB 以前逐行测试且先物化子查询（D71）。已修复，新增对照测试并用两个变异（不提前测试、先物化再测试）确认测试能发现。
- 测试：584 个，在参考 SQLite 3.53.4 上：Python 3.12/3.13/3.14 全部通过；3.11 上 581 通过、3 跳过。
- fuzz（参考 SQLite 3.53.4）：600 种子 × 500 + 150 种子 × 500（文件模式）+ 600 种子 × 400，0 不一致（修复 D71 之前是 1 个失败种子）。3.11 上另跑了 200 种子 × 400，0 不一致。
- CI：公开仓库 https://github.com/ayaya114514/MiniDB ，GitHub Actions 在 ubuntu-latest 上跑 3.11–3.14 测试矩阵 + fuzz（固定 300 种子、文件模式 100 种子、每次运行换 200 个新种子；每周定时一次）。CI 先编译参考 SQLite（按脚本哈希缓存）再跑测试和 fuzz。

## 阶段 15：外部基准与变形测试（完成）
- **sqllogictest**（`tools/sqllogictest.py`）：`.test` 格式 runner（`statement ok/error`、`query` 的类型串 / `nosort`/`rowsort`/`valuesort` / 标签 / MD5 哈希结果、`hash-threshold`、`skipif`/`onlyif`、`halt`），结果格式化照官方 C runner；多进程并行；`--json` 输出；`--min-passed` 做回归门槛。语料 622 个文件、1 GB，不入库：sqlite.org 的 Fossil 只对登录用户提供下载，改从 git 镜像按固定提交下载，`tools/sqllogictest.sha3` 固定每个文件的哈希（D72）。
- **基线**（参考 SQLite 规则，MiniDB 阶段 14 的功能）：622 个文件中 140 个全部通过；**5,939,879 条记录通过 3,743,727 条（63.03%）**，10 进程约 75 s（CPU 713 s）。按每个文件的第一个失败归类（482 个文件）：
  | 根因 | 文件数 | 例子 |
  |---|---:|---|
  | 聚合函数参数前的 `ALL` | 250 | `AVG(ALL col3)` |
  | 列类型 `FLOAT` | 213 | `CREATE TABLE tab0(pk INTEGER PRIMARY KEY, col0 INTEGER, col1 FLOAT, ...)` |
  | 列类型 `VARCHAR` | 12 | `CREATE TABLE t1(a INTEGER, b VARCHAR(30))` |
  | FROM 里带括号的连接 | 5 | `FROM (tab0 AS cor0 CROSS JOIN tab0)` |
  | 空的 IN 列表 | 2 | `SELECT 1 IN ()` |

  文件内部后续还缺：`CREATE VIEW`（4.4 万条记录）、`INSERT ... SELECT`（5 千条）、`x IN table`、`CREATE TRIGGER`。28 条“结果错误”全部在 `evidence/in1.test`，查明是前面的 `INSERT INTO t5 SELECT * FROM t4` 不支持导致表为空，不是已有功能的错误；**全量语料没有发现崩溃，也没有发现已支持功能的错误结果**。阶段 16 按此排序：列类型亲和性（REAL/NUMERIC/BLOB，任意类型名）、`f(ALL x)`、`IN ()`、括号连接、`INSERT ... SELECT`、`VIEW`、`IN table`，再做其余计划项。
- **变形测试**（`tests/metamorphic.py`，不依赖 sqlite3）：TLP where / distinct / min / max / count / sum / having 与 NoREC；复用 fuzz 的生成器（改为只生成字面量）；谓词里约三分之一是“列 比较 表中实际存在的值”（D73）。
  - 两轮：400 种子 × 300 查询（内存）+ 150 种子 × 300（文件模式），共比较 135,330 次（另有 26,696 次因出错跳过），0 不一致。
  - 变异测试（手工改 `executor.py` 后运行 60 种子 × 200 查询，再还原）：rowid 范围下界总是开区间 → 33 个种子失败（只用随机字面量时仅 2 个，据此加入了边界值条件）；索引 `<=` 上界漏掉等于边界值的行 → 27 个种子失败（NoREC、TLP where、TLP sum 都有发现）。`tests/test_metamorphic.py` 里固定了一个变异（值 3 的真值当成 NULL）必须被发现。
- `fuzz.py` 只在需要时导入 `sqlcompare`，生成器可以在没有参考 SQLite 的环境里使用。
- CI：fuzz 作业增加变形测试（固定 200 种子 + 每次换 100 个新种子的文件模式）；新增 sqllogictest 作业（语料按清单哈希缓存，`--min-passed 3743727`）。CI 上的这两项尚未实际运行（本阶段未 push）。
- 测试：619 个，全部通过（新增 `test_sqllogictest.py` 21 个、`test_metamorphic.py` 14 个）。
- 已知问题：sqllogictest 的 `label` 只解析不交叉核对（每条记录本身都有期望结果，不影响判定）。

## 阶段 16：SQL 补齐（完成）
完成的功能（每项都有与参考 SQLite 3.53.4 的对照测试，fuzzer 已覆盖）：
- `INSERT OR REPLACE/IGNORE/ABORT/FAIL/ROLLBACK`、`REPLACE`、UPSERT（`ON CONFLICT ... DO UPDATE/NOTHING`，含 excluded 的亲和性细节；DO UPDATE 只在有冲突检查能到达时才解析，D83）、`RETURNING`、语句日志（statement journal）语义；`INSERT INTO t (rowid, ...)`。
- CTE：`WITH`、`WITH RECURSIVE`（UNION 去重、ORDER BY 优先队列、LIMIT/OFFSET），`VALUES` 作为查询。
- `ALTER TABLE ADD COLUMN / RENAME TO / RENAME COLUMN / DROP COLUMN`（同步改写视图 SQL）、列 `DEFAULT`、`INSERT DEFAULT VALUES`、`TRUE/FALSE`。
- `CREATE [TEMP] VIEW` / `DROP VIEW`、`REINDEX`、`INDEXED BY` / `NOT INDEXED`。
- 窗口函数（D88）：全部内置窗口函数、聚合作窗口函数、ROWS/RANGE/GROUPS frame、EXCLUDE、`WINDOW` 子句；聚合的 `FILTER (WHERE ...)`；`string_agg`。按 SQLite window.c 的执行顺序逐步复现，滑动求和的浮点舍入与行序都与 SQLite 一致。
- 标量函数：`substr`、`replace`、`trim`/`ltrim`/`rtrim`（按 trimFunc 逐字节，字符集遇 NUL 截断）、`instr`、`round`、`printf`/`format`（移植 3.53 源码）、`hex`/`unhex`、`quote`、`unicode`/`unistr`、`char`、`concat`/`concat_ws`、`glob`/`like`（ESCAPE）、`iif`/`if`、数学函数、日期时间函数（移植 date.c）；位运算 `& | ~ << >>`，BLOB 字面量、十六进制整数字面量；后缀 `ISNULL` / `NOTNULL` / `NOT NULL`。
- 列类型亲和性 INTEGER/REAL/NUMERIC/TEXT/BLOB（任意类型名）、BLOB 值、非法 UTF-8 文本、REAL↔TEXT 逐位对齐（D80）。
- `RIGHT` / `FULL [OUTER] JOIN`（含 USING/NATURAL 的 SQLite 合并列语义、`ON clause references tables to its right` 检查，D84）；比较亲和性施加到两侧，索引查找的 key 转换同样按此规则（D85）。
- 结果列别名可在 WHERE/ON/GROUP BY/HAVING/ORDER BY 表达式中使用（D86）。
- 子查询中的外层聚合，以及 SQLite 判定聚合查询与 misuse 报错的规则（D87）。

**sqllogictest**：5,939,852 / 5,939,879 条通过（619/622 个文件无失败；阶段 15 基线 3,743,727，即 63.03% → 99.9995%）。剩下的 27 条：23 条是 `CREATE TRIGGER`（不支持）；4 条在 `slt_lang_aggfunc.test`，语料的期望值来自 3.43 之前的 SQLite（sum 还没有补偿求和、溢出时不报错），MiniDB 的结果与参考 SQLite 3.53.4 相同。CI 的 `--min-passed` 提高到 5939852（CI 上未运行，本阶段未 push）。

**测试**：871 个，全部通过（参考 SQLite 3.53.4，Python 3.12）。新增 `tests/test_window.py`（窗口函数探测集按顺序比较 + 40 个随机窗口种子）。独立的随机窗口对照（全部 frame 类型/边界、EXCLUDE、分区、ORDER BY 并列、NULL 与混合类型）另跑了 600 个种子，全部逐位一致。

**fuzz / 变形测试**（阶段后半段，参考 SQLite 3.53.4）：
- f25 600 种子 × 400：5 个失败种子，其中 4 个被随后的外层聚合、trim 修复消除（用当时的生成器回放确认），1 个促成了 ON 右侧引用检查。
- 加入 RIGHT/FULL、三表连接、别名、外层聚合、窗口函数之后：f28 400 × 400、f29（文件模式）200 × 400、m12 200 × 300、m13（文件模式）150 × 300 均 0 失败；f30 600 × 500 发现 1 个（连接重排后误用“第一张表的顺序”免排序，已修复并加了会在修复前失败的回归测试）。
- 其间修掉的其它 fuzz 发现：RIGHT JOIN 之前含子查询的 ON 被放到 RIGHT 级之后测试；INSERT OR REPLACE 用默认值填 NOT NULL 列后 upsert 的 excluded 没看到；USING 的 coalesce 值走索引查找时没做亲和性转换；`pow()` 溢出符号；DO UPDATE 的过早解析；`trim` 的 NUL 处理；RANGE 在 DESC 时 `<=` 的翻转（窗口）。
- 阶段末一轮（全部修复之后）：f31 600 种子 × 500 语句、m14 300 种子 × 300 查询，均 0 失败。

**性能**（`tests/benchmark.py`，10 万行，与阶段 15 相比）：插入 4.26 → 4.34 s、每条 SQL 都不同的 1 万次主键点查 0.71 → 0.78 s（解析 + 编译变多：新语法、别名替换、窗口收集器等），扫描/聚合/连接基本不变（比较改为两侧施加亲和性后用 `type(x) is str` 快速跳过，见 D85）。点查的解析/编译开销留给阶段 17。

已知问题 / 差异（如实记录）：
- 计划相关的报错时机：SQLite 的 WHERE 常量传播、恒真 OR 折叠会让某些错误（`abs()` 溢出、外层聚合 misuse）不出现或提前出现；子查询里 `sum()` 溢出同理。fuzzer 对这些做了 guard（D86、D87）。
- 聚合查询里的裸列取自哪一行依赖扫描顺序（`max()` 并列、无 min/max 时）；窗口 ORDER BY 有并列时的行序依赖读行顺序（单表全表扫描时已验证一致，走索引时不保证），fuzzer 的窗口 ORDER BY 以 rowid 收尾。
- 聚合参数中含子查询时，一律视为当前层的聚合（SQLite 会看子查询里的列）。
- `isnull`/`notnull` 在 MiniDB 里仍可作标识符（SQLite 里是保留字）；语法错误的措辞与 SQLite 不同。
- 不支持：TRIGGER、CHECK、COLLATE、TEMP TABLE、外键、表值函数；localtime 只在一个时区验证过；FROM 里外连接右侧的括号连接。


## 阶段 17：执行性能（完成）
每项改动都在改动前后跑了 `tests/benchmark.py`（新增两项：复合 WHERE 全表扫描、10 万行与 200 行无索引表的等值连接），并用差分测试或对照测试确认语义不变。

- **无索引等值连接用哈希**（D89）：内层表只能全扫、且有“本表列 = 已连接表的表达式”时，每次运行连接首次探测前把该表按列哈希（SQLite 的 automatic index），派生表也适用；有索引时仍走索引。17.2 s → 0.12 s。
- **tokenizer 用单个正则**：词、整数、普通字符串、运算符、空白走一个编译好的正则，其余情况沿用原逻辑。与旧 tokenizer 对 40 万个随机片段做差分，除非 ASCII 数字（如 `²`，旧实现直接崩溃，现在与 SQLite 一样当作标识符字符）外完全一致。
- **CREATE INDEX / REINDEX 批量构建**：键排序后 `BTree.bulk_load` 自底向上装满节点（每层末尾两节点平衡以满足最小填充），UNIQUE 在排好序的键上检查；新增 bulk_load 的不变量测试。文件因索引页装满变小（15.7 → 14.8 MB）。
- **生成 Python 源码**（D90）：运算符表达式、结果列元组、ORDER BY 键、只有内连接的嵌套循环、分组聚合循环都生成源码再 `compile()`；常量放在环境里、按源码文本缓存 code 对象，同形语句只编译一次。
- **解析器**：单个字面量/列（VALUES 行里）跳过各优先级层；当前 token 由属性改为普通字段。与旧解析器对 5.5 万条 fuzz 语句做差分，语法树与报错完全一致。
- 其它：`decode_row` 快路径。

| 操作（10 万行） | 阶段 16 末 | 阶段 17 末 | sqlite3 |
|---|---:|---:|---:|
| insert 100,000 rows, one INSERT each, one transaction | 4.358 s | 2.670 s | 0.191 s |
| insert 100,000 rows, one INSERT each with ? parameters | 1.061 s | 0.983 s | 0.067 s |
| insert 100,000 rows, 1,000 per INSERT, one transaction | 3.049 s | 1.770 s | 0.064 s |
| insert 1,000 rows, autocommit (a commit + fsync each) | 0.136 s | 0.137 s | 0.379 s |
| 10,000 primary key lookups | 0.798 s | 0.729 s | 0.055 s |
| 10,000 primary key lookups with ? parameter | 0.153 s | 0.106 s | 0.039 s |
| 100 primary key range scans (1,000 rows each) | 0.199 s | 0.081 s | 0.021 s |
| full scan: count(*) WHERE age > 50 | 0.124 s | 0.059 s | 0.002 s |
| full scan: SELECT * (all rows) | 0.123 s | 0.066 s | 0.036 s |
| GROUP BY city with 3 aggregates | 0.174 s | 0.093 s | 0.030 s |
| ORDER BY age, name LIMIT 10 | 0.142 s | 0.080 s | 0.003 s |
| full scan with a compound WHERE | 0.237 s | 0.121 s | 0.006 s |
| join 100,000 people with 200 unindexed rows (equality) | 17.156 s | 0.118 s | 0.010 s |
| CREATE INDEX on age | 0.697 s | 0.387 s | 0.017 s |
| 73 indexed lookups (age = ?, ~1,400 rows each) | 0.146 s | 0.074 s | 0.002 s |
| join 100,000 people with cities (index lookup per row) | 0.281 s | 0.152 s | 0.006 s |
| reopen and run one lookup | 0.001 s | 0.001 s | 0.000 s |
| database file size (MB) | 15.7 | 14.8 | 9.3 |

扫描、聚合、连接、排序大约快了一倍，字面量插入快约 40%；最慢的一项（无索引连接）从 1500 倍降到 12 倍于 SQLite。

**验证**：测试 888 个全部通过；fuzz 600 种子 × 500 语句、文件模式 200 × 400、变形测试 300 × 300，均 0 失败；sqllogictest 全量第一次跑出 528 条回归——21 张表的连接生成了 21 层嵌套 `for`，超出 Python 的 20 层静态嵌套限制——改为超过 16 张表时用通用循环，select5.test 恢复 1436/1436，总数回到 5,939,852 / 5,939,879。

**已知问题**：每条语句都不同的点查（解析 + 编译为主）只快了约 9%，解析与规划本身仍是纯 Python 的开销；参数化插入受 B+ 树与记录编码限制，只快了约 7%。生成代码的调试信息是 `<expression>` / `<join loop>` 这类伪文件名。

## 阶段 18：存储层（进行中，2026-09-30 暂停）
已完成：
- **VACUUM / VACUUM INTO**（D91，已提交）：在内存 pager 里用 bulk_load 重建紧凑副本，按相同页号写回并缩小 `page_count`、清空空闲链表；没有读者时的 checkpoint 截断文件。测试覆盖内容与索引/统计/overflow 值保持、并发读者、提交和 checkpoint 每一步的崩溃（`tests/test_vacuum.py`，11 个）。示例：删掉 2/3 数据后 3.16 MB → 0.65 MB。

进行中（未提交）：**读者标记 + 部分 checkpoint**。半成品存放在 `git stash` 里（`stash@{0}`，说明 "stage18-read-marks-wip"），恢复用 `git stash pop`：
- 已写：`locking.py` 的读槽锁（`<db>-read0..8`，槽 1..8 独占并记录快照，槽 0 共享）；`pager.py` 的 `<db>-shm`（generation、已回填帧数、各槽的快照标记）读写，以及 `begin_read` 登记读槽（登记后若发现更新的提交就重来；WAL 已全部回填时共享槽 0、只读数据库文件）。
- 未写：`checkpoint()` 改为按 min(各占用槽的标记) 部分回填、全部回填且无读者时才清空并截断；写者在 WAL 全部回填且无其他读者占槽时从头重启 WAL；`end_transaction` 释放槽；并发与崩溃测试。设计要点见 stash 中 `_take_read_slot` 的注释。

剩余：
1. 完成上面的读者标记与部分 checkpoint，并加“持续有读者时 WAL 不再无限增长”的测试。
2. Windows 锁（`msvcrt.locking` 只有排他锁：读者锁区间内一个随机字节、排他者锁整个区间来模拟共享锁），CI 增加 `windows-latest`。本机无法运行 Windows，需如实标注“未运行”。
3. 更强的崩溃模型测试：半页写（torn write）、fsync 之前的写入丢失。
4. 阶段末：全量测试 + sqllogictest + 大规模 fuzz；更新 README 已知限制（文件会收缩了）；勾选 CLAUDE.md 的阶段 18。
