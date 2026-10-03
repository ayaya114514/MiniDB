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

## 阶段 18：存储层（完成）
完成的功能：
- **VACUUM / VACUUM INTO**（D91）：在内存 pager 里用 bulk_load 重建紧凑副本，按相同页号写回并缩小 `page_count`、清空空闲链表，checkpoint 时截断文件。例：删掉 2/3 数据后 3.16 MB → 0.65 MB。rowid 规则照 SQLite：有 INTEGER PRIMARY KEY 或有索引的表保留 rowid，其余重新编号；`VACUUM INTO` 全部保留（最初写成“一律保留”，fuzz 加入 VACUUM 后发现，10/600 个种子）。
- **读者标记 + 部分 checkpoint + 日志重启**（D92）：`<db>-shm` 里记录已回填帧数和 8 个读槽的快照标记；checkpoint 只拷到最老的在用标记，并按那个状态的页数截断文件；已回填的帧从数据库文件读，所以全部回填后写者可以在有读者时从头重用日志（新 generation）；读者总是跨越提交时写者最多等 0.1 秒。实测 4 个读进程（每个读事务 ≥2ms）+ 1 个写者约 4700 提交/秒：不等待时日志涨到 48224 帧，现在最长 1007 帧，吞吐降约 5%。
- **锁改为 `-shm` 上的字节锁**（D93）：fcntl 记录锁 + 每进程一个描述符、进程内登记表仲裁；旁路文件只剩 `-wal`、`-shm`（`-lock` 没了）；数据库文件上的 SHARED 锁取消（每个读事务都持有读槽）。
- **Windows 锁**（D95）：`msvcrt.locking` 只有排他锁，按 SQLite 的做法用 64 字节区间模拟共享锁；锁层拆成 `PosixLocks` / `WindowsLocks` 两个后端。CI 增加 `windows-latest` 任务（存储/并发/崩溃测试）。
- **更强的崩溃模型**（D94）：`tests/test_crash_model.py` 模拟断电——未 fsync 的写入丢失、乱序、按 512 字节扇区撕裂，截断也可能丢失。发现并修正：checkpoint 扩展文件的写入被撕裂后文件大小不是页大小的整数倍，被误报为损坏。800 次随机崩溃（3075 个未同步操作被丢弃/撕裂）：633 次恢复到已确认提交、167 次恢复到进行中的提交、0 次错误。
- fuzzer：生成 VACUUM；文件模式下另有一个连接持有读快照，主连接 `checkpoint_frames = 8` 且随机 checkpoint，快照必须始终不变（20 个种子里 604 次新 generation、69 次部分回填）。
- 其间修掉的 fuzz 发现：UPSERT 的 `excluded` 要看到被同一语句中前面的行原地转换过的默认值（D96，SQLite 的寄存器复用行为）。

**验证**：测试 932 个全部通过（新增 test_read_marks 9、test_crash_model 6、test_locking 15、test_vacuum 13 等）；fuzz 内存模式 600 种子 × 500 语句 0 失败，文件模式 200 × 400 有 2 个失败种子——9791 是上面的 UPSERT 问题（已修），9661 见已知问题；变形测试文件模式 300 × 300 0 失败；sqllogictest 全量 5,939,852 / 5,939,879，与阶段 17 相同。

**benchmark**（阶段 17 末 → 阶段 18 末，同一台机器，10 万行）：autocommit 的 `?` 点查 0.116 → 0.145 s（每个读事务多约 3 µs：拿/放读槽和 WAL_READ 共 4 次 lockf，外加读 `-shm`）；逐条 autocommit 插入 0.126 → 0.140 s；其余在噪声范围内（单条 INSERT 2.97 → 2.85 s，批量 1.93 → 1.86 s，范围扫描、全表扫描、GROUP BY、连接基本不变）。CREATE INDEX 在 benchmark 里 0.42 → 0.475 s，但单独测量和 cProfile 显示两个版本调用完全相同（1.316 vs 1.333 s），差异是 benchmark 进程内的噪声。

**已知问题 / 做得不扎实的地方**：
- Windows：阶段 18 结束时只在假的 `msvcrt` 上测过；2026-10-01 推送后 CI 的 `windows-latest` 上 320 个测试通过（见阶段 19）。Windows 上最多 64 个进程同时共享一把锁；降级不是原子的（丢锁时放弃该槽重试）；Windows 上不跑与参考 SQLite 的对照测试。
- 一个始终不结束的读事务仍会让日志变长：写者等 0.1 秒后放弃，之后日志每增长 4000 帧才再等一次。
- `-shm` 的读写依赖小 pread/pwrite 的原子性，没有 SQLite 那样的双份头 + 校验和防撕裂读。
- 连接不能跨 `fork()` 使用；删除正在使用的 `-shm` 会让新旧连接各锁各的（与 SQLite 相同）。
- 崩溃模型不模拟目录项丢失和 `-shm` 内容（重开时清零），扇区固定 512 字节。
- fuzz 种子 9661（文件模式）：`sum(DISTINCT ...)` 的整数溢出取决于累加顺序，SQLite 用覆盖索引 `i12` 做全表扫描（按 c1 顺序），MiniDB 扫表（按 rowid），属于已记录的“依赖查询计划”一类，未修。
- 每个 autocommit 读语句多约 3 µs 的锁开销（见上）。

## 阶段 19：展示（完成，2026-10-01）
本地完成：
- **Playground**（D99，`playground/`、`tools/build_playground.py`）：Pyodide 314 在 module Web Worker 里运行内存中的 MiniDB；每条语句显示结果和 EXPLAIN 计划，右侧画出所选表/索引的 B+ 树（分层、父子连线、叶子兄弟链、填充率）；5 个示例（B+ 树与索引、窗口函数、递归 CTE 曼德博集合、UPSERT/RETURNING/连接、事务回滚）。本机浏览器实测：桌面 1440×900 与移动端 390×844 无横向溢出，console 无报错，⌘/Ctrl+Enter、出错停止、BLOB/NULL/Inf 显示正常；首个示例 7 条语句约 70–100 ms。`tests/test_playground.py` 在本机 Python 上跑 bridge 和全部示例。
- 做示例时发现并修复：`BETWEEN` 不能走索引（D97，计划现在与 SQLite 一致）。
- fuzzer 加入 TRUE/FALSE 后发现并修复（D98）：`... FROM t WHERE FALSE` 报 "no such column"（长期存在）；解析期 `X AND 0` 折叠；`IS [NOT] TRUE/FALSE` 真值测试；多行 VALUES 第二行起不经名字解析的怪癖。以及 fuzz 种子 20016（AND 折叠）。
- **系列文章草稿**（`docs/articles/`，5 篇 + 索引），素材来自 DECISIONS.md 与 PROGRESS.md 的实测数字，怪癖篇的每个 SQL 例子都在参考 SQLite 和 MiniDB 上核对过。未发布。
- `.github/workflows/pages.yml`：推送到 master 时构建并部署到 GitHub Pages。

验证：测试 957 个全部通过；fuzz 300 种子 × 400 语句 0 失败。

**发布**（用户确认后）：本地领先的 52 个提交（阶段 16–19）推送到 GitHub；仓库 Pages 来源设为 GitHub Actions，`pages` 工作流部署成功；线上 `https://ayaya114514.github.io/MiniDB/` 各文件 HTTP 200，浏览器实测首个示例 7 条语句 67 ms，console 无报错。blog 工具栏末尾加入 MiniDB（缩写 MDB），blog 本地构建通过、部署成功，线上 `/tools/` 显示“共 11 个”且卡片正常。
**CI**（第一次在这些提交上运行）：Linux 3.11–3.14、fuzz、sqllogictest（5,939,852 / 5,939,879）全部通过；新的 `windows-latest` 任务在真实 Windows 上 320 个测试通过。第一次运行时 Windows 与 Linux 3.14 各有一个失败：`test_threads_reading_and_writing` 断言“日志最长 < 700 帧”，CI 的 2 核机器上读线程在 GIL 下比写者的等待更久，日志到了约 1100 帧——这是依赖线程调度的断言，改为检查“日志在读者持续读时确实重启过”（长度上界由多进程测试检查）后全部通过。

