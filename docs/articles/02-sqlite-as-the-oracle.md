# 以 SQLite 为标准答案：对照测试、fuzz、sqllogictest 与变形测试

写一个“和 SQLite 行为一致”的数据库，最大的好处是：标准答案就在手边。Python 自带 `sqlite3`，同一条 SQL
在两边各跑一遍，结果必须一样。这篇讲 MiniDB 用了哪几层测试，以及每一层抓到了什么。

## 第一步：先确认标准答案是对的

第一次在 GitHub Actions（Ubuntu，系统 SQLite 3.45）上跑测试，对照测试大面积失败。查下来问题出在本地：
conda-forge 的 Python 链接的 SQLite 开了 ICU 扩展（`upper('é')` 变成 `'É'`），参数个数上限也改过。MiniDB 此前
“对齐”的就是这些非默认行为。

于是写了 `tools/reference_sqlite.py`：下载固定版本（3.53.4）的 amalgamation，校验 SHA3-256，用默认选项编译成
动态库，再用 `LD_LIBRARY_PATH` / `DYLD_LIBRARY_PATH` 让 Python 的 `sqlite3` 加载它。本地和 CI 用同一个脚本；
链接到带 ICU 的 SQLite 时对照测试直接报错，而不是悄悄对齐错误的答案。

## 第一层：手写的对照测试

`tests/sqlcompare.py` 的 `Pair` 把一条 SQL 同时交给 MiniDB 和 sqlite3：要么都成功且结果相同（区分 `1` 和 `1.0`），
要么都失败且异常类别相同，部分用例连报错文字都逐字比较。每加一个功能，先在 sqlite3 里做实验，再写成对照测试，
最后才实现。

## 第二层：随机 SQL 的差分 fuzz

`tests/fuzz.py` 随机生成 schema（约束、各种索引）和语句（嵌套表达式、子查询、连接、聚合、窗口函数、UPSERT、
事务、VACUUM……），一条一条在两边执行，每个种子结束时做一次 `integrity_check`。难点是**比较口径**：有些地方
SQLite 的答案取决于它选的查询计划，例如没有 ORDER BY 时的行序、聚合查询里“裸列”取自哪一行、相等的 1 和 1.0
里 DISTINCT 保留哪一个。这些地方 fuzzer 要么按多重集比较，要么干脆不生成。

fuzz 抓到的东西五花八门，挑几个：

- 外层查询的聚合出现在子查询里、`RIGHT JOIN` 之前含子查询的 `ON` 放错了层级；
- `INSERT OR REPLACE` 用默认值填 NOT NULL 列之后，upsert 的 `excluded` 行看到的值；
- VACUUM 之后 rowid 被重新编号（只有既没有 INTEGER PRIMARY KEY、也没有任何索引的表才会）；
- 加入 `TRUE` / `FALSE` 字面量之后，第一批种子就发现 `SELECT ... FROM t WHERE FALSE` 报
  “no such column: FALSE”——一个存在了很久、只因为 fuzzer 从不生成这两个词而没被发现的 bug。

教训：fuzzer 只能发现它会生成的东西。每加一个功能，都要同时扩展生成器。

## 第三层：sqllogictest

sqllogictest 是 SQLite 自己的、与引擎无关的测试集，约 594 万条记录，期望结果由 SQLite 产生并和其他数据库交叉核对过。
它的价值在于用例**不是我们写的**，会用到我们没想到的写法。

写了一个 `.test` 格式的 runner（结果格式化照官方 C runner：`I` 按 `sqlite3_column_int64`、`R` 用 `%.3f`、
超过阈值比较 MD5），第一次跑的结果是 63.03%。缺一个功能时，文件开头的 `CREATE TABLE` 就失败，后面每条都报
`no such table`，所以报告按“每个文件的第一个失败”归类根因——这份排名直接决定了接下来补什么：任意类型名
（`FLOAT`、`VARCHAR(10)`）、括号里的连接、视图……补完之后是 5,939,852 / 5,939,879（99.9995%）。剩下的 27 条：
23 条是当时还不支持的 `CREATE TRIGGER`，4 条的期望值来自 3.43 之前的 SQLite（那时 `sum` 还没有补偿求和）。
第四轮实现了触发器之后是 5,939,875 / 5,939,879，只剩那 4 条。

sqllogictest 也抓到过一次性能优化带来的回归：给连接循环生成 Python 源码之后，21 张表的连接生成了 21 层嵌套 `for`，
超过了 Python 编译器 20 层静态嵌套块的限制。

## 第四层：不需要标准答案的变形测试

差分 fuzz 依赖 sqlite3 给答案。SQLancer 的 TLP 和 NoREC 只靠 MiniDB 自己：

- **TLP**（三值分区）：任何一行，`p` 要么为真、要么为假、要么为 NULL。所以 `WHERE p`、`WHERE NOT p`、
  `WHERE p IS NULL` 三个查询结果的并集，必须等于不带 WHERE 的结果。聚合、DISTINCT、HAVING 也有对应的拆法
  （例如 `max` 对三部分分别求再取最大）。
- **NoREC**：优化器能利用的 `WHERE p` 和“逐行求 `CASE WHEN p THEN 1 END`”必须选出同样多的行。

它们专门找优化器的 bug：同一个查询的两种写法走不同的访问路径。为了验证它们真的有用，做过变异测试：手工把 rowid
范围的下界改成永远是开区间，只用随机字面量时 60 个种子里只有 2 个发现；加入“列 比较 表里实际存在的值”的条件后，
33 个发现。

## 第五层：崩溃

存储层的测试是另一回事：在提交和 checkpoint 的每一步模拟崩溃（进程内抛异常，以及子进程里真的 `os._exit`），
重开后数据必须是某次提交之后的完整状态。阶段 18 又加了一个更狠的模型：未 fsync 的写入可能丢失、乱序、
按 512 字节扇区撕裂，见第 3 篇。

## 对应的决策

D39 / D40（fuzz 口径与发现）、D70（参考 SQLite）、D72（sqllogictest）、D73（变形测试）、D80（读源码而不是凭记忆）、
D94（崩溃模型）、D98（TRUE / FALSE）。
