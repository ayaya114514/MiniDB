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
（已被 D80 取代：现在与 SQLite 逐位一致。以下为当时的记录。）
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
- （阶段 18 起所有锁改为 `<db>-shm` 上的 fcntl 字节锁 + 进程内登记表，见 D93。）

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
（阶段 18 已由 D92 取代：读者标记、部分 checkpoint、写者重启日志。）

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

## D74 任意类型名与五种亲和性
以前列类型只能是 `INTEGER` / `TEXT`，sqllogictest 的 622 个文件里有 225 个第一条 `CREATE TABLE`
就因 `FLOAT` / `VARCHAR(n)` 失败。现在类型名照 SQLite 语法：若干个词加可选的 `(n)` / `(n, m)`，也可以
省略；亲和性按 SQLite 的子串规则（`values.type_affinity`，原来只给 CAST 用）：
- 存储：INTEGER / NUMERIC 把像数字的文本转成数（整数优先），REAL 在此基础上把整数转成 REAL，
  TEXT 把数转成文本，BLOB（含无类型）不转换。SQLite 的 REAL 列在磁盘上可能存整数、读出时再转 REAL，
  MiniDB 直接存 REAL，外部看到的值相同。
- 比较：三种数值亲和性行为相同（都对另一侧应用 NUMERIC）；BLOB 列算“有亲和性”，
  所以 TEXT 列与 BLOB 列比较时两边都不转换（与“无亲和性”的表达式不同）。
- 只有类型名恰好是 `INTEGER` 的主键是 rowid 别名；`INT PRIMARY KEY`、`BIGINT PRIMARY KEY` 是普通列
  （带自动唯一索引），与 SQLite 相同。
- 类型名存大写、空白规范化（catalog 重新生成 CREATE 语句）；BLOB 值本身还不支持（阶段 16 后面做）。

## D75 FROM 里的括号连接按左结合展开
`FROM (a CROSS JOIN b)` 在语料中出现约 4,600 次，全部位于 FROM 的第一项。连接左结合，所以第一项的括号
可以直接去掉；在后面、且与前面用逗号或不带条件的内连接相连时，`x, (a LEFT JOIN b ON p)` 也等价于
`x, a LEFT JOIN b ON p`（p 只引用组内的表，内连接的 ON 只是过滤）。`x LEFT JOIN (a JOIN b)`、
外层带 ON / USING / NATURAL 的情况展开后语义会变，直接报 `NotSupportedError`，不给错误结果。

## D76 视图
- 存在 schema 里（type = 'view'，rootpage = 0，sql 保留原文），与表、索引共用命名空间；打开时重新解析。
- FROM 中的视图编译成一个看不到外层查询的子查询（`DerivedSource`），和 FROM 子查询一样先物化再扫描；
  没有做视图展平（flattening），所以视图上的条件不会下推到底层表的索引——语料里视图查询的规模下够用，
  性能问题留到阶段 17。
- 与 SQLite 一致：创建时不检查 SELECT（可以引用还不存在的表，声明的列数不对也要到使用时才报错），
  使用时缺表报 `no such table: main.x`；自引用报 `view v is circularly defined`；
  视图不能增删改、不能建索引、不能带参数；DROP TABLE / DROP VIEW 用错对象时给出 SQLite 的提示。
- 子查询和视图的重复列名照 SQLite 加 `:1`、`:2` 后缀（SQLite 在第 4 个以后改用随机数，不模仿）。

## D77 冲突处理、UPSERT、RETURNING
- 冲突策略按语句生效（`INSERT/UPDATE OR ...`；列级 `ON CONFLICT` 子句不支持）。检查顺序同 SQLite：
  NOT NULL → UPSERT 目标（按子句顺序）→ rowid → 其余 UNIQUE 索引（新建的在前）。REPLACE 删除冲突行后继续
  检查；因为没有 DEFAULT，NOT NULL 遇 REPLACE 仍报错。FAIL 保留本语句已做的修改（自动提交时也提交），
  ROLLBACK 结束事务；异常对象带 `resolution` / `changes`，由 `Database` 处理。
- UPSERT 目标必须对应 rowid 或某个 UNIQUE 索引的列集合。SQLite 的怪癖：包含 INTEGER PRIMARY KEY 的
  UNIQUE 索引永远匹配不上（解析后目标里的 id 是 rowid，索引里的 id 是普通列号），照做。
- `excluded` 行：列没有亲和性；值是否已按列亲和性转换，取决于 SQLite 发现冲突时是否已经检查过某个索引
  （它在检查第一个索引时就地转换寄存器）——rowid 冲突通常先检查，所以看到原值，但 REAL 列的整数已被
  `OP_RealAffinity` 转成 REAL；`excluded.<INTEGER PRIMARY KEY>` 是最终 rowid。这些都由 fuzz 发现，逐条
  对照 SQLite 确认。
- RETURNING 在修改每行时求值，全部修改完成后返回。DB-API 的 `rowcount` 模仿 sqlite3：取完最后一行才出现。

## D78 语句日志：事务内出错时哪些修改保留
SQLite 只在“语句可能写多行（多行 VALUES 或 SELECT）且可能中止”时开语句日志（statement journal）；
“可能中止”= 有按 ABORT 处理的约束检查，或调用了非内联函数（`sqlite3VdbeAddFunctionCall` 会调用
`sqlite3MayAbort`；coalesce/ifnull/iif 等内联函数与聚合不算）。没有语句日志时，事务内非约束错误
（如 datatype mismatch）留下已写入的行，且不计入 total_changes / changes()。MiniDB 在编译计划时按同样
规则算出 `statement_journal`，`Database` 据此决定回滚语句还是保留。自动提交模式下整个事务回滚，不受影响。

