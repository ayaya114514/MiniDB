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
