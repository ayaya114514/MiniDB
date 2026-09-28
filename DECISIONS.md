# 设计决策记录

## D1 Python 运行环境
系统只有 Python 3.10（conda `ai_env`）和 3.9（系统自带），不满足 3.11+ 要求。
在项目内用 `mamba create -p .venv python=3.12 pytest` 建了独立环境，统一用
`.venv/bin/python -m pytest` 跑测试。`.venv/` 不入库。

## D2 阶段 1 的语句语法
阶段 1 还没有 SQL 解析器，用最简单的空格分隔命令：`insert <id> <name> <age>` 和 `select`。
`select` 按 id 排序输出，方便和 sqlite3 的 `ORDER BY id` 对照。阶段 4 会整体换成真正的 SQL。

## D3 REPL 输出格式
与 sqlite3 命令行默认的 list 模式一致：列之间用 `|` 分隔，NULL 输出为空串。
错误输出为 `Error: <message>`，REPL 继续运行。非交互（管道输入）时不打印提示符，便于测试。

## D4 页对象缓存（pager 设计）
pager 缓存的是“解码后的页对象”而不是原始字节：每种页类型实现 `from_bytes(pgno, data)`
和 `to_bytes()`，写回时才序列化。这样 B+ 树可以直接操作 Python 列表（用 `bisect` 查找），
避免每次访问都重新解析整页，性能好很多。代价是修改页之前必须调用 `pager.write(page)`，
这个约定同时是后面做语句回滚和 WAL 的挂钩点。

## D5 数据库文件头与空闲页
第 0 页是文件头：16 字节 magic、页总数、空闲链表头。释放的页组成单链表（每个空闲页前 4 字节是
下一个空闲页号），分配时优先复用。`Pager(None)` 使用内存中的 `BytesIO`，方便测试和对照。

## D6 行记录格式（record.py）
目录结构里没有专门的模块，但行序列化既被表存储用、也被 catalog 和索引用，单独放在
`minidb/record.py`。格式：u16 值个数 + 每个值 1 字节类型标签（NULL/INTEGER/REAL/TEXT）+ 负载。
INTEGER 固定 8 字节、TEXT 用 u32 长度前缀。简单、自描述，足以支持 SQLite 的动态类型。

## D7 B+ 树按字节大小分裂/合并
节点的“满/欠满”按序列化后的字节数判断（上限 `capacity`，下限 `capacity // 4`），而不是按
key 个数。这样表树（整数 key + 变长行）和以后的二级索引树（变长 key）可以共用同一份代码。
为保证分裂/借位后两边都不低于下限，限制单个 key ≤ `capacity // 8`，单个叶子 cell ≤
`capacity // 5`。借位后父节点的分隔 key 可能变长导致父节点超限，此时继续向上分裂。

## D8 根页号固定
根分裂时把根的内容搬到新页、根页变成只有一个孩子的内部节点再分裂；根只剩一个孩子时把孩子
拷回根页。于是一棵树终生由同一个根页号标识，catalog 里存的根页号不需要随插入删除更新。

## D9 可调节点容量（仅测试用）
`BTree(..., capacity=128)` 让节点只用页里的前 128 字节，用一万条数据就能造出 5 层以上的树，
覆盖内部节点分裂/合并/借位和根分裂的所有路径。生产代码总是用 4096。

## D10 overflow 页
叶子 cell 超过上限时，值存到 overflow 页链（每页 4 字节 next + 数据），cell 里只存总长度和
首页号。这样单行大小不再受页大小限制。key 不支持 overflow（索引 key 有长度上限）。

## D11 删除时回收空闲页
合并释放的节点页、overflow 页都进入空闲链表，下次分配优先复用，文件不会无限增长（但也不会缩小）。

## D12 阶段 3 临时命令
在临时语法里加了 `delete <id>`，便于在 SQL 解析器之前测试删除；`.btree` 打印树结构（缩进 +
每个节点最多显示 8 个 key）。

## D13 运算符优先级与 SQLite 一致
由低到高：OR、AND、NOT、相等类（`= == != <> IS [NOT] IN LIKE BETWEEN`）、比较（`< <= > >=`）、
`+ -`、`* / %`、`||`、一元 `- +`。注意 SQLite 把 `=` 和 `<` 放在不同层级（`a < b = c` 解析为
`(a < b) = c`），这里照搬，保证和 sqlite3 对照时语义一致。`==` 归一为 `=`，`<>` 归一为 `!=`。

## D14 非保留关键字
`KEY`、`TEXT`、`INTEGER`、`COUNT` 等不作为保留字，词法上是标识符，解析器按上下文识别
（`PRIMARY KEY`、列类型、函数名），这样它们可以用作列名/表名，和 SQLite 行为接近。

## D15 语法错误格式
`syntax error near "<token>": expected <what> (line L, column C)`，到达末尾时是
`syntax error at end of input: ...`。异常对象带 `line`/`column`，`caret()` 返回出错行和 `^` 指示，
REPL 会打印出来。