已知问题：VALUES 行里的聚合（`VALUES (count(*))`，SQLite 合法）MiniDB 报 misuse；Playground 依赖 jsDelivr 上的 Pyodide（首次加载约 10 MB）。

## 阶段 20：SQLite 文件格式兼容（完成，2026-10-01）
- **SQLite 文件格式**（D100–D101）：`minidb/sqlite_format.py`（varint、record、100 字节文件头、B-tree 页与本地负载 / overflow 公式、freelist trunk/leaf）、`minidb/sqlite_btree.py`（表 B+ 树与索引 B 树，SQLite 式的最多 3 个兄弟页重分布、顺序追加时的 balance_quick、自底向上批量构建）、`minidb/sqlite_pager.py`（与 SQLite 逐字节相同的 rollback journal、SQLite unix VFS 的 PENDING/RESERVED/SHARED 锁、多段热日志回放）。按文件头自动识别格式；`Database(path, format="sqlite")`、`connect(..., format=)`、`python -m minidb --sqlite` 新建。catalog 直接用 `sqlite_schema`、`sqlite_autoindex_*`、schema cookie；ANALYZE 写 `sqlite_stat1`（行与数字与 SQLite 相同），VACUUM / VACUUM INTO 照 SQLite；SQLite 写下而 MiniDB 解析不了的对象原样保留、相关表只读；integrity_check 核对每一页的归属。两种格式都能读 `sqlite_schema` / `sqlite_master`。
- 对照测试（`tests/test_sqlite_format.py`，38 个）：record 与 sqlite3 逐字节相同；sqlite3 写的文件 MiniDB 读、MiniDB 写的文件 sqlite3 读并 `PRAGMA integrity_check`；两者轮流写同一个文件；提交的每个崩溃点 × 由 sqlite3 / MiniDB 恢复；MiniDB 回放真实 sqlite3 进程死掉留下的热日志；与另一个进程里的 sqlite3 双向加锁互斥；拒绝的文件（页大小、WAL、UTF-16、auto_vacuum）；ANALYZE / VACUUM 结果与 SQLite 相同；B 树对模型的随机测试（含 overflow、DESC 列、三层索引的内部项删除、页面归属）。fuzzer 的 `--sqlite-format` 模式在每个种子结束时让 sqlite3 检查 MiniDB 的文件并逐表比较内容；`tools/sqllogictest.py --format sqlite`、`tests/benchmark.py --sqlite-format`。
- 阶段末 fuzz 发现并修复（两种格式都复现，与文件格式无关）：
  - 种子 4108：含 NUL 的文本做算术——SQLite 的 sqlite3AtoF 停在 NUL 而 sqlite3Atoi64 读过 NUL，所以只有 NUL 之前整体是数字时才是 REAL（`'5\0'+0` 是 5.0，`'5 x\0'+0` 是 5）。
  - sqllogictest 发现：`NOT INDEXED` / `INDEXED BY` 以前只检查不影响计划，覆盖索引扫描之后就看得出来了。现在照 SQLite：NOT INDEXED 只用表本身（rowid 查找可以），INDEXED BY 只用那个索引、没有条件可用时扫整个索引，两者都不用自动索引（hash join）；SELECT、UPDATE、DELETE 都适用。
  - 种子 5512 与阶段 18 记录未修的 9661：没有 ORDER BY 时的行顺序（裸列、`group_concat`、`sum` 溢出）。按 SQLite 改了访问路径（D102）：覆盖索引全扫描（按 SQLite 的 szEst / LogEst 选索引）、全表扫描按 3N 计价、`x = a OR x = b` 生成虚拟的 IN、IN 按索引顺序输出、MULTI-INDEX OR 逐项输出去重。三个种子现在都与 SQLite 一致。
- 性能（D103）：剖析 SQLite 格式后只动热点——单元格缓存字节数、页拷贝共享单元格、record 按 header 编译 `struct` 解码、用户表直接交出解码好的行。

**benchmark**（10 万行，同一台机器；“阶段 19 末”是 e7764bf，规划器改动之前）：

| 操作 | 阶段 19 末 | 阶段 20 末 MiniDB 格式 | SQLite 格式第一版 | SQLite 格式优化后 | sqlite3 |
|---|---:|---:|---:|---:|---:|
| 逐条 INSERT，一个事务 | 3.06 s | 2.89 s | 14.11 s | 4.78 s | 0.25 s |
| 逐条 INSERT，`?` 参数 | 1.08 s | 1.08 s | 10.83 s | 2.59 s | 0.08 s |
| 每条 INSERT 1000 行 | 2.59 s | 2.48 s | 9.65 s | 3.98 s | 0.07 s |
| 1000 次 autocommit 插入 | 0.148 s | 0.142 s | 0.568 s | 0.564 s | 0.199 s |
| 1 万次主键点查 | 0.839 s | 0.843 s | 0.999 s | 0.883 s | 0.066 s |
| 1 万次主键点查，`?` 参数 | 0.148 s | 0.149 s | 0.170 s | 0.145 s | 0.042 s |
| 100 次主键范围扫描 | 0.091 s | 0.089 s | 0.295 s | 0.100 s | 0.023 s |
| 全扫 count(*) WHERE | 0.066 s | 0.065 s | 0.265 s | 0.075 s | 0.003 s |
| 全扫 SELECT * | 0.076 s | 0.073 s | 0.297 s | 0.107 s | 0.039 s |
| GROUP BY 3 个聚合 | 0.098 s | 0.098 s | 0.299 s | 0.108 s | 0.034 s |
| CREATE INDEX | 0.492 s | 0.475 s | 0.600 s | 0.412 s | 0.023 s |
| 73 次索引等值查找 | 0.082 s | 0.076 s | 0.184 s | 0.137 s | 0.002 s |
| 索引嵌套循环连接 | 0.170 s | 0.166 s | 0.406 s | 0.199 s | 0.006 s |
| 文件大小 | 14.8 MB | 14.8 MB | 13.4 MB | 13.4 MB | 9.3 MB |

规划器改动对 MiniDB 格式在噪声范围内。

**验证**：测试 1001 个全部通过；fuzz：SQLite 格式文件模式 300 × 400、SQLite 格式内存 600 × 500、MiniDB 格式内存 600 × 500、MiniDB 格式文件 200 × 400 全部 0 失败（含先前失败的 4108、5512 所在区间），变形测试文件模式 300 × 300 0 失败；sqllogictest 全量（规划器改动后）两种格式都是 5,939,846 / 5,939,879：比阶段 17 多出的 6 个失败都是 `... FROM t1 NOT INDEXED` 的 `group_concat` 顺序——覆盖索引扫描没有理会 NOT INDEXED。随后让 `NOT INDEXED` / `INDEXED BY` 照 SQLite 约束规划器（见下），这 6 个恢复；用到这两个提示的只有 evidence 目录的 3 个文件，该目录两种格式复跑都回到原来的 27 个失败，全量回到 5,939,852 / 5,939,879（推送后 CI 的全量运行确认）。覆盖率（只跑 test_sqlite_format）：sqlite_btree 92.4%、sqlite_format 94.1%、sqlite_pager 92.3%。

