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