## D16 列类型
只接受 `INTEGER`（`INT` 为别名）和 `TEXT`，其他类型名报语法错误。约束支持 `PRIMARY KEY`、
`NOT NULL`、`UNIQUE`、`NULL`。

## D17 阶段 4 期间 REPL 仍用临时命令
阶段 4 只交付词法/语法分析（带完整单测），执行器在阶段 5 重写；在那之前临时命令解析挪到
`executor.parse_command`，REPL 暂时继续使用它。

## D18 新增模块：errors / values / database
- `errors.py`：统一异常层次（`Error` → `DatabaseError`/`OperationalError`/`IntegrityError`，
  `SQLSyntaxError` 也继承 `Error`），对应 sqlite3 的异常分类，方便对照测试比较“是否同类报错”。
- `values.py`：SQL 值语义（亲和性、比较、三值逻辑、算术、文本转换、标量函数），和执行器解耦，
  便于单独对照 sqlite3 验证。
- `database.py`：对外 API `Database(path)` / `execute(sql)`，负责语句级原子性和自动提交。

## D19 严格对齐 SQLite 的类型语义
所有细节先用 sqlite3 做实验再实现，并写成对照测试：列亲和性（INTEGER 列把 `'12'`、`'3.0'`、
`'1e3'` 存成整数，TEXT 列把数字存成文本）、比较时的亲和性规则（列 vs 字面量、IN 只看左侧亲和性）、
NULL 三值逻辑、整数溢出转 REAL、整数除法向零截断、除零得 NULL、`%` 对 REAL 走整数取余、
`abs('1')` 返回 REAL、LIKE 大小写不敏感（本机 sqlite3 对非 ASCII 也不敏感，用 `re.IGNORECASE` 对齐）。

## D20 REAL 转文本
sqlite 3.53 的浮点转文本用自己的近似十进制算法，无法低成本逐位复刻。采用“15 位有效数字能往返
就用 15 位，否则 17 位”，排版规则与 SQLite 相同（总带 `.0`，<1e-4 或 ≥1e17 用指数形式）。
实测与 sqlite 一致率约 91%，差异只在最后几位数字，列为已知限制；对照测试避开这类转换。

## D21 语句级原子性（statement journal）
sqlite 中多行 INSERT 中途违反约束时整条语句不生效。pager 在语句开始时开启 journal，
`write(page)` 第一次碰某页时保存它的副本；语句失败时恢复副本、丢弃新分配的页，并重载 catalog。
因为脏页在提交前绝不写盘（no-steal），没在缓存里的页磁盘上就是旧内容，不需要副本。

## D22 行的内存表示与 rowid
执行器里一行是一个 list：每张表依次贡献“所有列 + rowid”。INTEGER PRIMARY KEY 是 rowid 的别名
（值存在 B+ 树 key 里，记录里对应位置存 NULL）。没有别名的表也有隐藏 rowid，可用
`rowid`/`oid`/`_rowid_` 访问。自动 rowid = 当前最大 rowid + 1（与 sqlite 一致）。

## D23 访问路径只做“缩小候选集”
规划器从 WHERE 的 AND 合取项里找 `rowid = / IN / < <= > >=` 与常量（或前面表的列）的比较，
选择点查或范围扫描；但无论选了什么，完整的 WHERE 仍然对每个候选行求值。因此规划只影响性能、
不可能影响结果，对照测试也能覆盖。键值先做数值亲和性转换（`id = '5'` 查 5；与 NULL/文本比较时
按 SQLite 规则直接得出空集或无上界）。`EXPLAIN [QUERY PLAN]` 输出选中的访问路径。

## D24 UNIQUE 约束的检查方式
阶段 5 先用全表扫描检查 UNIQUE（包括非整数 PRIMARY KEY，SQLite 中它只是 UNIQUE、允许 NULL），
阶段 6 有了二级索引后改为自动索引。

## D25 Shell 行为
语句以 `;` 结束，可跨行；按 token 判断 `;` 是否在字符串/注释里。一次输入多条语句时逐条输出结果，
出错即停止该批剩余语句。元命令：`.tables`、`.schema`、`.btree TABLE`、`.help`、`.exit`。

## D26 ORDER BY / LIMIT 语义
ORDER BY 的整数字面量（包括 `-1`）表示结果列序号，越界报 SQLite 同款错误；标识符优先匹配结果列
别名，其次才是输入列（GROUP BY 反过来，输入列优先）。NULL 默认 ASC 在前、DESC 在后，支持
`NULLS FIRST/LAST`。多列排序用“从最后一列起逐列稳定排序”。LIMIT/OFFSET 先做数值亲和性，
必须得到整数否则 `datatype mismatch`；负 LIMIT 表示不限，负 OFFSET 视为 0；支持 `LIMIT a, b`。

## D27 聚合函数移植 SQLite 实现
SUM/AVG/TOTAL 直接移植 sqlite 的 func.c：全是整数时精确整数求和，溢出时报 `integer overflow`；
出现非整数后切换到 Kahan-Babuska-Neumaier 补偿求和，大整数拆成两段相加。这样浮点结果与 sqlite
逐位一致（有专门的随机浮点对照测试）。数值文本按 `sqlite3_value_numeric_type` 规则转换
（'5' 算整数，'5.0' 算 REAL，'abc' 算 0.0）。另外实现了 COUNT(*)/COUNT(x)/MIN/MAX/
GROUP_CONCAT 和 DISTINCT 聚合。