**已知问题 / 做得不扎实的地方**：
- 只支持 4096 字节页、UTF-8、rollback journal（不支持 WAL 模式的 SQLite 文件）、无 auto_vacuum；SQLite 格式的大事务脏页全在内存。
- SQLite 格式下插入仍比 MiniDB 格式慢 1.6–2.4 倍：每次插入都 O(页内单元格数) 地累加页面用量，写入时还要先编码 MiniDB record 再转成 SQLite record。
- DESC 索引在 SQLite 格式里只维护、不用于查找和排序（扫描时整体排序）；MiniDB 格式里 DESC 仍按升序存。
- Windows：推送后第一次在 windows-latest 上运行，SQLite 格式的 23 个测试失败。(1) SQLite 的协议要把 SHARED 升级为 EXCLUSIVE，`msvcrt.locking` 不能转换锁，新建文件就超时——先照 SQLite 的 winLock 先解锁再加锁，修掉 19 个；(2) 第二次运行剩下的`test_locks_against_a_sqlite_process` 暴露了真问题：sqlite3 在 NT 上对 SHARED 区间加的是共享锁，MiniDB 用排他字节模拟共享锁必然冲突，于是 Windows 后端改用 ctypes 的 LockFileEx（D104）；(3) 两个 fuzz 种子是 Windows 上 Python 自带的 SQLite 版本不同（REAL 转文本、求和精度），该测试改为只在参考版本上运行；(4) 多进程测试里读者 2 秒内只读到 1 次（回滚日志模式读写互斥，Windows 上 fsync 慢），断言改为至少 1 次。同一次运行里 `test_threads_reading_and_writing` 又因线程调度没等到日志重启，改为读者停下后必须重启（读者持续读时的上界由多进程测试检查）。LockFileEx 后端推送后（f551a3f）CI 全绿：windows-latest 上 359 个通过、3 个跳过（需要参考 SQLite 的 fuzz 对照），Linux 3.11–3.14、fuzz、sqllogictest 全部通过。
- 行顺序：连接顺序仍是 MiniDB 自己的代价模型，可能与 SQLite 不同；MULTI-INDEX 的子项不做覆盖读取。
- sqlite_stat1 在 ≥10 张表时的行顺序与 SQLite 可能不同（D101 的测试只覆盖少量表）。

## 阶段 21：约束、排序规则与 PRAGMA（完成，2026-10-02）
- **约束**（D105）：列级 / 表级 `CHECK`（报错文本与 SQLite 相同，未命名的照 sqlite3Dequote 取原文），表级 `PRIMARY KEY`
  / `UNIQUE`、约束上的 `ON CONFLICT`、`AUTOINCREMENT`（`sqlite_sequence`）；schema 文本按原样保存，ALTER TABLE 照
  alter.c 改文本。`WITHOUT ROWID`、`STRICT`、生成列、表达式索引、部分索引明确报 “not supported”。
- **排序规则**（D106）：BINARY / NOCASE / RTRIM 在比较、IN、BETWEEN、CASE、min/max/nullif、DISTINCT 聚合、ORDER BY、
  GROUP BY / DISTINCT、复合查询、窗口、子查询列、索引（两种文件格式，含 SQLite 写的带排序规则的索引）里都照 SQLite 推导。
- **PRAGMA**（D107）：`table_info` / `table_xinfo`、`index_list` / `index_info` / `index_xinfo`、`foreign_key_list`、
  `foreign_key_check`、`table_list`、`database_list`、`integrity_check` / `quick_check`（含 NOT NULL / CHECK）、
  `user_version` / `application_id` / `schema_version`（两种格式的文件头）、`page_size` / `page_count` /
  `freelist_count`、`journal_mode`（只读）、`data_version`、设置类（`foreign_keys`、`defer_foreign_keys`、
  `ignore_check_constraints` 等），以及 `pragma_xxx()` 表值函数（可横向引用）。
- **外键**（D108，`minidb/foreign_keys.py`）：`REFERENCES` / `FOREIGN KEY`、`PRAGMA foreign_keys`、ON DELETE / ON UPDATE
  的 CASCADE / SET NULL / SET DEFAULT / RESTRICT / NO ACTION、`DEFERRABLE INITIALLY DEFERRED`、`defer_foreign_keys`，
  照 fkey.c 的计数器模型；编译期的 “foreign key mismatch” / “no such table”；子表有合适索引时按索引找子行。
- **SQLite 格式里带这些对象的表可写**；`tools/sample_databases.py` 下载 Chinook 1.4.5 与 Northwind（固定版本、SHA3-256
  校验、存 `.sample-databases/`、不入库），`tests/test_sample_databases.py`：同一批语句（外键开，含 FK 报错、延迟外键的
  事务、CHECK 报错、AUTOINCREMENT、NOCASE 索引、视图）在 MiniDB 和 sqlite3 各一份拷贝上执行、结果逐条相同，之后 sqlite3
  对 MiniDB 的文件 `integrity_check` 为 ok、`foreign_key_check` 与自己的相同、`sqlite_schema` 和每张表逐行相同。这条
  测试当场找到两个真问题：INTEGER PRIMARY KEY 加 NOT NULL 时插入 NULL 被拦（Chinook 的写法）、未命名 CHECK 的报错名
  （Northwind 的 `[UnitPrice]>=(0)`）。
- fuzzer：COLLATE（列、索引、表达式）、CHECK、ON CONFLICT、表级约束、REFERENCES（随机动作与延迟）、PRAGMA 语句、
  `likely()` 系列；奇数种子开 `PRAGMA foreign_keys`。变形测试的 TLP distinct / having / min / max 在排序规则下改为
  按折叠后的文本比较（哪一个“相等的文本”被留下取决于行序，这是 oracle 的局限，不是引擎问题）。
- 阶段内 fuzz 找到并修复（都与 SQLite 对过）：两遍式 UPDATE / DELETE 按 rowid 顺序处理行（RowSet）；外键动作的比较
  没有父列亲和性（`OLD.x`）；单行 INSERT 不找新父键修复的子行；外键只在可能中止时才要语句日志；`defer_foreign_keys`
  在事务外随“读了文件”的语句结束；RIGHT JOIN 的 USING `coalesce()` 带第一个参数的排序规则；单独的 `min/max(x)` 遇到
  `x = <外部表达式>` 只读第一行；`x IS NOT (TRUE COLLATE ...)` 是真值测试；缺少 `likely()` / `unlikely()` /
  `likelihood()`；GROUP BY 走能给出分组顺序的索引（D109）；复合查询相等行的代表、裸列的 regHit、NOCASE 遇 NUL、
  `LIMIT 0`、别名替换前的 AND 折叠（D106）。

**benchmark**（10 万行，同一台机器；“前”是阶段 20 末 f6588a3，“后”是 cf2d77d）：

| 操作 | MiniDB 格式 前 | MiniDB 格式 后 | SQLite 格式 前 | SQLite 格式 后 | sqlite3 |
|---|---:|---:|---:|---:|---:|
| 逐条 INSERT，一个事务 | 2.95 s | 2.99 s | 4.78 s | 4.98 s | 0.20 s |
| 逐条 INSERT，`?` 参数 | 1.08 s | 1.11 s | 2.61 s | 2.74 s | 0.07 s |
| 每条 INSERT 1000 行 | 2.48 s | 2.49 s | 4.00 s | 4.01 s | 0.07 s |
| 1000 次 autocommit 插入 | 0.144 s | 0.143 s | 0.461 s | 0.436 s | 0.210 s |
| 1 万次主键点查 | 0.850 s | 0.919 s | 0.890 s | 0.978 s | 0.058 s |
| 1 万次主键点查，`?` 参数 | 0.147 s | 0.150 s | 0.144 s | 0.150 s | 0.041 s |
| 100 次主键范围扫描 | 0.091 s | 0.091 s | 0.099 s | 0.102 s | 0.022 s |
| 全扫 count(*) WHERE | 0.066 s | 0.064 s | 0.072 s | 0.075 s | 0.002 s |
| GROUP BY 3 个聚合 | 0.100 s | 0.108 s | 0.112 s | 0.119 s | 0.041 s |
| CREATE INDEX | 0.475 s | 0.474 s | 0.413 s | 0.410 s | 0.022 s |
| 73 次索引等值查找 | 0.077 s | 0.079 s | 0.138 s | 0.144 s | 0.002 s |
| 索引嵌套循环连接 | 0.175 s | 0.170 s | 0.198 s | 0.208 s | 0.006 s |

