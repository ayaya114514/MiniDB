# 进度日志

## 阶段 1：骨架（完成）
- 项目结构：`minidb/`（`parser.py`、`executor.py`、`repl.py`、`__main__.py`）、`tests/`、`pyproject.toml`（pytest 配置）、git 仓库。
- 固定单表 `users(id, name, age)` 存在内存，支持 `insert <id> <name> <age>` 和 `select`；重复 id 报错。
- REPL：`python -m minidb`，`.exit` 退出，未知元命令和语句错误都会报 `Error:` 后继续。
- 测试：16 个，全部通过（含与 sqlite3 的对照测试、命令行入口测试）。
- 已知问题：数据只在内存里；语法是临时的，阶段 4 替换成 SQL。