## D28 聚合查询里的“裸列”
和 sqlite 一样允许 `SELECT a, count(*) ...` 这种裸列：取组内第一行的值；如果查询里只有一个
MIN() 或 MAX() 聚合，则取取得极值的那一行。分组按 SQL 相等（1 与 1.0 同组，'1' 与 1 不同组），
输出按分组键排序。聚合出现在 WHERE / GROUP BY / 嵌套聚合中时报与 sqlite 相同的错误。

## D29 连接：按书写顺序的嵌套循环
支持 `,`、`[INNER] JOIN`、`CROSS JOIN`、`LEFT [OUTER] JOIN ... ON`，任意多张表，不做连接重排。
WHERE 和内连接的 ON 合成一个条件池，每个合取项在它用到的表都绑定后立刻检查（谓词下推）；
LEFT JOIN 的 ON 只决定该层是否匹配，没有匹配时补 NULL 行；LEFT JOIN 右表的访问路径只能用自己的 ON
条件（用 WHERE 条件会改变补 NULL 的语义）。内层表可以用依赖外层列的条件做 rowid/索引查找。

## D30 二级索引的 key 设计
索引 B+ 树的 key 是“各索引列值的 SQLite 排序键 (rank, value) 元组 + (1, rowid)”，rowid 保证
key 唯一；值为空。复用同一份 B+ 树代码。范围边界用哨兵 `LOW=(-1,)`、`HIGH=(3,)`（比任何
(rank, value) 小/大），例如 `a > 5` 从 `(sk(5), HIGH)` 之后开始，没有下界时从 NULL 之后开始。
DESC 索引列语法接受但按升序存储（不影响结果）。

## D31 索引的使用条件
规划器找“列 op 表达式”的合取项，按 SQLite 比较亲和性规则判断：如果比较需要转换“列那一侧”
（例如 TEXT 列与 INTEGER 列比较时 TEXT 列要转数字），索引顺序不再适用，不能用；否则对 key 做
另一侧的转换后查找。评分：rowid 等值 > 唯一索引全列等值 > 等值列越多越好 > rowid 范围 > 索引范围 > 全表扫描。

## D32 自动索引与 UNIQUE 检查
UNIQUE 列和非整数 PRIMARY KEY 自动建唯一索引 `minidb_autoindex_<表>_<n>`（不可删除，
`minidb_` 前缀保留）。唯一性检查改为索引前缀查找；按“最新的索引先检查”顺序，和 sqlite
报错信息一致。约束检查顺序：NOT NULL → datatype mismatch → rowid 冲突 → 唯一索引。
`CREATE UNIQUE INDEX` 遇到已有重复数据报错并整体回滚。索引 key 最长约 512 字节（key 不支持 overflow），超长报错。

## D33 integrity_check
`Database.integrity_check()` 检查所有 B+ 树不变量，以及每个索引的内容是否恰好等于由表数据计算出的
key 集合。测试和后面的模糊测试都用它兜底。

## D34 WAL 是“重做日志”
阶段 7 的 WAL 采用最简单可靠的重做日志：提交时 ① 把所有脏页（完整页镜像）写入 `<db>-wal`，
末尾写提交记录（帧数 + `CMIT` + 全部帧的 CRC32），fsync；② 把页写回数据库文件，fsync；③ 删除 WAL。
打开数据库时如果 WAL 存在：提交记录完整且校验通过就重放（幂等，重放中途崩溃下次再放一遍），
否则整个丢弃。数据页在 WAL 落盘之前绝不写入数据库文件，所以任何时刻崩溃，结果要么是整个事务、
要么完全没有。与 SQLite 的 WAL 模式不同：不支持并发读者从 WAL 读，也不需要 checkpoint。

## D35 事务模型：no-steal + 语句级 journal
未提交的脏页只留在内存（大事务占内存，列为限制），`ROLLBACK` 直接丢弃脏页并重读文件头、重载
catalog。事务内某条语句失败只回滚这条语句（沿用阶段 5 的 statement journal），事务继续——
与 sqlite 默认行为一致。`BEGIN` 嵌套、无事务时 `COMMIT/ROLLBACK` 的报错文字与 sqlite 相同。
关闭数据库时未提交的事务回滚。

## D36 数据库文件无缓冲 I/O 与崩溃钩子
数据库文件和 WAL 用 `buffering=0` 打开，写入直接交给操作系统，进程崩溃时不会有 Python 缓冲区
里的残留数据“事后写入”。pager 在提交的每一步调用 `crash_hook(point, detail)`（`wal_frame`、
`wal_commit`、`wal_sync`、`db_page`、`db_sync`、`wal_delete`），测试据此在任意一步模拟崩溃：
进程内抛异常，以及子进程里直接 `os._exit`。