第一次跑“后”时 `?` 点查从 0.147 s 变成 0.278 s、插入慢 6%：每条语句都遍历语法树判断“是否读文件”（只在
`defer_foreign_keys` 开着时才需要），以及给新生成的 rowid 多查了一次 B 树；修掉后如上表。剩下的差距：字面量 SQL 的
编译多了排序规则推导（点查 +8%），GROUP BY 的裸列要按 regHit 记录行（+6–8%），SQLite 格式插入 +4%。

**验证**：测试 1343 个全部通过；fuzz（阶段末，最终代码）：MiniDB 格式内存 2000 种子 × 400 语句、SQLite 格式文件模式
1200 种子 × 400 语句，全部 0 失败；变形测试文件模式 300 × 300 0 失败；sqllogictest 全量 5,939,852 / 5,939,879（与阶段 20
相同：23 条是 TRIGGER，4 条是已知的 `sum`/`total` 精度与整数溢出）。

**已知问题 / 做得不扎实的地方**：
- 视图不做谓词下推：Northwind 的 `SELECT * FROM [Order Details Extended] WHERE OrderID = 10250` 要 3.4 s（sqlite3
  0.4 ms），`[Order Subtotals]` 同样；外键的子表没有索引时每次父键变化都全表扫描子表（Northwind 改一次 ProductID 约 1 s）。
- Chinook 的页大小是 1024，测试先用 sqlite3 `VACUUM` 成 4096（阶段 25 支持各种页大小）。
- 带统计信息时 GROUP BY 的索引选择是 SQLite 的代价模型，MiniDB 只照搬“无统计”的规则：fuzz 种子 2266（旧随机流）里
  `GROUP BY c3, c0` 在 ANALYZE 之后 SQLite 选了只排好 c0 的索引，组内显示的 NOCASE 文本因此不同（依赖查询计划的一类）。
- CHECK 里双引号标识符找不到列时 SQLite 当字符串（DQS），MiniDB 报 “no such column”。
- 外键动作语句是否需要语句日志是保守估计（动作碰到任何约束就算）；`defer_foreign_keys` 的“读文件”判断里 CTE 名
  不分作用域。
- `WITHOUT ROWID`、`STRICT`、生成列、表达式 / 部分索引仍不支持（SQLite 写的含这些对象的表只读）。

## 阶段 22：触发器（完成，2026-10-02）
- **触发器**（D110，`minidb/triggers.py`）：`CREATE [IF NOT EXISTS] TRIGGER` / `DROP TRIGGER [IF EXISTS]`，`BEFORE` / `AFTER` /
  `INSTEAD OF`（视图上），`INSERT` / `UPDATE [OF 列]` / `DELETE`，`FOR EACH ROW`、`WHEN`、`NEW` / `OLD`，
  `RAISE(IGNORE / ROLLBACK / ABORT / FAIL)`，`PRAGMA recursive_triggers`（最多 1000 层，含外键动作）。触发器存进
  `sqlite_schema`（两种格式，文本照 SQLite 保存）；SQLite 格式里带触发器的表可写，sqlite3 和 MiniDB 互相执行对方建的
  触发器；ALTER TABLE 照 alter.c 改写触发器文本（RENAME TO / RENAME COLUMN），DROP COLUMN 检查触发器。
- 与 SQLite 逐语句对照的行为：触发顺序（新的先）、BEFORE INSERT 的 NEW（亲和性、rowid -1）、BEFORE 触发器改动或删除
  当前行后的重读、外键动作与触发器的先后、REPLACE 删除的行只在 recursive_triggers 开着时触发且之后重查唯一约束、
  upsert 触发 UPDATE 触发器、外层 `OR ...` 覆盖程序里的冲突子句、`changes()` / `total_changes()` /
  `last_insert_rowid()`、INSTEAD OF 的各种细节（rowid 列、亲和性只在有触发器时转换、带 RETURNING 时的可写性）。
- **编译期语义**：SQLite 在编译语句时编译它可能运行的程序，所以 MiniDB 在构造计划时按 SQLite 的代码生成顺序编译触发器
  程序和外键动作，并记录“编译过的程序清单”——外键 mismatch、程序里的 no such column 等错误报得一样早、报的是同一个；
  isSetNullAction 怪癖经由触发器程序时也一致；多行写入与语句日志（mayAbort）的判断照 SQLite。
- sqllogictest：剩下的 23 条 TRIGGER 记录通过，全量 5,939,875 / 5,939,879（剩 4 条是已知的 `sum` / `total`
  精度和整数溢出），CI 的基线随之提高。
- fuzzer：随机触发器（表上 BEFORE / AFTER、视图上 INSTEAD OF；程序里 INSERT / UPDATE / DELETE / RAISE，用 NEW / OLD）、
  DROP TRIGGER、写视图、`PRAGMA recursive_triggers`。它在阶段内找到并修复（都与 SQLite 对过，大多与触发器交织）：编译顺序
  （BEFORE、约束检查、外键、AFTER；UPDATE / DELETE 为算列掩码先编译全部触发器）、单行 INSERT 的外键优化在触发器程序里
  及有 INSERT 触发器时失效、递归按触发器判断、NEW 的 INTEGER PRIMARY KEY 有 INTEGER 亲和性、语句日志的 mayAbort
  （DELETE 也不再总有日志）、NOT NULL 分两遍检查、`x IN (列表)` 按 OP_Eq 的亲和性规则比较、有窗口函数的查询里属于它的
  聚合出现在子查询中是误用。
- 测试：`tests/test_triggers.py`（两种格式，各 13 组对照，含重开文件、sqlite3 与 MiniDB 互相执行对方的触发器）。

**benchmark**（10 万行；“前”是阶段 21 末 7bc00af）：

| 操作 | MiniDB 格式 前 | MiniDB 格式 后 | SQLite 格式 前 | SQLite 格式 后 | sqlite3 |
|---|---:|---:|---:|---:|---:|
| 逐条 INSERT，一个事务 | 3.04 s | 3.40 s | 4.95 s | 5.46 s | 0.21 s |
| 逐条 INSERT，`?` 参数 | 1.14 s | 1.14 s | 2.74 s | 2.76 s | 0.07 s |
| 每条 INSERT 1000 行 | 2.51 s | 2.54 s | 4.07 s | 4.10 s | 0.07 s |
| 1 万次主键点查 | 0.923 s | 0.934 s | 0.972 s | 1.016 s | 0.059 s |
| 1 万次主键点查，`?` 参数 | 0.153 s | 0.153 s | 0.151 s | 0.155 s | 0.041 s |
| GROUP BY 3 个聚合 | 0.109 s | 0.106 s | 0.118 s | 0.114 s | 0.032 s |
| 索引嵌套循环连接 | 0.175 s | 0.189 s | 0.208 s | 0.206 s | 0.006 s |

第一次跑“后”时，从字面量 SQL 编译的语句慢了约 20%（每条 INSERT / SELECT / DELETE 都为 mayAbort 遍历一次语法树）；
改成只在多行写入和触发器程序里计算、窗口检查改为按需、没有触发器且外键关闭时跳过编译期的程序准备后，逐条字面量 INSERT
仍慢约 6%（剖析：每条 INSERT 编译多约 5 µs），用 `?` 参数的路径不变。上表是修正后的数字（“前”的 SQLite 格式数字是同一
次运行里测的）。

**验证**：测试 1373 个全部通过；fuzz（最终代码）：MiniDB 格式内存 4000 种子 × 400 语句（0–1999、4000–5999）、SQLite 格式
文件模式 2400 种子（2000–3199、6000–7199）、MiniDB 格式文件模式 300 种子（5000–5299）——只有 3 个种子不同，都已归类
（见下）；变形测试文件模式 300 × 300 0 失败；sqllogictest 全量见上。第一次全量 sqllogictest（`--jobs 8`）在 1 小时的
后台时限被停掉，原因没有查清（按目录分别跑全部通过、每个文件耗时与阶段 21 相同）；重跑 `--jobs 8` 用了 21 分钟、结果如上。

