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
