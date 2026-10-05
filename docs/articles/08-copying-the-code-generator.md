# 照着代码生成器写：约束、外键、触发器与 JSON

能读写 SQLite 的文件之后，下一道坎是文件里的**对象**：CHECK、外键、触发器。一张带触发器的表，MiniDB 只要还不懂它，
就只能只读打开。第四轮的阶段 21–23 把这些补上，外加 JSON 函数。这篇讲做的过程中最大的一个认识：很多“语义”并不写在
文档里，它们是 SQLite 代码生成器的副作用，想和它一致，就得按它生成代码的顺序做事。

## 标准答案换成了真实的库

前几轮的对照测试用的是随机生成的表。这一轮加了两个公开样例库 Chinook 和 Northwind（固定版本、校验哈希、不入库），
测试流程是：两边各拿一份拷贝，执行同一批语句（打开外键，包括会触发外键错误的、延迟外键的事务、违反 CHECK 的、
AUTOINCREMENT、NOCASE 索引、视图），每条结果都要相同；最后让 sqlite3 对 MiniDB 改过的文件做 `integrity_check`，
`foreign_key_check` 要和它自己的一致，`sqlite_schema` 和每张表逐行相同。

这个测试一跑起来就找到两个随机 fuzz 从没碰到的问题：

- Chinook 的每张表都写成 `INTEGER PRIMARY KEY NOT NULL`。往这种列插 NULL 不违反 NOT NULL，它的意思是“分配一个新 rowid”。
- Northwind 有一条未命名的 `CHECK ([UnitPrice]>=(0))`，SQLite 报错时用 sqlite3Dequote 处理原文，报成“UnitPrice”。

schema 也改成了存原文。以前 MiniDB 把表定义重新打印成规范化的 SQL，约束一多就容易丢东西。现在 `sqlite_schema.sql`
和 sqlite3 存的逐字相同，ALTER TABLE 也照 alter.c 改文本里的记号。SQLite 写的、MiniDB 并不完全理解的表，经过 ALTER
也不会丢内容。

## 外键：计数器，以及没有亲和性的 OLD

SQLite 的外键（fkey.c）不是逐行检查的，它用计数器。子表多一行孤儿就 +1，修好一行就 -1。立即约束在语句结束时看计数，
延迟约束在 COMMIT 时看。计数非零时 COMMIT 失败，事务保持打开。MiniDB 照搬这个模型，连“插入一行父表不会去找等着它的
子行”这种优化也照搬，因为它看得见：自引用表里，新行自己的悬空外键照样报错。

更隐蔽的是 ON DELETE 动作。SQLite 把动作实现成一个生成的触发器 `... WHERE OLD.父列 = 子列`，而触发器里的 `OLD.x`
带着父列的排序规则，却**没有亲和性**。于是同一对值，计数器认为匹配，动作却找不到它：

```sql
PRAGMA foreign_keys = ON;
CREATE TABLE p(x REAL PRIMARY KEY);
CREATE TABLE c(y TEXT REFERENCES p(x) ON DELETE SET NULL);
INSERT INTO p VALUES (-1.0);
INSERT INTO c VALUES ('-1');   -- 计数器按父列亲和性比较：-1.0 = '-1'，不是孤儿
DELETE FROM p;                 -- SET NULL 按子列（TEXT）比较：'-1.0' ≠ '-1'，找不到
-- FOREIGN KEY constraint failed（两边都是）
```

## 触发器：错误在编译时报，按编译顺序报

SQLite 编译一条 INSERT 时，会把它可能运行的所有程序一起编译出来：BEFORE 触发器、REPLACE 可能触发的 DELETE 触发器、
外键检查、外键动作、AFTER 触发器。所以一条语句如果有两处错误（某个触发器里引用了不存在的列，另一个外键 mismatch），
报的是**先编译到的那个**，而且一定在任何一行改变之前报。

MiniDB 原来是执行到才编译。为了一致，改成在构造计划时按 SQLite 的代码生成顺序编译这些程序，并把编译过的程序按顺序记成
一个清单。这个清单还有一个用处：SQLite 编码新行的外键检查时，会看“最后编译的那个程序是不是这个外键的 SET NULL 动作”，
是的话就跳过检查（isSetNullAction）。这完全是实现细节，但结果看得见，所以 MiniDB 也要知道“最后编译的是哪个”。