**已知问题 / 做得不扎实的地方**：
- fuzz 种子 4181、6037：外键子表扫描用索引查找时，SQLite 把子表列的亲和性就地作用在被删除行的寄存器上，于是 DELETE 的
  RETURNING 看到 `127` 而不是 `'127.0'`（寄存器复用的副作用），MiniDB 不模拟。
- fuzz 种子 6647：ANALYZE 之后 SQLite 选覆盖索引全扫描，ORDER BY 在 NOCASE 下相等的 'b' / 'B' 先后不同（依赖统计信息的
  计划，同阶段 21 的种子 2266）。
- 带 BEFORE INSERT 触发器时 SQLite 对单行 VALUES 的表达式求值两次（NEW 一次、存储一次），只在 `random()` 之类上看得出，
  MiniDB 只求一次。
- 程序清单的回放：缓存的程序记录的是它编译时请求过的程序，在别的语句里回放时去重规则与 SQLite 不完全相同（极端组合下
  isSetNullAction 可能不同）。外键动作在运行时才第一次编译的子表触发器，其错误报得比 SQLite 晚。
- TEMP 触发器不支持（阶段 25 才有 temp schema）；触发器程序里的 `UPDATE ... FROM` 不支持（MiniDB 本来就不支持），SQLite
  文件里带这种触发器的表只读。
- 逐条字面量 INSERT 的编译比阶段 21 慢约 6%。

## 阶段 23：JSON 函数（完成，2026-10-02）
- **JSON 函数**（D111，`minidb/jsonb.py`、`minidb/jsonfuncs.py`）：照 json.c 在 SQLite 的二进制格式 JSONB 上实现——
  `json`、`jsonb`、`json_valid`（含 flags）、`json_type`、`json_extract` 与 `->` / `->>`、`json_array` / `json_object`、
  `json_insert` / `json_replace` / `json_set` / `json_array_insert` / `json_remove` / `json_patch`、`json_quote`、
  `json_array_length`、`json_pretty`、`json_error_position`，聚合 `json_group_array` / `json_group_object`（也可作窗口
  函数），表值函数 `json_each` / `json_tree`（参数可引用前面的表）。**jsonb 系列全部实现**（`jsonb_*` 编辑函数、
  `jsonb_array` / `jsonb_object`、`jsonb_group_*`、`jsonb_each` / `jsonb_tree`），JSONB 字节与 SQLite 相同；参考 SQLite
  3.53.4 的 `pragma_function_list` 里 30 个 json 函数全部存在。支持 JSON5 输入。
- 与 SQLite 逐值对照的细节：结果文本逐字节相同（数字保留原文、`0x1F` → `31`、REAL 写法）、错误信息（malformed、
  `bad JSON path: '...'`、`not an array element`、`JSON path too deep`、`JSON nested too deep`、参数个数）、
  `json_error_position`、`json_each` 的 `id`（JSONB 偏移）、1000 层的嵌套上限（解析、路径查找、为不存在的路径造子
  结构、compact / pretty 渲染、json_patch 各自的条件）。
- **JSON subtype**：`JSONText`（str 子类）/ `JSONBlob`（bytes 子类）在表达式、CASE / coalesce / 标量子查询之间传递，
  在 SQLite 丢掉它的地方丢掉——存进表、FROM 子查询 / 视图 / CTE（只有被展平的简单子查询里的裸列保留，按列判断）。
- **解析缓存**：每条语句 4 项的 JsonCache 照搬（同一语句里 `json_set` 的结果文本带着编辑后的 JSONB，`jsonb()` /
  `json_valid()` 看得出来），连“结果还在 JsonString 的 100 字节静态区时不进缓存”的条件也照搬。
- `DISTINCT` 聚合放过一个 NULL（`json_group_array(DISTINCT x)` 看得出，以前 MiniDB 跳过 NULL）。
- fuzzer：随机 JSON 文档 / 路径 / 函数（文本与 JSONB）、JSON 聚合与窗口、`json_each` / `json_tree` 与前面的表连接；
  对照层给 sqlite3 设步数上限（约 500 万 VM 步，随机触发器偶尔级联出上千万次改动，MiniDB 跳过这一句）。
- fuzz 在阶段内找到并修复（JSON 让随机流整个换了一遍，所以也找出不少与 JSON 无关的问题，D112）：subtype 按列丢失、
  JSONB 带 subtype、解析缓存及其静态区条件（种子 1822）；rowid / INTEGER PRIMARY KEY 与 upsert 的 `excluded.x` 没有排序
  规则、展平子查询的裸列就是该列；UPDATE 的 REPLACE 删除冲突行并运行 DELETE 触发器时固定当前行（`constraint failed`）、
  REPLACE 之后的唯一约束复查（拿第一遍最后找到的 rowid 比较）、外键子表扫描就地改写 OLD 的亲和性（阶段 22 记下的种子
  4181 / 6037 随之修好）、upsert 的 isSetNullAction 编译顺序、`total_changes()` 在出错的触发器程序之后、触发器跳过
  upsert、NEW / OLD 读 REAL 列、mayAbort 的更多来源。自己检查边界时另外发现：998 层的 JSON 就抛 Python
  `RecursionError`（递归上限，现在处理大 JSON 前按需提高）；`json_array_insert`（3.53 新增）原先缺失。
- 测试：`tests/test_json.py` 13 组对照（含嵌套上限、array_insert、解析缓存、subtype 经过各种查询），另在
  test_triggers / test_constraints / test_foreign_keys 里补了 D112 的用例。

**benchmark**（10 万行；“前”是阶段 22 末 5472a37，与“后”同一次运行里测；“后”测于 b6ed109，之后的 f8dccfc 只改了
JSON 代码）：

| 操作 | MiniDB 格式 前 | MiniDB 格式 后 | SQLite 格式 前 | SQLite 格式 后 | sqlite3 |
|---|---:|---:|---:|---:|---:|
| 逐条 INSERT，一个事务 | 3.23 s | 3.18 s | 5.11 s | 5.10 s | 0.20 s |
| 逐条 INSERT，`?` 参数 | 1.15 s | 1.16 s | 2.71 s | 2.77 s | 0.07 s |
| 每条 INSERT 1000 行 | 2.50 s | 2.54 s | 4.06 s | 4.03 s | 0.07 s |
| 1 万次主键点查 | 0.937 s | 0.949 s | 1.016 s | 0.996 s | 0.059 s |
| 1 万次主键点查，`?` 参数 | 0.154 s | 0.154 s | 0.146 s | 0.150 s | 0.041 s |
| GROUP BY 3 个聚合 | 0.110 s | 0.106 s | 0.121 s | 0.117 s | 0.031 s |
| 索引嵌套循环连接 | 0.173 s | 0.176 s | 0.201 s | 0.202 s | 0.006 s |

持平（差别在噪声内；SQLite 格式的全表 `SELECT *` 一次测出 0.069 → 0.108 s，复测基线也是 0.106 s）。

**JSON 微基准**（1 万个文档，内存库，结果与 sqlite3 逐条相同；脚本不入库）：

| 操作 | MiniDB | sqlite3 | 倍数 |
|---|---:|---:|---:|
| `json_extract` 嵌套字段作过滤条件 | 0.153 s | 0.003 s | 54x |
| `->>` 后 GROUP BY | 0.152 s | 0.004 s | 34x |
| 每行 `json_set` 两个路径 | 0.297 s | 0.006 s | 51x |
| `json_each` 展开数组 | 0.193 s | 0.002 s | 79x |
| `json_tree` 遍历每个文档 | 0.539 s | 0.003 s | 156x |
| `json_group_array` 1 万个对象 | 0.196 s | 0.005 s | 39x |
| `jsonb()` 再转回文本 | 0.196 s | 0.004 s | 45x |
| `json_valid` | 0.113 s | 0.002 s | 47x |

