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