## D79 BLOB 与非 UTF-8 文本
BLOB 用 `bytes`，记录格式新增类型码 9（旧文件不受影响），排序 NULL < 数 < 文本 < BLOB。BLOB 转文本按 UTF-8
解读，非法字节用 surrogateescape 保留为孤立代理字符，转回 BLOB 得到原字节；这类文本比较、排序、索引按
UTF-8 字节序（`values._text_key`：非 ASCII 文本转成“每字节一个字符”的串，索引键读回时用
`plain_value` 还原），与 SQLite 的 memcmp 一致。DB-API 遇到这类文本像 sqlite3 一样报解码错误；对照测试
给 sqlite3 设 surrogateescape 的 text_factory，在引擎层面比较。SQLite 的 C 字符串行为也照搬：LIKE、
length()、printf 的 %s 等在 NUL 处截断；数值转换遇到含 NUL 的文本得到 REAL；列亲和性只看 NUL 之前部分。

## D80 浮点与文本转换逐位对齐 SQLite 3.53（取代 D20）
D20 记录的“REAL→TEXT 末位与 SQLite 不同”已解决：直接读参考 SQLite 3.53.4 的源码，把 `sqlite3FpDecode`、
`sqlite3Fp2Convert10`、`sqlite3Fp10Convert2`（改编自 rsc/fpfmt，64/128 位整数运算）和 `sqlite3AtoF` 移植到
`minidb/fp.py`。REAL→TEXT 是 `printf('%!.17g')`，在一串 9 或 0 能缩短且可精确转回时缩短；TEXT→REAL 只用前
约 19 位有效数字。10 万个随机 double 与 5 万个最长 30 位的字面量逐位一致。printf（`minidb/printf.py`）同样
按 `sqlite3_str_vappendf` 源码移植。先前凭记忆写的版本（双倍精度 dekker 乘法）与 3.53 不符，是读源码才发现的
——教训：对照对象有源码时，直接读源码而不是凭记忆。

## D81 日期时间函数
`minidb/dates.py` 逐段移植 date.c：C 的整数除法/取余向零截断用 `_div`/`_rem`，修饰符上限按 C float 取值，
格式化走 printf 移植。'now' 在一条语句内固定（执行器在每条语句开始时清空缓存）。localtime/utc 用 Python 的
`time.localtime`，与 SQLite 在同一时区下结果一致（未在其他时区的机器上验证）。

## D82 一元负号即 0 - X
SQLite 把非数字字面量的 `-X` 编译为 `0 - X`，所以 `-(c)` 永远得不到 -0.0（`atan2(0, -c)` 对 c = 0.0 是 0.0）。
只有紧跟数字字面量的负号生成负常量（`-0.0` 仍是 -0.0）。

## D83 UPSERT 的 DO UPDATE 只在可达时解析
SQLite 在生成冲突检查代码时才解析 ON CONFLICT 的 SET/WHERE：rowid 冲突只在 INSERT 显式给出 rowid
（INTEGER PRIMARY KEY 列或 `rowid`/`oid`/`_rowid_`）时检查，每个 UNIQUE 索引各检查一次，由第一个目标匹配
（或无目标）的子句处理。一个子句若没有任何检查能到达，其中写错的列名不报错。`PreparedUpsert.resolve()`
按同样的可达性延迟编译。无目标的子句必须是最后一个，否则是语法错误（与 SQLite 的文法一致）。

## D84 RIGHT / FULL JOIN
嵌套循环不变，RIGHT/FULL 级记录匹配过的行号；主循环结束后，按级别从左到右对每个 RIGHT/FULL 表扫一遍，
没匹配过的行配上前面各表的 NULL，再接着跑后面的级别（后面 RIGHT 级的匹配也在这一轮里记录）。
语义上 WHERE 条件和 RIGHT JOIN 之后的内连接 ON 不能下推到 RIGHT 级之前（否则会改变“哪些行没匹配”）：
每个条件有一个最低级别；RIGHT 级之前的常量 ON 条件只过滤它所在的连接，不再作为整个查询的常量。
有外连接时不重排表；有 RIGHT/FULL 时不用索引顺序免排序（未匹配行最后才出）。

USING 列按 SQLite 的 lookupName：内连接/LEFT 后未限定名指最左表；RIGHT 后指右表（左表的同名列被遮蔽）；
FULL 后是 `coalesce(...)`，在行尾占一个计算 slot（`Merge`），在该级加载后算出。`*` 对 RIGHT JOIN 左边、且
名字出现在后面某个 USING 里的列用未限定名展开（`a.*` 也是）。查询里有 RIGHT/FULL 时，USING 条件的左侧是
所有左表同名列的 coalesce（除第一个外都必须来自 USING，否则 "ambiguous reference"）。这些 coalesce 取第一个
参数的亲和性（SQLite 的 `SQLITE_AFF_DEFER`）。

## D85 比较亲和性作用于两侧
SQLite 的比较 opcode 把比较亲和性施加到两个操作数上（TEXT 只在至少一侧是文本时才把数字转文本），而不是
“只转换另一侧”。对普通列两者等价（列值已有该亲和性），但对值不一定符合亲和性的表达式（UNION 子查询的
列、上面的 coalesce）不同。`value_comparator` 改为两侧都转换，用 `type(x) is str` 快速跳过，benchmark 无明显变化。