## D37 提交失败后连接作废
提交途中出错（包括模拟崩溃）时，无法确定提交记录是否已经落盘，内存状态不可信。此时 `Database`
关闭文件并拒绝后续操作，要求重新打开，由恢复流程决定事务是否生效。

## D38 顺序追加时不均匀分裂
新 key 落在最右叶子的末尾（自增 rowid 的典型情况）时，被分裂的节点只分给新右节点“刚好达到
最小填充率”的内容，左节点保留约 75%。内部节点在同一路径上同样处理。原来 50/50 分裂导致顺序
插入的页只有一半满，10 万行的文件从 23.5 MB 降到 17.1 MB。由于单个 cell 不超过容量的 1/5，
两边都仍在 [capacity/4, capacity] 内，B+ 树不变量不变。

## D39 模糊测试的比较口径
fuzzer 与 sqlite 对照时刻意避开“SQLite 的答案取决于它的查询计划”的地方：没有全序 ORDER BY
时按多重集比较、聚合里不出现裸列、不用 GROUP_CONCAT；修改 UNIQUE 列或 rowid 的 UPDATE 只作用于
一行（多行时先处理哪一行决定是否冲突，而 SQLite 按它选的索引顺序处理）；数字按值比较（相等的
1 和 1.0 同时存在时，DISTINCT/GROUP BY/MIN/MAX 返回哪一个取决于扫描顺序）。常规对照测试仍然严格
区分 1 和 1.0。另外生成器避开 D20 的 REAL→TEXT 差异（会转成文本的表达式里不用 `/` 和大整数），
也不生成 `-9223372036854775808` 字面量（`abs()` 对它报错，而 SQLite 对常量表达式的求值时机
决定错误是否出现）。

## D40 fuzzer 发现并按 SQLite 修正的行为
- ORDER BY / GROUP BY 中，32 位以内的整数常量（可带一元 +/-）是列序号；SQLite 解析器还会把
  `<非 NULL 字面量> IS [NOT] NULL` 折叠成 0/1、把不含函数调用的 `X AND 0` 折叠成 0，这些折叠结果
  同样被当作列序号（因而报“ORDER BY term out of range”）。
- 最大 rowid 已是 2^63-1 时，新行的 rowid 随机选取（最多尝试 100 次，否则 `database or disk is full`）。
- 标量 `min()` 在相等值中返回最后一个，`max()` 返回第一个（与 sqlite 的 minmaxFunc 一致）。

## D41 异常层次改为 PEP 249
`Error` 下分 `InterfaceError` 和 `DatabaseError`，后者下分 `DataError`、`OperationalError`、
`IntegrityError`、`InternalError`、`ProgrammingError`、`NotSupportedError`，另有 `Warning`。
`SQLSyntaxError` 改为 `OperationalError` 的子类（sqlite3 对语法错误也抛 OperationalError），
仍带行列号和 `caret()`。文件损坏仍是 `DatabaseError` 本身。

## D42 参数绑定：解析期占位、执行期替换
占位符 `?`、`?NNN`、`:name`、`@name`、`$name`，编号规则照 SQLite：裸 `?` 取当前最大编号 + 1，
名字第一次出现时分配编号；编号范围 1..32766（SQLite 默认的 SQLITE_MAX_VARIABLE_NUMBER；最初照 conda-forge 构建写成 250000，见 D70）。解析结果按 SQL 文本缓存（LRU，256 条），
执行时把 `Parameter` 节点替换成 `Bound` 值（`Literal` 的子类）得到新的语法树。`Bound` 与字面量
求值完全相同（没有亲和性），但不参与解析期常量折叠、也不会被当作 ORDER BY 列序号——
与 SQLite 一致（`ORDER BY ?` 绑定 1 是常量）。绑定值：None/int/float/str，bool 转 int，
超出 64 位抛 `OverflowError`，其他类型（包括 bytes，MiniDB 没有 BLOB）抛 `ProgrammingError`。
参数个数/名字不匹配的报错逐字照搬 sqlite3。

## D43 DB-API 模块
`minidb.connect(path_or_":memory:", autocommit=False)` 返回 `Connection`，接口照 Python 3.12
sqlite3：`autocommit=False` 时连接建立即开事务、`commit()`/`rollback()` 后立即再开，SQL 里手写
`COMMIT` 结束后不会自动重开；`autocommit=True` 时 `commit()`/`rollback()` 什么也不做。
`with conn:` 成功提交、异常回滚；关闭时回滚未提交事务。Cursor 的 `rowcount`（DML 为影响行数，
其余 -1）、`lastrowid`（连接的最后插入 rowid；executemany 不更新）、`description`、`fetchmany`
的 `arraysize`、`executemany` 只接受 DML 等行为都与 sqlite3 对照测试。

## D44 多个连接打开同一个文件（已知问题，阶段 10 解决）
目前每个 `Database` 有自己的页缓存，另一个连接提交后本连接看不到新数据。阶段 10 用文件头里的
变更计数器检测并清空缓存，同时加跨进程锁。