JSON 是逐字节照搬 json.c 的纯 Python 解析 / 渲染，比 sqlite3 慢 34–156 倍，本阶段没有做优化。

**验证**：测试 1428 个全部通过；fuzz（最终代码 f8dccfc）：MiniDB 格式内存 4000 种子 × 400 语句（0–1999、4000–5999）、
SQLite 格式文件模式 2400 种子（2000–3199、6000–7199）、MiniDB 格式文件模式 300 种子（5000–5299）——只有 5 个种子不同，
都依赖查询计划、已归类（见下）；变形测试文件模式 300 × 300 0 失败；sqllogictest 全量（`--jobs 8`，20 分钟）5,939,875 / 5,939,879，与阶段 22 相同（剩 4 条是已知的 `sum` / `total` 精度和整数溢出）。

**已知问题 / 做得不扎实的地方**：
- 依赖查询计划的 fuzz 种子（与 JSON 无关，都缩成了最小用例，差别只在 SQLite 选的计划决定的行序或代表行）：248（DESC 索引上相等键的
  先后）、1961（ANALYZE 之后 GROUP BY 在 NOCASE 下相等的组取哪一行作代表）、2789 / 6728 / 7119（索引扫描顺序决定
  并列行的先后）。同类的还有阶段 20 的种子 25（有统计信息时 `sum(DISTINCT)` 的溢出取决于累加顺序）。
- subtype 经过 FROM 子查询时，SQLite 是否展平还取决于外层查询（例如外层有 LIMIT），MiniDB 只看子查询本身来近似。
- 视图 INSERT 的 RETURNING 里 `typeof()` 的参数不经 OP_RealAffinity，MiniDB 不区分：`INSERT INTO v VALUES (1)
  RETURNING a, typeof(a)`（a 是 REAL 列）SQLite 返回 `1.0, 'integer'`，MiniDB 返回 `1.0, 'real'`。
- `json_each` / `json_tree` 的参数不能引用 FROM 里排在它后面的表：`FROM json_each(t.j), t` 在 SQLite 可以（规划器
  把它挪到后面），MiniDB 报 `no such column: t.j`。
- 编辑函数为不存在的路径造子结构时，json.c 会多减一次 `iDepth`，之后渲染时的 1000 层上限因此偏移；只有用编辑拼出
  超过 1000 层的值才看得出，MiniDB 不模拟。
- 没有 `pragma_function_list`。
- JSON 性能见上表，没有优化。
- fuzz 的步数上限会跳过级联改动过多的语句（种子 0–59 里没有一句被跳过）。

## 阶段 24：Playground 打开本地 SQLite 文件（本地完成，2026-10-02；部署待确认）
- **打开与导出**（D113）：页面上“打开文件”或把文件拖进页面，文件读成字节交给 worker，MiniDB 用新的
  `Database.deserialize()` 把它变成内存里的 SQLite 格式库；查询、修改都在它上面，“导出”用 `serialize()` 交回文件下载
  （文件名沿用原名）。数据全程只在浏览器内存里。打开后若编辑器里是示例，换成列出 `sqlite_schema` 的查询并运行。
  不是 SQLite 文件、或 MiniDB 还不支持的 SQLite 文件（如 1024 字节页），给出明确的报错，原来的库保持不变。
- **`serialize()` / `deserialize()`**（`Database` 与 DB-API `Connection`）：照 Python sqlite3 同名方法，语义按实测对齐
  （含未提交改动、事务中 deserialize 报 `database is locked`、文件连接变成内存库而原文件不动）。
- **真实页面布局**：SQLite 格式的库在右侧多两块——“文件”：每页一格，按类型着色（表 / 索引的内部页与叶子页、溢出页、
  空闲列表主干页与空闲页、锁字节页），高亮所选表或索引的页，点击查看；“第 N 页”：按页面原始字节画出 4096 字节的分布
  （文件头、页头、单元格指针数组、未分配区、各单元格、空闲块、碎片），给出内容区起点、空闲字节构成、最右子页，下面列出
  单元格（偏移、字节数、rowid 或索引项、负载、左子页、溢出页链）。点击 B 树节点或页面格子切换。SQLite 的索引是 B 树
  （内部页也存条目），树的标题随格式写“B 树”/“B+ 树”。大文件的树和页面图按 change counter 缓存。
- fuzzer：SQLite 格式（内存与文件）的每个种子结束时做 `serialize()` / `deserialize()` 双向对照（sqlite3 打开 MiniDB 的
  镜像做 integrity_check 并逐表比较，MiniDB 打开 sqlite3 的镜像同样比较）。
- 测试：`test_serialize_and_deserialize`（与 sqlite3 互相交换镜像、事务中、无效数据、文件连接、DB-API）；
  `test_playground.py` 新增两项：用 sqlite3 写出带空闲块、碎片、溢出页、空闲列表的文件，检查每页的区域恰好覆盖 4096
  字节、碎片字节数等于页头记录、叶子页 rowid 与 sqlite3 一致、空闲页数等于 `PRAGMA freelist_count`、修改后导出的文件
  sqlite3 `integrity_check` 通过且内容一致；以及拒绝的文件。
- **浏览器实测**（本机 Chrome，经 `python -m http.server` 打开构建好的 `site/`）：
  - 桌面 1440×900：示例照常（首个示例 76 ms）；“打开文件”打开 Chinook（217 页）显示 schema、文件图、各页布局；
    拖入 24 MB 的 Northwind（6031 页）0.1 秒打开，切换到 60 万行的 `Order Details` 后树与页面正确；
    在 Pyodide 里 `SELECT count(*), sum(...)` 扫 60 万行 2.3 秒，结果与 sqlite3 相同。
  - 修改 Chinook 后导出：浏览器导出的字节与本机 Python 跑同样语句的导出 SHA-256 相同；点“导出”真实下载到
    `~/Downloads/Chinook.sqlite` 的文件同一哈希，sqlite3 `integrity_check` 为 ok。
  - 1024 字节页的 Chinook 给出 MiniDB 的报错原文（只显示异常信息，不带 traceback），原库不变。
  - 在打开的文件上运行示例、⌘+Enter、清空数据库后回到 MiniDB 格式（导出禁用、页面区隐藏）都正常。
  - 390×844（移动端模拟）：无横向溢出（scrollWidth = 390）。上述过程 console 无报错。
  - 实测中发现并修复：构建出的 app.js 里有一处变量重名的语法错误（之后构建都先 `node --check`）；打开文件后立刻切换
    对象时，两次刷新交错导致页面区停在旧页（加了世代号，过期的刷新和页面请求作废）；示例下拉在编辑器不是示例时显示空白
    （加了占位项）。
- 性能：本阶段没有改动查询 / 存储的热路径（只有 `Database` 打开时的代码拆分），没有重跑 benchmark。

**验证**：测试 1431 个全部通过；fuzz：SQLite 格式内存 2000 种子（0–1999，每个种子结束时做镜像双向对照）、SQLite 格式文件模式 1200 种子（2000–3199）、MiniDB 格式内存 2000 种子（4000–5999）、MiniDB 格式文件 300 种子（5000–5299）——只有已归类的 248、1961、2789 不同（依赖查询计划，同阶段 23）；变形测试文件模式 300 × 300 0 失败。

**已知问题 / 做得不扎实的地方**：
- **部署未做**：推送到 GitHub（Pages 工作流部署）需要用户确认，本地领先若干提交，线上仍是阶段 19 的版本。
- 只能打开 4096 字节页、非 WAL、无 auto_vacuum 的 SQLite 文件（阶段 25 的范围）；MiniDB 自己格式的文件不能在页面里打开。
- 页面布局只画 B 树页；溢出页、空闲列表页只标出类型。文件图对上万页的库仍是一页一格（可滚动）。
- 整个文件读进内存（Pyodide 的 WebAssembly 内存），没有测过百 MB 以上的文件。
- 只在本机 Chrome 上实测，Safari / Firefox 未运行；拖放用合成的 DragEvent 测试，没有真人拖拽。