## D86 结果列别名
SQLite 允许 WHERE、ON、GROUP BY、HAVING、ORDER BY 的表达式里用结果列别名（非标准扩展）：名字先按 FROM 的列
解析，找不到才看别名（别名优先于外层查询的列）；结果列表本身看不到别名。`CompiledSelect` 在编译完结果列
之后把这些子句里的别名引用替换成被引用的表达式；子查询里引用外层别名时由 `Scope.resolve` 在逐层查找中识别
（`AliasReference`），在外层 scope 编译该表达式、用外层当前行求值。

已知差异：SQLite 的 WHERE 常量传播会把 `x = 常量` 代入其他 AND 项，使其变成语句开始时只算一次的常量，于是
`abs()` 的整数溢出这类错误可能在没有任何行时也报出；MiniDB 按行求值。只影响报错时机（fuzzer 给 abs 加了 guard）。

## D87 外层查询的聚合、聚合查询的判定
按 SQLite 的 resolve.c：聚合调用属于其参数用到的最内层查询（参数只用外层列时属于外层；不用任何列时属于
当前层）。`Compiler.aggregate_depth` 解析参数里的列得出层数；属于外层的调用登记到那一层的
AggregateCollector，在子查询里通过外层的 `cell`（当前分组行）读取结果，中间各层标为相关子查询。
只有 GROUP BY 或结果列里（含子查询中）属于本层的聚合才使查询成为聚合查询——HAVING 和 ORDER BY 里的
不算（与 SQLite 相同：`SELECT a FROM t ORDER BY count(*)` 报错）。结果列编译到一半才发现外层聚合时抛出
`NeedsAggregate`，该层按聚合查询重新编译。报错位置和措辞随子句（`Scope.phase`）而定：WHERE/ON 里在非聚合
查询中是 "misuse of aggregate function"，在聚合查询中是 "misuse of aggregate:"；GROUP BY 里是
"aggregate functions are not allowed in the GROUP BY clause"；聚合参数里的子查询不能再引用同层聚合。
参数里含子查询的聚合一律算作当前层（SQLite 会看子查询里的列，这是简化）。

已知差异：SQLite 在 WHERE 里把 `x OR 5` 这类恒真表达式折叠掉后，不再为其中的子查询生成代码，因此不会
报其中外层聚合的 misuse 错误；MiniDB 照常报错（fuzzer 只在聚合查询的结果列里生成外层聚合）。

外连接的 ON（FROM 中有 RIGHT/FULL 时任何 ON）不能引用它右边的表，报 "ON clause references tables to its
right"；含子查询的 ON 先试编译一次，收集解析到本层的表（`Scope.watch`）。

## D88 窗口函数：照搬 SQLite 的执行顺序
SQLite 按窗口的 PARTITION BY + ORDER BY 排序后，用 start / current / end 三个游标在分区缓冲上推进：end 处调用
xStep、start 处调用 xInverse、current 处返回行，何时移动哪个游标由 frame 决定（`sqlite3WindowCodeStep` /
`windowCodeOp`）。滑动 frame 下 sum/avg/total 的浮点结果取决于 xStep 与 xInverse 的交错顺序（KBN 求和），
所以 `minidb/window.py` 不按“每行重算 frame”实现，而是逐步复现这段代码：同样的主循环、flush、peer 判断、
RANGE 的 `windowCodeRangeTest`（含 DESC 时的比较翻转、NULLS 顺序、对非数值不做加减）、IfPos 倒计时。
内置函数也照搬：row_number/rank/dense_rank/percent_rank/cume_dist/ntile 是带固定 frame 的聚合
（`sqlite3WindowUpdate`），first_value/nth_value/lead/lag 直接读缓冲中的行（lag 的负偏移只能看到已读入的行），
滑动 frame 的 min/max 用 (值, 序号) 的“索引”，EXCLUDE 时每行全量重扫并 finalize，group_concat 的 xInverse
按字节删除（包括 SQLite 在剩余值全为空串时返回一个 NUL 字符的怪癖）。不同定义的窗口依出现顺序倒序计算
（SQLite 把后出现的嵌进子查询），最终行序与 SQLite 相同。

语义细节：窗口只能出现在结果列和 ORDER BY；非常量的 frame 偏移按 SQLite 的解析期规则变成 NULL（有行时才报错）；
WINDOW 子句里只有非首个定义在解析时基于前面的定义链接；聚合的 `FILTER (WHERE ...)` 同时支持普通聚合与窗口。
验证：随机窗口对照（全部 frame 类型、EXCLUDE、分区、带并列的 ORDER BY、NULL、混合类型）数百个种子全部逐位
一致（含无 ORDER BY 时的行序）；fuzzer 在单表查询里生成窗口函数（ORDER BY 以 rowid 收尾，避免依赖读行顺序）。

## D89 无索引等值连接用哈希（自动索引）
连接的内层表只能全表扫描、但有 `本表列 = 已连接表的表达式` 时，访问路径改为 `HashLookup`：每次运行连接、
首次探测时把该表的行按列值哈希一次，之后按 key 取行——相当于 SQLite 的 automatic index。哈希键是经过比较
亲和性转换后的 `sort_key`，转换与索引查找相同（列侧会被转换时不可用，见 D85）；派生表的列值不一定符合其
亲和性，所以建表侧也施加同样的转换。每次 `join_rows` 重置，相关子查询每次运行都重建（表可能已被 UPDATE 改过）。
只在最佳路径仍是全表扫描时启用，有索引时仍走索引（不必付建表代价）；key 不引用已连接的表（常量）时不用。
benchmark：10 万行与 200 行无索引表的等值连接 17.2 s → 0.15 s。