## D45 锁协议：SQLite rollback-journal 模式的 SHARED / RESERVED / EXCLUSIVE
- SHARED：读事务期间对数据库文件 `flock(LOCK_SH)`；RESERVED：写者对 `<db>-lock` 文件
  `flock(LOCK_EX)`，同时只有一个写者；EXCLUSIVE：提交时对数据库文件 `flock(LOCK_EX)`，等读者结束。
- 用 `flock` 而非 `fcntl` 字节锁：flock 属于“打开的文件”，同进程的两个连接也互斥，关闭一个 fd
  不会误释放另一个连接的锁（POSIX 字节锁的著名缺陷）。进程死亡时操作系统自动释放。
- 提交先拿 EXCLUSIVE 再写 WAL：拿锁超时（`LockTimeout`，即 "database is locked"）时什么都没写，
  事务完整保留、可以重试 COMMIT，不会触发 D37 的“连接作废”。自动提交语句超时则撤销该语句。
- 死锁规避照 SQLite：自动提交的写语句先 RESERVED 后 SHARED；显式事务里已持有 SHARED 时拿
  RESERVED 不等待、直接报 locked（对方写者可能正等我们的 SHARED）。`BEGIN IMMEDIATE/EXCLUSIVE`
  在 BEGIN 时就拿 RESERVED。默认忙等超时 5 秒（`Database(path, timeout=...)`）。
- 崩溃留下的 WAL（“热日志”）：持有 SHARED 时 WAL 仍存在，说明写者已死；谁先拿到 RESERVED 谁在
  EXCLUSIVE 下恢复，其他连接释放 SHARED 后重试（受超时限制）。
- 阶段 13 改成 WAL 模式后读者不再阻塞写者，这里的锁工具、超时和变更计数器会沿用。

## D46 变更计数器与缓存一致性
文件头新增 u32 变更计数器，每次提交加 1。每个读事务开始时（已持有 SHARED）重读文件头，
计数器或页数与缓存不同就清空页缓存并重载 catalog。代价是每条自动提交语句多一次 flock 和
读头页（1 万次点查慢约 30%），换来多个连接/进程看到彼此已提交的数据。

## D47 页校验和与文件格式 2
每页最后 4 字节是前 4092 字节的 CRC32，读页时校验，不符报 `DatabaseError("database disk image
is malformed ...")`；页解码时的任何异常也统一转成这个错误。B+ 树可用空间相应变为 4092 字节。
文件格式版本升为 2（magic "MiniDB format 2"），旧文件明确报 "unsupported MiniDB file format"。
`integrity_check()` 会读遍所有已提交页校验 checksum，因此空闲页上的损坏也能发现。

## D48 目录 fsync
创建数据库文件、创建和删除 WAL 后 fsync 所在目录，保证文件的出现/消失本身也持久。
每次提交多两次 fsync（自动提交写入慢约 50%）。

## D49 SELECT 编译一次、可多次运行
SELECT 改为先编译成 `CompiledSelect` / `CompiledCompound`（作用域、连接计划、所有闭包），
`run()` 才读数据。相关子查询在外层每行调用一次 `run()`，不重复编译。作用域有父链：子查询里找
不到的列去外层找，引用外层列编译成“读外层当前行”的闭包——外层调用子查询前把当前行放进
自己作用域的 `cell`。聚合查询里传入的是分组行，所以 HAVING/SELECT 里的相关子查询也正确。
不相关的子查询（整条链上没有外层引用）只算一次并缓存。

## D50 子查询的语义细节（均对照 sqlite 实测）
- 标量子查询取第一行第一列，无行为 NULL，多列报 "sub-select returns N columns - expected 1"；
  其亲和性取结果列表达式的亲和性。
- `x IN (SELECT y ...)` 的亲和性与 `IN (列表)` 不同：两边都是列时有数值亲和性就用数值，否则不转换；
  只有一边有亲和性就用那一边；得到的亲和性同时作用于左值和子查询的每个值。子查询为空时结果为 0
  （即使 x 是 NULL）。
- 含子查询的 WHERE 合取项视为引用了当前查询的所有表：放在最后一层过滤，也不用来选访问路径。
- FROM 里的子查询（derived table）每次运行时物化，看不到同一 FROM 的其他表（没有 LATERAL），
  但可以引用更外层的查询；它没有 rowid。

## D51 复合查询
`UNION [ALL]`、`INTERSECT`、`EXCEPT` 同一优先级、从左到右。去重类运算的结果按行排序，
同一行出现多次时保留后出现的那个（与 SQLite 的临时索引行为一致）。ORDER BY 只能引用结果列：
序号、任一分支的结果列名/别名、或与某分支的结果表达式完全相同，否则报
"ORDER BY term does not match any column in the result set"。

## D52 CAST 与 CASE
CAST 的类型名按 SQLite 规则取亲和性（含 INT→INTEGER；含 CHAR/CLOB/TEXT→TEXT；含 BLOB 或空→BLOB；
含 REAL/FLOA/DOUB→REAL；其余 NUMERIC）。转 INTEGER 取文本的整数前缀并在 64 位边界饱和；
转 NUMERIC 时文本按数值前缀解析、整数值的 REAL（仅来自文本）变 INTEGER。MiniDB 没有 BLOB，
CAST 到 BLOB 报 `NotSupportedError`。CAST 表达式带目标类型的亲和性（影响比较）。简单 CASE 的
比较照 `=` 的亲和性规则，NULL 永不匹配。

