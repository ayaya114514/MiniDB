# 能改真实的库：SQLite 文件格式、WAL 与它的锁协议

前三轮的 MiniDB 有自己的文件格式。第四轮的目标换成了一句话：**拿一个 sqlite3 建的真实数据库，MiniDB 打开、修改，
sqlite3 再打开时 `integrity_check` 是 ok，内容一字不差**——甚至两边同时开着。这篇讲做到这一点要照搬些什么，以及
哪些地方不能“差不多”。

## 原生读写，而不是导入导出

最省事的办法是打开时把整个文件导入 MiniDB 的格式、关闭时再导出。这样做不到“同时开着”，也做不到崩溃安全，所以
SQLite 格式有自己的一套存储层，与 MiniDB 的格式并列，按文件头自动选择：

- `sqlite_format.py` 只管字节：varint、record 的 serial type、100 字节文件头、B 树页（cell 指针数组、cell 从页尾往前排）、
  一个 cell 本地放多少 payload、其余进 overflow 链，以及空闲列表。record 的编码与 SQLite 写出的逐字节相同（有测试）。
- `sqlite_btree.py`：表是按 rowid 的 B+ 树，**索引却是真正的 B 树**——内部页的 cell 本身就是条目。MiniDB 自己的索引是
  B+ 树，所以不能逐页翻译，平衡也照 SQLite 的 balance_nonroot 重写了一遍。
- `sqlite_pager.py`：SQLite 的回滚日志（`-journal`）和 SQLite 的文件锁（SHARED 是 510 字节区间上的读锁，PENDING、
  RESERVED、EXCLUSIVE 各占一个字节），于是另一个进程里的 sqlite3 能和 MiniDB 同时打开同一个文件。

执行器几乎不用改：`SqliteTable` / `SqliteIndex` 提供和 MiniDB B+ 树相同的接口。代价是边界上的转码。第二轮性能优化时，
表树直接接收值列表，省掉了一次“编码成 MiniDB record 再解码”。

## 那些“必须一样”的细节

能读不难，能写而不让 sqlite3 报错才难。几个只有对照才会发现的例子：

- **整数形式的 REAL**。SQLite 把值为整数的 REAL 存成整数，读回时按列亲和性转回 REAL。在寄存器里它还有第三种身份 IntReal：
  读出来是 REAL，一经过记录就变成整数。生成列、窗口函数的临时表、GROUP BY 的排序器，各自在不同的地方经过记录。
  MiniDB 用 `values.IntReal` 表示它，在每个“经过记录”的地方转换。
- **WITHOUT ROWID 表就是一棵索引 B 树**，二级索引的条目要接上缺的主键列，而主键列在二级索引里的升降序：`CREATE INDEX`
  照抄主键的 DESC，表定义里 UNIQUE 约束的自动索引一律升序。这是 SQLite 源码里明说的“bAscKeyBug”，为了兼容旧文件而保留。
- **页大小与 auto_vacuum**：512 到 65536 字节都要能读写；auto_vacuum 的根页必须集中在文件前部，否则 sqlite3 的 incremental
  vacuum 搬页时会报损坏。指针图（ptrmap）没有在 B 树代码里逐处维护，而是在提交前从脏页推出来。
- **只在溢出时平衡**：sqlite3BtreeInsert 只有页溢出才调用 balance()。MiniDB 原来对“不足 1/3”的页也做平衡，于是顺序追加时
  新开的最右叶子几乎每插一行都与兄弟页重新分配一次（3 万行插入里 1383 次），页面常年半空。照 SQLite 改过之后，
  10 万行的文件从 13.4 MB 变成 9.4 MB（sqlite3 是 9.3 MB），这一项让插入快了约三成。

## WAL：照搬 `-wal` 和 `-shm`

WAL 模式要和 sqlite3 进程并发读写，所以日志、wal-index 和锁都必须是 SQLite 自己的：

- `-wal`：32 字节头，然后是一帧帧的页；帧头有页号、提交帧上的库大小、salt，以及贯穿整个日志的校验和。
- `-shm`（wal-index）：两份索引头、checkpoint 信息和 5 个 read mark，然后是每块 32 KB 的页号数组和 8192 槽的哈希表。
  sqlite3 的读者靠哈希表找帧，所以 MiniDB 写帧时照 walIndexAppend 维护它，包括清理崩溃写者留下的残项。
- 锁是 `-shm` 第 120–128 字节上的 WRITE / CKPT / RECOVER / READ0–4 / DMS。读者选一个 read mark 持共享锁，checkpoint 只回填
  到所有读者都看不到的地方；最后一个连接关闭时回填全部并删掉两个文件。

SQLite 把 `-shm` mmap 进内存，MiniDB 用 pread / pwrite 读写同一个文件。在同一台机器上两者看到的是同一份页缓存，结果一致，
也省掉了 mmap 的平台差异。

有一个坑不在格式里，而在操作系统：**POSIX 记录锁属于进程**。同一个进程里的 sqlite3 模块看不见 MiniDB 持有的锁，关闭时
以为自己是最后一个连接，就回填并删掉了 MiniDB 还在用的日志。所以测试里只要 MiniDB 开着文件，sqlite3 一律放在子进程里跑。

## 怎么验证

- 互相读写：sqlite3 写 → MiniDB 改 → sqlite3 `integrity_check` 并逐表比较内容；反过来也做。公开样例库（Chinook、Northwind）
  也这样走一遍。
- 差分 fuzz 的 SQLite 格式模式：每个种子结束时，把 MiniDB 的文件交给 sqlite3 检查，也把 sqlite3 的库交给 MiniDB 检查；
  `--wal` 模式下 sqlite3 在子进程里检查 WAL 文件。
- 崩溃：日志的每一步、checkpoint 的每一步都做了崩溃点，之后让 sqlite3 或 MiniDB 恢复。阶段 27 又加了随机断电，见下一篇。

## 对应的决策

D100（原生读写）、D103（性能）、D114（页大小）、D115（auto_vacuum）、D116（生成列）、D119（WITHOUT ROWID）、
D120（WAL）、D121（只在溢出时平衡）。