## D90 表达式与连接循环生成 Python 源码
运算符（算术、比较、AND/OR/NOT、一元运算）不再是一层层闭包，而是拼成一个表达式的 Python 源码，`compile()`
成一个 lambda：整数的 + - * 在 64 位范围内直接算（否则调用原来的 `values.add` 等，溢出转 REAL 的语义不变），
两侧都是数值的比较直接用 Python 比较（亲和性转换只作用于文本，此时是空操作），AND/OR 保持短路与三值逻辑。
其它节点（函数、CASE、子查询……）仍是闭包，作为辅助函数被调用，它们的子表达式照样走代码生成；嵌套过深时子树
退回闭包，避免生成的源码超过 Python 的嵌套限制。常量不写进源码而放进环境字典，源码只取决于表达式的形状，
按源码文本缓存 code 对象——同形的语句（例如只差一个常量的点查）只编译一次。
只有内连接的计划生成扁平的嵌套 for 循环（每张表一层，过滤条件内联真值判断），代替递归生成器；外连接、
RIGHT/FULL、合并列仍用原来的通用循环。

## D91 VACUUM
在写事务里把整个数据库按 schema 表的键顺序复制进一个新的内存 pager：表按 rowid、索引按键用 `bulk_load`
自底向上装满，schema 表保留原来的键、只换根页号；然后把新 pager 的页对象按相同页号写回（`page_count`
缩小、空闲链表清空），提交后立即尝试 checkpoint。checkpoint 在持有 EXCLUSIVE（没有读者）时把文件截断到
`page_count` 页——有读者时不截断，以后的 checkpoint 再截断（D92 之后：拷贝到哪个提交就按那个提交的
页数截断，不再需要 EXCLUSIVE）。rowid：有 INTEGER PRIMARY KEY 或有任何索引的表保持不变，其余的表按 rowid 顺序重新编号为 1, 2, 3…
（与 SQLite 相同：它的 VACUUM 走 xfer 优化，只在 rowid 可能被引用时保留；`VACUUM INTO` 一律保留）。
最初写成“一律保持不变”，是 fuzz 在阶段 18 发现的（旧对照测试恰好只用了带 UNIQUE 的表）。
`VACUUM INTO 'file'` 把同样的副本写进一个新文件（已存在且非空时报 "output file already exists"）。
VACUUM 不能在事务中执行。崩溃安全沿用 WAL：提交前崩溃是旧库，提交后崩溃是新库，截断发生在 WAL 清空之前。

## D92 读者标记、部分 checkpoint、写者重启日志（取代 D63）
照 SQLite WAL 的 wal-index 做了一个简化版，状态放在 `<db>-shm`（不 fsync；只信任其中与当前日志
generation 相同的部分；打开时若没有别的连接就清零，崩溃后最多是重拷一遍）：
- `backfill`：当前 generation 已拷进数据库文件的帧数。8 个读槽各有一个读者标记 `(generation, 帧数)`。
- 读事务开始：日志已全部拷完（快照 == backfill）就共享槽 0，只读数据库文件；否则占一个标记 ≤ 快照的槽
  （优先共享标记恰好等于快照的槽，否则独占一个空槽写标记后降为共享，否则共享标记最高的那个）。拿到槽后
  若已有更新的提交就重来——拿槽之前 checkpoint 可能已经拷过了我们快照之后的帧。所有槽都忙时等待（超时报 locked）。
- checkpoint 只拷到“所有在用槽的最小标记”，拷贝期间独占槽 0（槽 0 的读者要求数据库文件不变）；按拷到的那个
  提交的页数截断文件。全部拷完且没有任何读者（EXCLUSIVE）时才把日志截为 0。不再需要 EXCLUSIVE 才能拷。
- 读页规则：快照里的帧若 ≤ 当前 backfill，就从数据库文件读（那一页在文件里恰好是这一帧：checkpoint 不会拷
  过我们的标记）。于是日志全部拷完后没有任何读者还需要它——写者在新事务写第一帧前发现全部拷完，就开一个新
  generation 从头覆盖日志（重启），不必等持有旧标记的读者结束。读日志帧（以及扫描日志）时持有 WAL_READ
  共享锁，重启时独占它，防止读到一半被覆盖。旧 generation 的标记仍在用时，新 generation 的 checkpoint
  一帧也不拷（保守）。与 SQLite 的差别：SQLite 只有“读者都在槽 0”时才能重启。
- 写者等待：只靠上面这些，读者若总是跨越提交（每个读事务都比两次提交的间隔长），最小标记永远落后，日志仍
  会无限增长（SQLite 的 checkpoint starvation）。所以日志达到 `checkpoint_frames`（默认 1000）帧而拷不完时，
  写者（持有 RESERVED，期间没有新提交，新读者的标记都是最新）最多等 0.1 秒让旧读者结束，拷完即重启；等不到
  就继续写，并且在日志再增长 4000 帧之前不再等（一个长事务不会拖慢每次写）。
  实测 4 个进程持续读（每个读事务 2ms+）、1 个写者每秒约 4700 次提交：不等待时日志涨到 48224 帧，
  等待时最长 1007 帧，写入吞吐下降约 5%。
- 已知限制：`-shm` 的读写不是原子的多字段更新，依赖小 pwrite/pread 在同一文件上的原子性（Linux/macOS 的
  常规文件满足）；SQLite 用两份头 + 校验和防撕裂读，这里没做。长时间不结束的读事务仍然会让日志变长（等待
  0.1 秒后放弃）。