## D53 JOIN USING / NATURAL
转成 `左表.c = 右表.c` 的 ON 条件；右表的这些列只能用限定名访问，因此 `c` 与 `*` 都指左表的列
（LEFT JOIN 时左表的值就是非 NULL 那一侧）。列不在两边时报与 sqlite 相同的错误。

## D54 预编译计划（prepared plan）
参数不再代入语法树，而是编译成读取执行器参数数组的闭包；SELECT/INSERT/UPDATE/DELETE 编译出的
计划挂在（按 SQL 文本缓存的）语法树上，带着 catalog 的 schema 版本号，版本变了（任何 DDL、
重载 schema——包括 ROLLBACK 和别的连接的改动）就重新编译。不相关子查询的“只算一次”缓存
在每次执行前清空。代价：计划持有 TableInfo/BTree 对象，所以所有 schema 变化都必须 bump 版本。

## D55 排序：单次复合键 + top-k；索引顺序免排序
ORDER BY 改为一次 `sorted()`，DESC 用反向比较的包装对象；有 LIMIT 时用 `heapq.nsmallest`
（等价于 sorted()[:n]，同样稳定）。访问路径报告自己产出的顺序（rowid，或索引等值前缀之后的列 +
rowid）；ORDER BY 全是第一张表的升序、NULLS FIRST 普通列且与之吻合时不排序，LIMIT 满足即停止
扫描；有 LIMIT 且没有别的可用访问路径时，规划器沿 ORDER BY 第一列的索引顺序扫描。

## D56 覆盖索引
作用域记录查询用到的每个 (表, 列)；plan_joins 先编译所有条件再选访问路径。若某表用到的列都在
所选索引里（外加 rowid），就直接用索引 key 组装行，不回表；EXPLAIN 显示 COVERING。只用于 SELECT。

## D57 B+ 树 key 溢出（取消索引 key 长度上限）
超过 capacity/8 的 key 存进溢出页链，cell 里只放长度和首页号；内存里节点同时保存完整 key 和
`key_refs`。每条链只属于一个 cell：叶子 key 复制成分隔键时复制一条链，分隔键消失时释放。
没有采用“截断前缀 + 回表复核”：那样索引顺序不再精确，会破坏索引顺序免排序、覆盖索引和 UNIQUE
判断。变异测试验证了“共享链”和“漏释放”都会被测试发现。

## D58 记录格式 3
每列一个类型码（1/2/4/8 字节整数、0 和 1 零字节、短文本长度编码在类型码里、长文本长度放在头部）。
每种不同的头部只编译一次成 `struct.Struct` + 生成的组装函数，解码一行就是一次 C 调用：10 万行
解码 0.28 s → 0.03 s，文件 23.6 MB → 15.7 MB。文件格式升到 3（旧文件明确报不支持）。
计划里的“按需解码列”没有单独做：实测整行解码已只占全表扫描约 1/4，按列解码最多再省 0.02 s/10 万行。

## D59 语句 journal 的页副本保留 size
一直以来 `pager.write()` 保存页副本时走构造函数、逐 cell 重算 size（每条语句 O(页内 cell 数)），
是写入的最大热点；副本直接带上 size 后，3 万行带参数插入 + 建索引从约 0.85 s 降到 0.39 s。

## D60 WAL 模式取代阶段 7 的重做日志
提交只向 `<db>-wal` 追加页帧（帧头：页号、提交帧时的页数否则 0、generation、链式 CRC32），
最后一帧是提交帧，fsync 日志即完成；数据库文件只由 checkpoint 写。读事务开始时取“最后一个提交帧”
为快照，读页先找快照内该页最新的帧，否则读数据库文件——读者不阻塞写者，写者也不等读者，
同一读事务内看到一致快照（快照隔离）。帧有效的条件是 generation 与链式校验和都对上，因此崩溃
留下的半截尾巴、磁盘上翻转的字节都会让该点之后的帧失效，结果总是“某个提交之后的完整状态”。
取代阶段 7 的“写日志→写数据文件→删日志”：同样原子，但提交少一次 fsync（数据文件），读写并发。
日志文件在最后一个连接关闭时 checkpoint 后清空（文件保留，大小 0）。

## D61 写者与快照
写者先拿 RESERVED（单写者）。自动提交的写语句先拿 RESERVED 再开读快照，所以快照一定最新；
显式事务里先读后写时，若快照之后已有别人提交，则不能开始写（报 "database is locked"，
对应 SQLite 的 SQLITE_BUSY_SNAPSHOT），只能回滚重来。

## D62 steal：大事务溢出到日志
事务内每条语句结束时若脏页超过 1000 页，就把它们作为“未提交帧”追加到日志并清出缓存（缓存限
2000 页），自己读时能看到自己的未提交帧；ROLLBACK 把日志截回上一个提交帧。溢出只发生在语句
边界，因此语句回滚的前像依然有效。下一个写者追加前会截掉崩溃写者留下的未提交帧（链式校验和本已
使它们失效，截断是为了不留死数据）。