## 阶段 25：SQLite 文件格式补全（完成，2026-10-03）
- **各种页大小**（D114）：512–65536 的 2 的幂都能读写；页面几何（可用字节、本地负载上下限、空闲列表容量、锁字节页）
  由每个库的 `Geometry` 给出。`PRAGMA page_size` 对新库立即生效，已有内容的库在下一次 `VACUUM` 时生效；改页大小的提交
  的回滚日志按旧页大小记录，sqlite3 和 MiniDB 都能在各崩溃点回放。`VACUUM` / `VACUUM INTO` 支持 SQLite 格式。
- **auto_vacuum**（D115）：FULL / INCREMENTAL 的指针图（提交前从脏页推出），根页照 SQLite 集中在前部，`DROP` 搬页，
  FULL 提交时搬页截断，`PRAGMA incremental_vacuum(N)`、`PRAGMA auto_vacuum`。顺带修好：提交使文件变小时被截掉的原页
  也进回滚日志；批量建索引少一个分隔键的边界情况（种子 2184）。
- **生成列**（D116）：`VIRTUAL` / `STORED`，两种格式；记录里只放非 VIRTUAL 列（与 SQLite 的存储布局一致，可直接读写
  sqlite3 建的表）；计算次序、循环检测、CREATE TABLE 报错、NOT NULL 两趟、UPDATE 的依赖、ALTER TABLE ADD / RENAME /
  DROP COLUMN 照 SQLite。
- **临时表**（D117）：`CREATE TEMP TABLE / VIEW / INDEX / TRIGGER`，`main.` / `temp.` 限定名，`sqlite_temp_master`；
  名字解析与主库视图 / 触发器“固定到所在库”照 SQLite。`CREATE TABLE ... AS SELECT`（D118，列名去重、类型名、SQL 文本）。
- **WITHOUT ROWID**（D119）：两种格式；SQLite 格式里表就是主键索引 B 树，二级索引接上缺的主键列（DESC 规则含 SQLite 的
  bAscKeyBug），`PRAGMA index_xinfo` 等与 sqlite3 一致；REPLACE / UPSERT / 外键 / 触发器 / RETURNING 都走同一套代码。
- **WAL 模式**（D120，新模块 `minidb/sqlite_wal.py`）：SQLite 自己的 `-wal` / `-shm` 格式与锁协议（WRITE / CKPT /
  RECOVER / READ0–4 / DMS），读者快照、写者 BUSY_SNAPSHOT、日志重开、PASSIVE 自动 checkpoint（`PRAGMA
  wal_autocheckpoint`）、`PRAGMA wal_checkpoint(PASSIVE|FULL|RESTART|TRUNCATE)`、恢复（只认最后一个校验和正确的提交帧）、
  最后一个连接关闭时回填并删除 `-wal` / `-shm`；`PRAGMA journal_mode = WAL / DELETE` 双向切换。
  - 与 sqlite3 进程并发读写：测试里 MiniDB 与 sqlite3 子进程交替 / 同时读写，各自看到对方的提交，读者快照不受并发写者
    影响；多进程（MiniDB 和 sqlite3 混合）对一个 WITHOUT ROWID 的 kv 表并发自增，最终计数正确、`integrity_check` ok。
  - 崩溃：写日志头、写帧、sync、更新 wal-index、checkpoint 写页、checkpoint sync 各崩溃点，以及 torn frame、被 kill 的
    进程；之后 sqlite3 和 MiniDB 分别打开恢复，内容等于最后一次提交、`integrity_check` ok。超过 4062 帧（一个 wal-index
    块以上）的大事务。
- **fuzz 新增**：`--wal`（SQLite 格式文件、WAL 模式；sqlite3 在子进程里跑，结果用 pickle 传回）；生成器加了各种页大小、
  auto_vacuum、生成列、TEMP 对象、CTAS、WITHOUT ROWID 表（含 DESC / COLLATE / 复合主键、自引用外键）。镜像对照时把
  文件头 18–19 字节改回 1 再比较；参考 SQLite 自己 `integrity_check` 不过（D119 的已知 SQLite 行为）时跳过结尾检查。
- **fuzz 找到并修好的问题**（都有回归测试）：WITHOUT ROWID 改主键时的 REPLACE + 外键（49）、自引用外键（3713 / 3449）、
  重查唯一性（343）、`foreign_key_check` 的 rowid（224）、NOT NULL REPLACE 后重算主键（4958）、单列 INTEGER 主键的
  COLLATE 与 REAL 最后一列（703 / 857）、UNIQUE 自动索引的升序（149）；MiniDB 格式 B+ 树删除的长度记账（136）；
  `hasFK>1`（173）；VIRTUAL 列不算覆盖、从索引读 VIRTUAL 列（88 / 1001）、未用到的 VIRTUAL 列不计算（2496 的一部分）；
  GROUP BY 排序器、窗口临时表与 `scan_groups` 里的 IntReal / JSON 子类型（2197 / 177 / 3706）；upsert 的 `excluded`
  IntReal（895）、生成列影响的语句日志（3878）、带外键 REPLACE 算多行写（4799）；多行 INSERT 何时失去 JSON 子类型
  （4141，useTempTable 规则）；失败的 `CREATE UNIQUE INDEX` 结束隐式事务（566）；VACUUM 漏页与 schema format 0
  （2289 / 1478）。

**验证**：测试 1548 个通过、8 个跳过（MiniDB 格式的自动索引命名，与以前相同）；新增 `test_sqlite_wal.py` 29、
`test_without_rowid.py` 24、`test_generated.py` 30、`test_temp.py` 9，`test_sqlite_format.py` 扩到 62。fuzz（各 300–400
条语句）：MiniDB 格式内存 3000 种子（2000–4999）、SQLite 格式内存 2500 种子（2000–4499）只有 2496 不同；SQLite 格式文件
600 种子（1000–1599）0 失败；MiniDB 格式文件 400 种子（1000–1399）只有 1234 不同；WAL 800 种子（0–799）0 失败；变形测试
文件模式 300 × 300 0 失败。收尾改动后又跑了新种子：内存与 SQLite 格式内存各 300（5000–5299）、WAL 150（800–949）、
SQLite 格式文件 100（1600–1699），全部 0 失败。

**benchmark**（100,000 行；阶段开始 95f69cb → 现在）：
- MiniDB 格式：各项在 ±3% 以内（insert one-each 3.555 → 3.497 s，1000-per-INSERT 2.572 → 2.575 s，CREATE INDEX
  0.431 → 0.424 s，73 次索引查找 0.084 → 0.083 s）。中途 1000-per-INSERT 一度到 2.85 s：多行 INSERT 为判断
  useTempTable 遍历了每个字面量；改成跳过字面量和参数后恢复。
- SQLite 格式：insert one-each 5.147 → 5.430 s（+5%）、带参数 2.769 → 2.919 s（+5%）、1000-per-INSERT 4.149 → 4.209 s，
  其余持平或略快（CREATE INDEX 0.471 → 0.424 s）。插入的多出部分分散在新功能的检查里（TEMP pager 的语句驱动、生成列 /
  WITHOUT ROWID 分支），留给阶段 26。中途测到“73 次索引查找” 0.150 → 0.23 s，用 gc 回调确认是一次恰好落在这一步的
  第 2 代 GC 停顿（0.083 s），不是代码路径变慢；最终一次为 0.144 s。
- WAL 模式（两边都是 WAL）：自动提交插入 1000 行 0.363 s（回滚日志 0.430 s），主键点查 1.181 s（回滚日志 1.059 s，
  每个读事务多读索引头 / read mark 并加锁），带参数点查 0.187 s（0.171 s）。

**已知问题 / 做得不扎实的地方**：
- 种子 2496：SQLite 惰性计算 VIRTUAL 列，短路求值时可以避开在 ALTER ADD COLUMN 之前就存在的行上的出错表达式；MiniDB
  读行时算出所有用到的 VIRTUAL 列，会报错。