## D93 锁改为 `-shm` 上的字节锁（取代 D45 的 flock）
读槽要 9 把锁，外加 WAL_READ、SHARED/EXCLUSIVE、RESERVED；flock 只能锁整个文件，意味着十几个旁路文件。
改用 POSIX 记录锁（`fcntl.lockf`）锁 `<db>-shm` 偏移 4096 之后的单个字节，旁路文件只剩 `-wal` 和 `-shm`。
记录锁属于进程而不是打开的文件：同进程两个连接不会互斥，关闭任何一个 fd 会丢掉整个进程在该文件上的锁。
照 SQLite 的 `unixInodeInfo`：每个进程按 (device, inode) 只打开一次 `-shm`（`_LockFile`，连接共享、最后一个
连接关闭时才关），进程内各连接之间由登记表（每个字节的独占者/共享者集合，线程锁保护）仲裁，进程在有连接
持有时才持有 OS 锁。记录锁的升降级是原子的（flock 不是）。进程死亡时 OS 释放锁。已知限制：连接不能跨
`fork()` 使用（子进程不继承记录锁，与 SQLite 相同）；删除正在使用的 `-shm` 会让新旧连接各锁各的。

## D94 崩溃模型：丢失未 fsync 的写入、乱序、半页写
原来的崩溃测试只模拟“进程在某一步死掉”（此前的写入全部生效）。`tests/test_crash_model.py` 模拟断电：记录写者
对数据库文件和日志的每次 `write` / `truncate`，`fsync` 时把文件当前内容记为持久；在第 N 次操作处崩溃后，每个
文件 = 持久内容 + 之后操作的随机子集，每次写入要么丢失、要么完整、要么按 512 字节扇区撕裂（只落下一部分扇区），
截断也可能丢失。工作负载包括事务、VACUUM、手动 checkpoint、持有旧快照的读者（因此有部分 checkpoint 与日志重启）。
重开后必须通过 integrity_check，状态等于最后一个已确认的提交或正在进行的那个（用内存参考库同步执行得到）。
设计上成立的理由：日志帧有 generation 与链式校验和，丢失/撕裂/乱序的帧让该点之后全部失效；数据库文件只在
checkpoint 写，写完 fsync 后才记录 backfill（`-shm`，重开且无人使用时清零，重新从日志读）；日志只在全部拷完并
fsync 数据库文件后才被截断或重启。发现并修正一处：checkpoint 扩展文件的写入被撕裂后，文件大小可能不是页大小的
整数倍，原来直接报“文件损坏”；现在日志里有帧时容忍（这些页都在日志里，下次 checkpoint 重写并截断）。
实测 20 个工作负载 × 40 个随机崩溃点（800 次，3075 个未同步操作被随机丢弃/撕裂）：633 次恢复到已确认状态
（其中 485 次丢掉了进行中的提交），167 次恢复到进行中的提交，0 次错误。已知限制：没有模拟目录项丢失（创建文件时
已 fsync 目录）、没有模拟 `-shm` 的内容（重开时清零）、扇区大小固定 512 字节。

## D95 Windows 锁：msvcrt.locking 字节区间（未在 Windows 上运行）
标准库在 Windows 上只有 `msvcrt.locking`：按句柄、强制性（锁住的字节别的句柄也不能读写）、只有排他锁。做法照
SQLite 在没有 LockFileEx 时的方案：每把逻辑锁是 64 字节的区间，共享 = 锁住区间里任意一个空闲字节，排他 = 锁住
整个区间；因此最多 64 个进程同时共享一把锁（第 65 个拿不到，当作忙）。锁区间在 `-shm` 偏移 4096 之后，不挡数据
读写。进程内仍由 D93 的登记表仲裁（每进程一个句柄）。转换不是原子的：共享时升级直接失败（代码里本来就不会升级），
降级可能在中间被别的进程抢走——`downgrade_slot()` 返回 False，调用方放弃该槽重试。`pread`/`pwrite` 在 Windows
上退化为 `lseek` + `read`/`write`（有互斥锁保护的地方才共享文件位置）。
锁层拆成 `PosixLocks` / `WindowsLocks` 两个后端。本机（macOS）无法运行 Windows：`tests/test_locking.py` 用一个
按 `msvcrt.locking` 语义实现的假模块（按句柄、重叠即失败、解锁必须匹配）测试 Windows 后端的共享/排他/降级，并在
这个后端上跑读者标记和并发测试。CI 增加了 `windows-latest` 任务，只跑不依赖参考 SQLite 版本的存储/并发/崩溃测试
（Windows 上编译参考 SQLite 需要 C 工具链）。2026-10-01 第一次推送后在真实 Windows 上运行：320 个测试通过
（第一次运行唯一的失败是一个依赖线程调度的断言，Linux 3.14 上同样失败，已改为检查“日志确实重启过”）。

## D96 UPSERT 的 excluded 看到被前一行转换过的默认值
SQLite 把列的（常量）DEFAULT 在每条 INSERT 里只计算一次，直接放进构造行的寄存器（`sqlite3ExprCodeRunJustOnce`）；
而列亲和性是原地作用于这些寄存器的——第一次查索引时（OP_Affinity）或生成记录时（OP_MakeRecord）。所以一旦
语句中某一行走到了这一步，之后各行 upsert 的 `excluded` 里的默认值就是转换过的（`VARCHAR DEFAULT -1.5` 变成
文本 `'-1.5'`），而第一行冲突时看到的是原值。显式给出的值每行重新计算，不受影响。MiniDB 用 `DefaultRegisters`
记录“是否已转换”照做（D83 的 raw excluded 之上）。文件模式 fuzz 种子 9791 发现。