## D63 checkpoint
把日志里每页已提交的最新版本拷进数据库文件、fsync、再把日志截为 0。需要 EXCLUSIVE（没有任何读者）
且只尝试、不等待：忙就跳过，日志继续增长，下次再试。每次提交后若日志达到 1000 帧、以及连接关闭时
尝试。checkpoint 中途崩溃没有关系：日志仍在，读者照样从日志读，下次重做即可（幂等）。已知限制：
一直有读者时日志会持续变长（SQLite 用共享内存里的读者标记做部分 checkpoint，这里没有实现）。

## D64 优化器：代价模型、ANALYZE、连接顺序、OR/IN
- 每个访问路径估计 (行数, 代价)，取代价最小者；代价以“访问行数”为单位，B+ 树查找 4，回表 2。
- `ANALYZE` 把表行数和各索引每个前缀的“平均每个值多少行”存为 schema 里的 stat 条目；没有统计时
  照 SQLite 的默认：表至少 100 行（否则用 B+ 树扇出估算），索引等值约 10 行，范围条件保留 1/4。
- `col IN (...)` 走索引时逐值查找再按 rowid 合并；OR 的每一项都能走 rowid/索引时做多路查找并集。
- 内连接（没有 LEFT JOIN）按代价重排顺序：6 张表以内穷举排列，更多时贪心；每张表在“已绑定表集合”
  下的访问路径只规划一次。代价 = Σ 外层行数 × 本层单次代价；只起过滤作用的条件按 1/4 估算。
- 这些都只影响性能：WHERE 仍对每个候选行完整求值，fuzz 中加入了 ANALYZE 语句来切换计划。

## D65 与 SQLite 求值时机对齐
- 多行 `INSERT ... VALUES` 先算完所有行再插入（SQLite 把其中的常量子查询在语句开始时算一次）。
- 被 SQLite 解析器折叠成 0 的 `X AND 0` 不再编译其操作数，因此其中不存在的表/列不会报错。

## D66 类型注解
每个模块用 `from __future__ import annotations`（注解不在运行时求值，零开销，也能前向引用）。
层与层之间传递的数据用别名说明：`SQLValue`、`Row`、`RowFunction`（编译后的表达式）、`Record`、
`OrderTerm`、`Bound`；页、键编解码器、访问路径用 `Protocol` 描述（结构化类型，不需要改继承关系）。
不引入 mypy（只用标准库）：`tests/test_annotations.py` 用 `typing.get_type_hints` 解析每个函数和
方法的注解，缺注解或引用了不存在的名字都会失败。注解只做文档与静态检查，不做运行时校验。

## D67 覆盖率只用标准库
`tools/coverage.py` 在 `trace.Trace` 下运行 pytest，用 `ast` 找出每个模块的语句行（去掉文档字符串）
做分母。只统计测试进程本身，子进程（多进程并发、CLI 测试）里执行的代码不计入，所以 CLI 入口另有
进程内测试。剩下未覆盖的行记录在 PROGRESS.md，均为防御性分支。
标准库 `trace` 的忽略列表按“模块短名”缓存判断结果，所有包的 `__init__.py` 共用 `__init__` 这个名字：
先遇到的第三方包 `__init__` 被忽略后，我们的也被忽略。因此换成只按路径判断的忽略对象（只跟踪 `minidb/`）。

## D68 多 Python 版本与参考行为
- 支持 Python 3.11–3.14，CI 全部跑。对照参考是 Python 3.12+ 的 sqlite3：3.11 没有
  `connect(autocommit=...)`，那里用 `isolation_level=None` 代替做与事务无关的对照，两组事务对照跳过。
- 命名占位符用序列传参：3.12/3.13 的 sqlite3 只警告，3.14 报 `ProgrammingError`。MiniDB 采用 3.14
  的规则（已废弃的行为不再模仿），3.14 以下的测试改为断言 MiniDB 的报错。
- pytest 把警告视为错误（`filterwarnings = ["error"]`），能发现没关闭的文件和连接。测试里用到的
  `Pair` 登记在一个集合里，由 conftest 的 autouse fixture 在每个测试后关闭（用强引用集合：弱引用在
  测试函数返回时就被回收，连接没关闭就被释放，警告依旧）。

## D69 CI
GitHub Actions（`.github/workflows/tests.yml`），ubuntu-latest：3.11–3.14 矩阵跑全部测试并打印
参考 SQLite 版本；fuzz 作业跑固定种子、文件模式种子，以及按 `GITHUB_RUN_NUMBER` 每次换一批的新种子
（失败时日志里有种子号，可在本地复现）；每周定时运行一次，没有提交也能继续找新种子。