- 种子 1234：NOCASE 下相等的值做 GROUP BY 时，SQLite 取哪一个作代表取决于查询计划；类似地，ORDER BY 走不覆盖的索引
  时的计划差异（种子 88 的 `ORDER BY g0`）。
- WAL：Windows 上未运行（`-shm` 的字节锁走 `locking.py` 的 msvcrt 后端，pread / pwrite 退回 lseek + read / write，
  这些路径都没有实际跑过；CI 的 windows job 不含 `test_sqlite_wal.py`）；不支持 `PRAGMA locking_mode = EXCLUSIVE`（无 `-shm` 的 heap-memory wal-index）与 `journal_mode = MEMORY / OFF`；
  checkpoint 只实现 PASSIVE 语义（FULL / RESTART / TRUNCATE 不等待忙的读者，只在没人用日志时重开）。同一进程里同时用
  sqlite3 与 MiniDB 打开 WAL 库不安全（POSIX 锁按进程，见 D120）。
- 临时表：写临时表的语句也拿主库 RESERVED 锁；`deserialize()` 丢掉临时表。
- Playground：本阶段没有在浏览器里重新实测（构建测试 `test_playground.py` 通过，覆盖了新文件种类的打开）；阶段 24 的
  部署仍待确认。

## 阶段 26：执行性能（第二轮）（完成，2026-10-03）
- **索引等值查找**（D121）：`count(*)` 加索引前缀等值 / 范围、且没有别的条件时，B 树数区间里的键而不逐行经过连接循环和
  分组循环（MiniDB 格式每个叶子一次二分；SQLite 格式沿区间两条边界二分，中间整棵子树按单元格数累加，不解码键）；
  组的代表行仍读第一行。IndexScan 用作等值键的合取项不再在每个候选行上复查（SQLite 的 disableTerm，只对 `=`、
  非 VIRTUAL 列、内连接）。
- **插入路径**：语句种类按 `type()` 分派；参数绑定、record 编码先判断常见类型；`insert_row` 在没有触发器 / 外键 /
  索引 / REAL 列时跳过对应工作；tokenizer 的快速正则带上前导空白（每个 token 一次匹配，4 值 INSERT 5.45 → 4.75 µs）；
  多行 VALUES 的“解析期常量”判断对字面量直接返回。
- **SQLite 格式插入**：照 sqlite3BtreeInsert 只在页溢出时 balance（原来新开的最右叶子在填到 1/3 前每插一行都与兄弟页
  重新分配，页面也常年半空）；页面用量增量维护（`BtreePage.used()` 记住结果，插入时加上新单元格，原地替换单元格处
  作废）；表树直接收值列表，不再 MiniDB record 编码再解码。
- 测试：`test_counting_index_ranges_agrees_with_sqlite`（两种格式、混合类型、NOCASE、DESC、512 字节页的深树）、
  `test_page_usage_kept_up_to_date`（随机操作后逐页核对缓存的用量；去掉一处 forget_used 会失败）。
- **大 fuzz 找到并修好的老问题**（阶段 25 就有，新种子范围才暴露；都有回归测试）：
  - 表上有生成列时，SQLite 在检查约束前就对新行做了亲和性转换（sqlite3ComputeGeneratedColumns 先调
    sqlite3TableAffinity），upsert 的 `excluded` 应看到转换后的值（种子 7240）。
  - 窗口函数的参数在 SQLite 里先算进窗口的临时表再读回（IntReal 变整数）；只有读子类型的 json_group_* 在读回后
    重算；不同定义的窗口一层套一层，外层读到的是内层表按亲和性读回的值（种子 5359）。
  - WITHOUT ROWID 的行键用了寄存器里的值，JSON 子类型留在缓存的键里，覆盖索引扫描读回时带着子类型（种子 5430）。
  - fuzz harness：每条写语句后让 sqlite3 做 `integrity_check`（`quick_check` 不核对索引与表），参考库自己损坏
    （D119 的生成列 UPDATE）就结束这个种子——之后 SQLite 走索引的 DELETE 可能正好删掉那个陈旧条目（它删的是索引
    游标所在的条目），MiniDB 不模仿这种“意外自愈”（种子 6783）；`integrity_check` 计算 CHECK / 生成列时报错不算损坏。

**验证**：测试 1557 个通过、8 个跳过。阶段末大 fuzz（各 400 条语句）：SQLite 格式内存 2000 种子（6000–7999）、
MiniDB 格式内存 2000 种子（6000–7999）、SQLite 格式文件 600 种子（3200–3799）0 失败、MiniDB 格式文件 300 种子
（5300–5599）、WAL 600 种子（1400–1999）0 失败；找到的 6783、7240、5359、5430 已修复并复跑通过，剩下 7487
（与 2496 同类：SQLite 惰性计算 VIRTUAL 列，NATURAL RIGHT JOIN 的空表一侧从不需要比较，MiniDB 读行时就算出
`json_quote(BLOB)` 而报错）。变形测试文件模式 300 × 300（300–599）0 失败。阶段中途还跑过：内存与 SQLite 格式内存
各 1300+ 种子、SQLite 格式文件 900、WAL 400，除已归类的 1234 外 0 失败。

**benchmark**（100,000 行；阶段开始 ecfe261 → 现在）：

| 操作 | MiniDB 格式 前 → 后 | SQLite 格式 前 → 后 | sqlite3 |
|---|---:|---:|---:|
| 插入，每行一条 INSERT，一个事务 | 3.497 → 3.495 s | 5.430 → 3.813 s | 0.24 s |
| 插入，每行一条带 `?` 参数 | 1.208 → 1.103 s | 2.919 → 1.254 s | 0.08 s |
| 插入，每条 INSERT 1000 行 | 2.575 → 1.915 s | 4.209 → 2.427 s | 0.07 s |
| 自动提交插入 1000 行 | 0.149 → 0.150 s | 0.430 → 0.408 s | 0.21 s |
| 1 万次主键点查（`?` 参数） | 0.158 → 0.162 s | 0.171 → 0.148 s | 0.04 s |
| 与 200 行无索引表等值连接 | 0.172 → 0.132 s | 0.202 → 0.123 s | 0.011 s |
| 73 次索引等值计数（约 1400 行 / 次） | 0.083 → 0.008 s | 0.144 → 0.010 s | 0.002 s |
| 与小表连接（每行一次查找） | 0.182 → 0.170 s | 0.212 → 0.200 s | 0.006 s |
| 数据库文件大小 | 14.8 MB | 13.4 → 9.4 MB | 9.3 MB |

其余各项在 ±5% 内。几处看起来变慢的（MiniDB 格式 CREATE INDEX 0.424 → 0.486、SQLite 格式 `SELECT *` 0.069 → 0.102）
用 gc 回调确认是第 2 代 GC 停顿落在了这一步（分别 0.056 s、0.03 s；`SELECT *` 在旧代码上同样出现），代码路径不变。
WAL 模式（两边都 WAL）：逐行插入 5.738 → 3.685 s，带参数 2.916 → 1.295 s，索引计数 0.230 → 0.010 s。
SQLite 格式与 MiniDB 格式的差距：插入 1.1–1.3 倍（原 1.6–2.4 倍），查询 20% 以内。与 sqlite3 比：索引等值计数从
约 48 倍缩到 4–5 倍；带参数插入仍约 14 倍、拼字面量的插入约 14 倍（每条语句的解析与编译）。

**已知问题 / 做得不扎实的地方**：
- autocommit 点查约四成时间在读快照的系统调用（MiniDB 格式约 9 次：加解锁、fstat、pread），没有动（D121）。
- hash join（自动索引）仍对每个候选行复查等值条件，建 hash 时解码整行。
- 种子 7487 / 2496（惰性 VIRTUAL 列）、1234（计划相关的 GROUP BY 代表值）照旧；内存模式种子 6079 跑了 263 秒，
  原因没有查。