## D97 BETWEEN 也能走索引
SQLite 的 `exprAnalyze` 把 `x BETWEEN a AND b` 额外拆成两个虚拟项 `x >= a`、`x <= b` 供索引规划使用，原 BETWEEN
仍然照常求值。`find_constraints` 现在同样展开（NOT BETWEEN 不展开），所以 `age BETWEEN 30 AND 35` 走
`SEARCH USING COVERING INDEX ... (age>=? AND age<=?)`、主键上走 rowid 范围，计划选择与 SQLite 一致。结果不受影响
（过滤仍按 BETWEEN 求值），比较亲和性与两个比较相同。在做 Playground 示例时发现。

## D98 解析期的 AND 折叠、TRUE / FALSE、IS TRUE、多行 VALUES 的直接编码
fuzzer 加入 TRUE / FALSE 字面量后发现的一组与 SQLite 解析/解析名字时机相关的行为，逐条照搬：
- `sqlite3ExprAnd`：解析时只要一侧是整数字面量 0（`0`、`(0)`、`0x0`，不含 `-0`、`0.0`、`'0'`、FALSE）且两侧都没有
  函数调用（LIKE/GLOB 算函数，子查询里的不算），整个 AND 就变成整数 0——在名字解析之前，所以另一侧可以引用不存在的列
  而不报错。原来只在编译期折叠，规划器的预处理（`tables_referenced` 等）仍会去解析那些名字；现在在 `Parser.and_expr`
  里折叠（`fold_and`），且层层传递（`(1 AND 0) AND nope` → 0）。
- TRUE / FALSE 在表达式里是名字：有同名列时是列，否则是 1 / 0。编译器早就这样做，但规划器的几处（连接条件放置、索引约束、
  ORDER BY 走索引）直接解析列名，`SELECT ... FROM t WHERE FALSE` 都会报 "no such column: FALSE"——长期存在的 bug，
  fuzzer 以前不生成 TRUE/FALSE 所以没发现。统一用 `is_true_false()` 判断。
- `x IS [NOT] TRUE/FALSE`（右侧是解析成常量的 TRUE/FALSE）是真值测试（SQLite 的 TK_TRUTH / OP_IsTrue）：`48 IS NOT TRUE`
  为 0，`NULL IS TRUE` 为 0，`NULL IS NOT FALSE` 为 1，而不是与 1 / 0 比较。
- 怪癖：多行 VALUES 的第二行起，SQLite（`sqlite3MultiValues`）在可以时把行直接编码进协程、不经过名字解析，于是其中的
  `x IS TRUE` 不会变成真值测试，就是 `x IS 1`（`VALUES (1, 2), (2, 5 IS NOT TRUE)` 第二行是 1）。条件逐项实测：语句里此前
  出现过 WITH 则不用；该行必须是常量（无列名，TRUE/FALSE 除外；无子查询；函数须是确定性的标量函数——random、changes、
  last_insert_rowid、聚合不算，日期函数算）；若前一行是普通（解析过的）行，它也必须是常量且顶层没有 CAST（有亲和性）。
  `Parser.value_rows` 照此把这些行里的 `IS TRUE/FALSE` 改写成与 1 / 0 的普通比较。
- 已知差异：VALUES 行里的聚合函数（SQLite 里回退成无 FROM 的 SELECT，`VALUES (count(*))` 合法）MiniDB 报 misuse，未处理。

## D99 浏览器 Playground
- Pyodide（v314.0.7，从 jsDelivr 加载）在 Web Worker 里运行，界面不卡顿；worker 必须是 ES module（`type: "module"`，
  `import { loadPyodide } from ".../pyodide.mjs"`）：314 版在 classic worker 里 `importScripts` 后 `loadPyodide()` 永远不返回
  （浏览器里实测）。数据库是内存数据库，SQL 和数据不离开浏览器（只从 CDN 下载 Pyodide 本身）。
- MiniDB 源码打成 `minidb.zip` 和 `bridge.py` 一起解压进 Pyodide 的文件系统；`bridge.py` 只做三件事并返回 JSON：
  按顶层 `;` 拆分并逐条执行（遇错停止，查询/UPDATE/DELETE 附带 EXPLAIN 的计划）、列出表和索引、按层导出 B+ 树
  （每层最多 48 个节点，每个节点显示前 3 个和最后 1 个 key、填充率）。构建脚本把 `__BUILD__` 换成内容哈希，避免浏览器缓存旧文件。
- 界面按用户偏好：只有暗色、无顶栏和宣传文案，名字 + 一句说明，左边编辑器与逐条结果（计划显示在结果上方），右边 B+ 树
  （绝对定位布局：叶子等距排开，父节点居中于子节点之上，SVG 画父子连线和叶子兄弟链，初始滚动让根节点可见）。
- `tests/test_playground.py` 在本机 Python 上跑 `bridge.py` 和 `app.js` 里的每个示例，避免 MiniDB 的改动悄悄弄坏 Playground。
- 部署：`.github/workflows/pages.yml`（configure-pages / upload-pages-artifact / deploy-pages）。

## D100 SQLite 文件格式：原生读写，而不是导入导出
目标是 MiniDB 和 sqlite3 能用同一个文件——先后使用，甚至同时使用。没有走“打开时整个导入、关闭时整个导出”的
捷径，而是给 SQLite 格式写了原生的存储层，与 MiniDB 自己的格式并列，按文件头自动选择：
- `minidb/sqlite_format.py`：纯字节布局——varint、record（serial type；与 SQLite 写出的字节逐字节相同，有测试）、
  100 字节文件头、B-tree 页（页头、cell 指针数组、cell 从页尾往前排）、按 SQLite 公式决定 cell 内本地 payload 多少、
  其余进 overflow 页链、freelist 的 trunk / leaf 页。
