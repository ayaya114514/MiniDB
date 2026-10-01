# SQLite 的怪癖图鉴：那些只有读源码才知道的行为

“和 SQLite 行为一致”听起来是一句话，做起来是几百个细节。这篇收集了做 MiniDB 时遇到的、最不像“设计”而更像
“实现的副作用”的行为。每一条都在参考 SQLite 3.53.4 上实测过，MiniDB 照做，并有对照测试。

## 1. `5 IS NOT TRUE` 的值取决于它在 VALUES 的第几行

```sql
VALUES (1, 5 IS NOT TRUE), (2, 5 IS NOT TRUE);
-- 1|0
-- 2|1
```

`x IS TRUE` 在 SQLite 里是真值测试（`48 IS NOT TRUE` 为 0），由名字解析阶段把 `IS` 改写成 `TK_TRUTH`。但多行 VALUES
的第二行起，SQLite 在可以的时候把行直接编码进一个协程，**不经过名字解析**；`TRUE` 只是被判断“是不是常量”的函数
顺手变成了 1，于是第二行算的是 `5 IS NOT 1`。“可以的时候”有一串条件：语句里此前没出现过 WITH、这一行是常量（确定性
函数可以，`random()` 不行）、前一行如果是普通行，它也得是常量且没有 CAST……全部是一条条实测出来的。

## 2. `0 AND 不存在的列` 不报错

```sql
SELECT * FROM t WHERE (0 AND no_such_column) OR c = 1;   -- 正常执行
SELECT * FROM t WHERE 0 AND abs(no_such_column);          -- no such column
```

解析器在构造 AND 节点时（`sqlite3ExprAnd`），只要一侧是整数字面量 0、且两侧都没有函数调用，就直接换成 0——在名字
解析之前，所以另一侧写了什么都无所谓。有函数调用就不折叠（LIKE 也算函数）；`-0`、`0.0`、`'0'`、`FALSE` 都不算 0。

## 3. TRUE 和 FALSE 是名字，不是关键字

`TRUE` 在表里有名为 `true` 的列时是那一列，否则才是 1。MiniDB 的表达式编译器早就这样处理，但规划器有几处直接解析
列名，于是 `SELECT ... FROM t WHERE FALSE` 报“no such column: FALSE”。这个 bug 存在了很久，因为 fuzzer 从来不生成
这两个词——加进去的第一批种子就发现了它。

## 4. VACUUM 会给某些表重新编号

```sql
CREATE TABLE plain (a);  INSERT INTO plain VALUES (1), (2), (3);
DELETE FROM plain WHERE a = 2;  VACUUM;
SELECT rowid, a FROM plain;   -- 1|1, 2|3
```

VACUUM 用“传输优化”把表复制进新文件：表有 INTEGER PRIMARY KEY 或者任何索引时保留 rowid，否则给新行分配新的
rowid（1, 2, 3…）；`VACUUM INTO` 则一律保留。MiniDB 最初写成“一律保留”，fuzzer 一生成 VACUUM 就在 10 个种子上发现了。

## 5. upsert 的 `excluded` 看到的值，取决于前面的行

```sql
CREATE TABLE t (id INTEGER PRIMARY KEY, c1 VARCHAR(5) DEFAULT -1.5, c3);
INSERT INTO t (id) VALUES (1);
INSERT INTO t (id) VALUES (5), (1)
  ON CONFLICT (id) DO UPDATE SET c3 = typeof(excluded.c1);   -- 'text'
INSERT INTO t (id) VALUES (1)
  ON CONFLICT (id) DO UPDATE SET c3 = typeof(excluded.c1);   -- 'real'
```

常量 DEFAULT 每条语句只算一次，直接放进构造行的寄存器；而列亲和性是**原地**作用于寄存器的（第一次查索引时，或生成
记录时）。所以前面有一行走到了那一步，后面各行 `excluded` 里的默认值就已经被 VARCHAR 列的亲和性转成了文本。

## 6. sum 的结果和加法顺序有关，而且要逐位一致

SQLite 3.43 起，`sum`/`avg`/`total` 遇到浮点数后用 Kahan-Babuska-Neumaier 补偿求和。MiniDB 直接移植 func.c，浮点
结果逐位一致。窗口函数更进一步：滑动窗口里 `sum` 的值取决于 xStep 和 xInverse 的交错顺序，所以 `window.py` 不是
“每行重算 frame”，而是逐步复现 SQLite 的三游标主循环。sqllogictest 里剩下的 4 条失败，正是期望值来自 3.43 之前的版本。

## 7. 浮点数转文本

`SELECT 0.1 + 0.2` 的文本形式，最初用“15 位有效数字能往返就用 15 位、否则 17 位”，与 SQLite 一致率约 91%。后来直接
读 3.53 的源码，把 `sqlite3FpDecode` 等函数（基于 rsc/fpfmt 的 64/128 位整数算法）移植过来，10 万个随机 double
逐位一致。教训写进了决策记录：对照对象有源码的时候，读源码，而不是凭记忆。

## 8. 一元负号是 `0 - X`

`-x` 对非字面量编译成 `0 - x`，所以 `-(c)` 在 c = 0.0 时得到 0.0 而不是 -0.0（`atan2(0, -c)` 能看出区别）；只有紧跟
数字字面量的负号产生负常量。

## 9. 比较亲和性作用于两侧

`a = b` 时，SQLite 先算出比较亲和性，再把它施加到**两个**操作数上，而不只是转换“另一侧”。对普通列两种理解等价，
但对 UNION 子查询的列、FULL JOIN USING 产生的 `coalesce` 这类“值不一定符合亲和性”的表达式就不同了。

## 怎么找到这些

几乎都是差分 fuzz 先报出“两边不一样”，再缩小到几条语句，最后去读 SQLite 的源码（resolve.c、insert.c、select.c、
vdbe.c）确认机制，并补上一组覆盖边界条件的对照测试。也有一部分是 MiniDB 自己的 bug——两者的区分方式就是读源码。

## 对应的决策

D27（聚合）、D77 / D96（upsert 与 excluded）、D80（浮点）、D82（一元负号）、D85（比较亲和性）、D88（窗口函数）、
D91（VACUUM）、D98（AND 折叠、TRUE / FALSE、IS TRUE、多行 VALUES）。
