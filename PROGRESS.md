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