- `minidb/sqlite_btree.py`：表是按 rowid 的 B+ 树，索引是**真正的 B 树**（内部 cell 本身就是条目，与 MiniDB 自己的
  B+ 树不同，所以不能逐页翻译）。平衡照 SQLite 的 balance_nonroot：过满或过空的页与最多两个兄弟（加上父页里夹在
  它们之间的分隔 cell）一起按需要的页数重新均分，再向上修父页；根页号不变（溢出时内容下移到新子页，只剩一个子页
  且放得下时上移）；末尾追加时照 balance_quick 只开新页，顺序插入的页因此是满的。`build()` 自底向上建树（CREATE INDEX、
  VACUUM），按磁盘顺序复制 cell，不需要比较 key，所以连 MiniDB 解析不了的索引也能原样复制。`SqliteTable` /
  `SqliteIndex` 提供与 `minidb.btree.BTree` 相同的接口，在边界上把 MiniDB record 与 SQLite record 互转（SQLite 把
  整数值的 REAL 存成整数，读回时按列亲和性转回 REAL），catalog 和执行器因此几乎不用改。
- `minidb/sqlite_pager.py`：与 `Pager` 相同的接口，但用 SQLite 的 **rollback journal** 和 **SQLite 的锁**，而不是
  MiniDB 的 WAL：提交时先把要改的页的原内容写进 `<db>-journal`（SQLite 的头：magic、记录数、校验和种子、原页数、
  扇区大小、页大小；记录：页号、页、每 200 字节取一个字节的校验和），fsync，再写数据库文件、fsync，最后删日志。
  崩溃留下的“热日志”由下一个打开者回放——MiniDB 或 sqlite3 都行（多段日志、按校验和截止也照 pager.c）。锁照
  unix VFS：SHARED 是对 510 字节区间的读锁（先读锁 PENDING 字节，让等待中的写者挡住新读者），RESERVED、PENDING、
  EXCLUSIVE 是写锁；这些锁用 D93 的进程内登记表（按 inode 只开一次文件），数据库文件的读写也走那个共享描述符——
  关闭任何一个描述符都会丢掉进程的所有记录锁。于是 MiniDB 与另一个进程里的 sqlite3 可以安全地同时打开同一个文件
  （同一进程里的 sqlite3 模块则不行：记录锁属于进程）。
- 选择：`Database(path, format="sqlite")`、`minidb.connect(..., format=)`、`python -m minidb --sqlite`；已有文件看文件头。
- catalog：schema 表就是 `sqlite_schema`；自动索引叫 `sqlite_autoindex_<表>_<n>`、sql 为 NULL（SQLite 按约束出现
  顺序编号，MiniDB 只有列约束，即列顺序）；`sqlite_` 名字保留；schema 变化时增加 schema cookie。DESC 索引列按降序
  保存（MiniDB 自己的格式仍全部升序），这样的索引会被维护，但规划器不用它查找（磁盘顺序不是 MiniDB key 的顺序）。
  ANALYZE 写 `sqlite_stat1`（与 SQLite 相同的行与数字，见测试），规划器读它；VACUUM / VACUUM INTO 也照 SQLite。
- SQLite 写下而 MiniDB 解析不了的对象（CHECK、触发器、表达式索引、WITHOUT ROWID……）原样保留：用到它们报
  `NotSupportedError`；有这种索引或触发器的表只读（MiniDB 没法维护它们）；VACUUM 照样复制。
- 拒绝打开：页大小不是 4096、WAL 模式、UTF-16、auto_vacuum（报错里说明怎样用 sqlite3 转换）。大事务的脏页全在内存
  （没有 MiniDB 格式那样的溢出）。

验证：sqlite3 写的文件 MiniDB 读、MiniDB 写的文件 sqlite3 读并 `PRAGMA integrity_check`；两边轮流写同一个文件；
在提交的每一步让 MiniDB 崩溃，分别由 sqlite3 和 MiniDB 恢复；让真实的 sqlite3 进程在事务中途死掉（cache_size=2 迫使
脏页先写进文件），MiniDB 回放它的热日志；锁的双向互斥（另一个进程里的 sqlite3）；fuzzer 的 `--sqlite-format` 模式在
每个种子结束时让 sqlite3 打开 MiniDB 的文件做完整性检查并逐表比较内容。

## D101 sqlite_schema / sqlite_master
两种格式都可以读 `sqlite_schema` 和 `sqlite_master`（只读；改、删、建索引、ALTER 的报错与 SQLite 逐字相同）。
SQLite 格式里它就是第 1 页的表；MiniDB 格式里是 schema 表去掉统计行、自动索引的 sql 显示为 NULL，但自动索引的名字是
`minidb_autoindex_...`，表的 sql 是 MiniDB 规范化重写过的，rootpage 也是 MiniDB 的页号。MiniDB 格式也保留 `sqlite_` 前缀。