其余的照搬清单还很长，每一条都先用最小用例在 sqlite3 上实测过：

- 同一事件的触发器，新建的先触发；
- BEFORE 触发器改了或删了当前行，UPDATE 会再读一次这一行；
- REPLACE 删掉的行只在 `recursive_triggers` 打开时才触发 DELETE 触发器；
- UPDATE 的 REPLACE 运行 DELETE 触发器期间，正在更新的行被固定（`OP_CursorLock`），触发器再写这张表就报
  `constraint failed`；
- REPLACE 之后的复查复制了第一遍的代码，但跳过了读 rowid 的那条指令，于是没改键的索引会撞上这一行自己。

最后两条是阶段 23 的 fuzz 找到的。它们都不是文档里的语义，只是代码生成的副作用（D112）。照搬的理由和整个项目一样：
标准答案是 SQLite 3.53.4 的实际行为，而这些行为体现在结果、错误信息和 `total_changes()` 上。sqllogictest 里剩下的
23 条 TRIGGER 记录随之通过，全量 5,939,875 / 5,939,879，剩下 4 条是已知的 `sum` 精度和整数溢出。

## JSON：一切经过 JSONB

JSON 函数最省事的写法是用 Python 的 `json` 模块。但对照测试要求结果**逐字节**相同：`json('1.50')` 是 `1.50`，
`json('0x1F')` 是 `31`，`json(1e20)` 是 `1.0e+20`。Python 的解析器既不认 JSON5，也会丢掉数字的原文。所以 `jsonb.py`
照着 json.c，先把文本解析成 SQLite 的二进制格式 JSONB（头部低 4 位是类型，高 4 位是长度；数字和字符串保留原文），
所有函数都在 JSONB 上做。`jsonb()` 的字节、`json_each` 的 `id`（就是 JSONB 里的偏移）、`json_error_position` 也就
自然一致了。

有两个机制是“看得见的实现”：

- **subtype**。JSON 函数返回的文本带一个标记，别的 JSON 函数收到它会当 JSON 嵌入，而不是当字符串加引号。MiniDB 用
  `str` 的子类表示：它原样穿过 CASE、coalesce、标量子查询，经过 `||` 或 `upper()` 就变回普通字符串，存进表或经过
  不被展平的子查询时丢掉。这些丢失点都是逐个实测出来的。
- **解析缓存**。每条语句有一个 4 项的缓存，`json_set` 这类编辑函数把结果文本连同编辑后的 JSONB 一起放进去。于是：

  ```sql
  SELECT json_set('{a:0x10}', '$.b', 1),   -- '{"a":16,"b":1}'
         json_valid('{"a":16,"b":1}');     -- 0：命中缓存，拿到的 JSONB 里还是 JSON5 的 0x10
  ```

  连“结果还在 100 字节的静态缓冲区里就不进缓存”这个条件也要照搬，是 fuzz 种子 1822 发现的。

收尾阶段又补了一轮**损坏的 JSONB**：拿合法的 JSONB 随机改字节、截断、插入，同一个值交给两边的所有 JSON 函数。找到的
差异都是 json.c 里一两行的细节：带载荷的 null / true / false 算损坏；pretty 打印只检查对象的键越界，数组的
元素越界不管；`\u00H1` 是 U+0011，因为 jsonHexToInt 不检查它的参数是不是十六进制数字；拼接时前一个字符是 `[` 或 `{`
就不加逗号；比较键时按 sqlite3Utf8ReadLimited 读码点、读到 0 为止，所以 `"a\u0000b"` 和 `"a\u0000c"` 是同一个键。
另外修了 json_patch 遇到头部损坏的键时的一处死循环。

代价是速度。逐字节照抄 json.c 的纯 Python，比 sqlite3 慢 34 到 156 倍。这一轮没有优化它。

## 对应的决策

D105（约束与 schema 原文）、D106（排序规则）、D107（PRAGMA）、D108（外键）、D110（触发器）、D111（JSON）、
D112（代码生成的怪癖）。