## D70 标准答案固定为 sqlite.org 发布的 SQLite 3.53.4（不带 ICU）
第一次在 CI（Ubuntu，系统 SQLite 3.45.1）上运行时对照测试大量失败，查下来本地的“标准答案”本身有问题：
本地 Python 来自 conda-forge，它的 SQLite 编译时开了 `SQLITE_ENABLE_ICU`（`upper`/`lower`/`LIKE`
按 Unicode 处理大小写），`SQLITE_MAX_VARIABLE_NUMBER` 也改成了 250000。MiniDB 此前对齐的就是这些
非默认行为。处理：
- `tools/reference_sqlite.py` 下载固定版本的 amalgamation（校验 SHA3-256），用默认选项（外加
  `SQLITE_ENABLE_MATH_FUNCTIONS`，与官方 autoconf 构建一致）编译成动态库，再用
  `LD_LIBRARY_PATH`/`DYLD_LIBRARY_PATH` 让 Python 的 `sqlite3` 加载它。本地和 CI 用同一个脚本。
  macOS 的 SIP 让 `/usr/bin` 下的程序（如 perl）启动时丢掉 `DYLD_*` 变量，经由它们启动 Python 时
  要再用 `env` 设置一次。
- `tests/sqlcompare.py` 发现链接的 SQLite 带 ICU 就直接报错；pytest 头部显示参考版本，不是
  3.53.4 时提示（不同版本在常量折叠等边角上确有差异，例如 3.45 不把 `ORDER BY (2 IS NULL)` 当列号）。
- MiniDB 改为官方默认行为：大小写只认 ASCII 字母——`upper`/`lower`、`LIKE` 的忽略大小写、以及
  关键字、标识符、类型名的比较（`values.ascii_lower/ascii_upper`）。这也修掉了一个与 ICU 无关的
  差异：以前表 `É` 和 `é` 被当成同一张表，`ſelect`（长 s）被当成 `SELECT`。
- 参数编号上限改为默认的 32766。

## D71 常量条件在循环开始前测试一次
SQLite 把不引用本层 FROM 中任何表、且不含子查询和非确定函数的 WHERE 项（包括内连接的 ON 项）在进入
循环前求值一次，假或 NULL 就整个跳过循环（也就不会去算 FROM 子查询或行上的表达式）；这样的项出错时，
即使表是空的也会报错。MiniDB 以前把它们放在第一层逐行求值，并且先物化 FROM 子查询，fuzz 因此发现
`... WHERE (+(y) IS NULL) AND 0 ...`（整体折叠为 0）时 MiniDB 报了 SQLite 不会报的溢出错误。
现在 `plan_joins` 把这些项单独返回，SELECT、UPDATE、DELETE 在物化子查询和进入循环之前测试它们。
（没有被 SQLite 展平的 FROM 子查询，SQLite 可能先物化再测试；这种情况下出错时机仍可能不同。）

## D72 sqllogictest 作为外部基准
sqllogictest 是 SQLite 自己的、与引擎无关的测试集（约 600 万条记录，期望结果由 SQLite 产生并与
其他数据库交叉核对），用例不是我们写的，能暴露我们没想到的 SQL 写法。
- 语料约 1 GB，不入库。sqlite.org 的 Fossil 只对登录用户提供 tarball / raw 下载（匿名登录要过验证码），
  所以从 git 镜像 `github.com/gregrahn/sqllogictest` 下载，固定到与 Fossil trunk check-in db57eba95d
  （2026-04-15）对应的提交；镜像不是官方的，因此 `tools/sqllogictest.sha3` 固定每个文件的 SHA3-256，
  `--fetch` 逐个核对。
- runner 按官方 C runner 的规则格式化结果（`I` 按 `sqlite3_column_int64` 转换、`R` 用 `%.3f`、
  `T` 里非可打印 ASCII 变 `@`、空串 `(empty)`），`rowsort`/`valuesort` 按格式化后的文本排序，
  超过 `hash-threshold` 比较 MD5。按 `sqlite` 引擎处理 `skipif`/`onlyif`。
- 缺一个功能时，文件开头的 `CREATE TABLE` 失败，后面每条都报 `no such table`，所以报告按“每个文件的
  第一个失败”统计根因；错误结果和崩溃单独列出（这些不是缺功能而是 bug）。
- CI 用 `--min-passed` 防止通过数倒退；每补一个功能就调高基线。

## D73 变形测试（TLP、NoREC）
差分 fuzz 依赖 sqlite3 给答案；TLP / NoREC（SQLancer）只靠 MiniDB 自己：同一查询的两种写法必须一致，
与存储、索引、计划无关，专门针对优化器（访问路径、多路索引、连接重排）。
- 查询出错就跳过：规划器可以合法地不在某些行上求值谓词（例如索引范围已经排除了这些行），
  所以一边出错另一边不出错不算不一致。约 15% 的查询因此跳过。
- 比较多重集时区分 1 和 1.0；DISTINCT、GROUP BY、MIN/MAX 保留相等的 1 / 1.0 中哪一个取决于访问顺序，
  这些比较按数值相等。REAL 的 SUM 与加法顺序有关，不比较。
- 只用随机字面量时，条件很少恰好落在表中已有值上，rowid 下界的差一变异 60 个种子只发现 2 个；
  加入“列 比较 表中实际存在的值”的条件后发现 33 个。