## D102 访问路径与行顺序照 SQLite：覆盖索引全扫描、OR 转 IN、MULTI-INDEX 的输出顺序
没有 ORDER BY 时的行顺序本不属于语义，但它会被看到：聚合里的裸列取“最后一行”、`group_concat` 的拼接顺序、不带
ORDER BY 的 LIMIT、`sum` 的整数溢出与否（取决于累加顺序）。fuzz 种子 9661 与 5512 都是这一类。与其在 fuzzer 里回避，
不如让常见情形的计划和顺序与 SQLite 一致：
- 覆盖索引全扫描（whereLoopAddBtree 的 "full scan via index"）：选了全表扫描、且有索引包含查询用到的全部列、且
  `szIdxRow < szTabRow` 时，改扫该索引。宽度照 SQLite 估：`sqlite3AffinityType` 给每列的 szEst（整数为 1，
  TEXT/BLOB/CLOB 为 5，`VARCHAR(k)` 为 k/4+1，无类型为 1），表宽 = 列宽之和（没有 INTEGER PRIMARY KEY 时 +1），
  索引宽 = 索引列之和 + 1（rowid），再取 `sqlite3LogEst(4 * 宽)`；在满足条件的索引里取 `15 * szIdxRow / szTabRow`
  （整数除法）最小的，同价取 `table.indexes` 里靠前的——它与 SQLite 的 `pTab->pIndex` 一样新建的在前。
  只在已选定全表扫描后替换，不进入代价比较（SQLite 里这个代价总低于 3N 的全扫，所以效果相同）。
- 全表扫描按 3N 计价（SQLite 的 `rSize + 16`，有意压低全扫）：只用在单表选路，连接顺序的估算仍用原值，避免牵动
  连接顺序。效果是两个单边范围的 OR 走 MULTI-INDEX OR，8 行的表也走索引——都与 SQLite 一致。
- `x = a OR x = b ...`（同一列，右边没有亲和性或与列相同）另外生成虚拟项 `x IN (a, b, ...)`
  （exprAnalyzeOrTerm），OR 本身照样检查；亲和性不同时仍是 MULTI-INDEX OR，也与 SQLite 一致。
- MultiScan 不再按 rowid 排序输出：IN 按索引 key 顺序（SQLite 把 IN 列表排序后逐个查），OR 逐项输出、每行一次
  （SQLite 的 RowSet）。相应地 `order()`：IN 给出索引顺序，OR 给不出顺序（ORDER BY id 时要排序，SQLite 也用临时
  B 树排序）。
- `NOT INDEXED` / `INDEXED BY` 从“只检查”改为约束规划器（SQLite 的 notIndexed / isIndexedBy）：前者不用任何索引
  （rowid 查找照用），后者只用那个索引、不扫表本身（没有可用条件时扫整个索引），都不用自动索引。
仍然不同的：连接顺序（MiniDB 自己的代价模型）、IN/OR 子项是否覆盖、DESC 索引（SQLite 格式里不用于查找）。

## D103 SQLite 格式的性能：只在热点上动，转码层保留
SQLite 格式第一版比 MiniDB 格式慢得多（参数化插入 10 倍、全扫 3.5 倍）。剖析后只改三处：
- 单元格缓存自己的字节数（按页类型），页拷贝（语句日志）共享单元格对象——B 树代码在改单元格前总是先 copy，
  所以页上的单元格从不被原地修改；
- SQLite record 解码像 `minidb.record` 一样，每种 header 编译一次成 `struct.Struct` + 组装函数并缓存；
- 用户表的树直接把解码好的行交给执行器（`Executor.load_row` 两种都收），省掉“SQLite record → 值 → MiniDB
  record → 值”中间那一次编码和解码。写入方向仍是 MiniDB record → SQLite record，schema 和统计表仍走 bytes 接口。
没有做的：每次插入仍要 O(页内单元格数) 地累加页面用量，平衡时重算分布；插入因此仍比 MiniDB 格式慢 1.6–2.4 倍。

## D104 Windows 锁改用 LockFileEx（取代 D95 的 msvcrt.locking）
D95 选 `msvcrt.locking` 是因为它不需要 ctypes；它只有排他锁，共享锁靠“锁区间里任意一个字节”模拟。MiniDB 自己的格式
里这没问题（推送后真实 Windows 上通过），但 SQLite 文件格式要与 sqlite3 互操作：SQLite 在 NT 上用 `LockFileEx`
给整个 SHARED 区间加*共享*锁（winGetReadLock），MiniDB 读者锁其中一个字节的排他锁必然与之冲突——sqlite3 读时 MiniDB
读不了，反之亦然（Windows CI 的 `test_locks_against_a_sqlite_process`）。另外 SQLite 的协议要把 SHARED 升级为
EXCLUSIVE，`msvcrt.locking` 连同一句柄的重叠字节都锁不上，新建 SQLite 格式文件就超时。
改为 ctypes 调用 `LockFileEx` / `UnlockFileEx`（标准库，句柄来自 `msvcrt.get_osfhandle`）：
- 共享 / 排他都是真的，每把锁一个字节（与 POSIX 后端相同的布局），不再有 64 个进程的上限；
- 升级照 SQLite 的 winLock：解锁、排他加锁、失败则重新共享（SQLite 的协议只在持有 PENDING 时升级，期间没有新读者）；
- 降级是原子的：同一句柄可以在自己的排他锁上再加共享锁，之后第一次解锁去掉的是排他锁（LockFileEx 文档的语义）。
测试用一个按 LockFileEx 语义实现的假内核（按句柄；排他锁不与任何锁重叠，包括同一句柄；共享锁可与共享锁和同一
句柄的排他锁重叠；解锁必须匹配、先去掉排他锁），包括“sqlite3 持有整个 SHARED 区间的共享锁时 MiniDB 能读不能写”。
真实 Windows 上（CI）359 个测试通过，见 PROGRESS.md。
