# MiniDB 系列文章（草稿）

用 Python 从零写一个以 SQLite 为标准答案的数据库，一路踩过的坑。素材来自 [DECISIONS.md](../../DECISIONS.md)
（每篇末尾列出对应的决策编号）和 [PROGRESS.md](../../PROGRESS.md) 里的实测数据。

1. [页、B+ 树与记录：一个 4 KB 页的数据库长什么样](01-pages-and-btrees.md)
2. [以 SQLite 为标准答案：对照测试、fuzz、sqllogictest 与变形测试](02-sqlite-as-the-oracle.md)
3. [WAL、快照与读者标记：一个写者、许多读者，日志不再无限增长](03-wal-and-read-marks.md)
4. [让 Python 快一点：代码生成、哈希连接与批量建索引](04-making-python-fast.md)
5. [SQLite 的怪癖图鉴：那些只有读源码才知道的行为](05-sqlite-quirks.md)

状态：草稿，未发布。数字都是本机（Apple Silicon，Python 3.12）上实测的，发布前若代码有变化需要重跑核对。
