"use strict";

const EXAMPLES = [
  ["B+ 树与索引", `-- 2000 行的表和一个索引：右边切换查看它们的 B+ 树，结果上方是查询计划
DROP TABLE IF EXISTS people;
CREATE TABLE people (id INTEGER PRIMARY KEY, name TEXT, age INTEGER, city TEXT);
WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < 2000)
INSERT INTO people (name, age, city)
SELECT 'user' || i, 18 + (i * 37) % 60,
       CASE i % 4 WHEN 0 THEN 'Tokyo' WHEN 1 THEN 'Paris' WHEN 2 THEN 'Oslo' ELSE 'Lima' END
FROM n;
CREATE INDEX people_age ON people (age);

SELECT count(*), avg(age) FROM people WHERE age BETWEEN 30 AND 35;
SELECT name, age, city FROM people WHERE id = 42;
SELECT city, count(*), min(age), max(age) FROM people GROUP BY city ORDER BY 2 DESC;
`],
  ["窗口函数", `DROP TABLE IF EXISTS sales;
CREATE TABLE sales (day TEXT, region TEXT, amount REAL);
INSERT INTO sales VALUES
  ('2026-09-01', 'north', 120), ('2026-09-01', 'south', 80),
  ('2026-09-02', 'north', 95),  ('2026-09-02', 'south', 130),
  ('2026-09-03', 'north', 160), ('2026-09-03', 'south', 70),
  ('2026-09-04', 'north', 110), ('2026-09-04', 'south', 150);

SELECT day, region, amount,
       sum(amount) OVER (PARTITION BY region ORDER BY day) AS running_total,
       rank() OVER (PARTITION BY day ORDER BY amount DESC) AS rank_in_day,
       avg(amount) OVER (PARTITION BY region ORDER BY day
                         ROWS BETWEEN 1 PRECEDING AND CURRENT ROW) AS moving_avg
FROM sales
ORDER BY region, day;
`],
  ["递归 CTE：曼德博集合", `WITH RECURSIVE
  xaxis(x) AS (VALUES (-2.0) UNION ALL SELECT x + 0.08 FROM xaxis WHERE x < 1.1),
  yaxis(y) AS (VALUES (-1.0) UNION ALL SELECT y + 0.125 FROM yaxis WHERE y < 1.0),
  m(iter, cx, cy, x, y) AS (
    SELECT 0, x, y, 0.0, 0.0 FROM xaxis, yaxis
    UNION ALL
    SELECT iter + 1, cx, cy, x * x - y * y + cx, 2.0 * x * y + cy FROM m
    WHERE x * x + y * y < 4.0 AND iter < 20
  ),
  m2(iter, cx, cy) AS (SELECT max(iter), cx, cy FROM m GROUP BY cx, cy),
  a(t) AS (SELECT group_concat(substr(' .:-=+*#%@', 1 + min(iter / 2, 9), 1), '') FROM m2 GROUP BY cy)
SELECT rtrim(t) AS mandelbrot FROM a;
`],
  ["UPSERT、RETURNING 与连接", `DROP TABLE IF EXISTS stock;
DROP TABLE IF EXISTS items;
CREATE TABLE items (sku TEXT PRIMARY KEY, name TEXT);
CREATE TABLE stock (sku TEXT PRIMARY KEY, qty INTEGER NOT NULL);
INSERT INTO items VALUES ('A1', 'apple'), ('B2', 'bread'), ('C3', 'cheese'), ('D4', 'dates');
INSERT INTO stock VALUES ('A1', 5), ('B2', 0);

INSERT INTO stock VALUES ('A1', 3), ('C3', 7)
  ON CONFLICT (sku) DO UPDATE SET qty = qty + excluded.qty
  RETURNING sku, qty;

SELECT i.name, coalesce(s.qty, 0) AS qty
FROM items AS i LEFT JOIN stock AS s USING (sku)
ORDER BY qty DESC, i.name;
`],
  ["事务与回滚", `DROP TABLE IF EXISTS account;
CREATE TABLE account (id INTEGER PRIMARY KEY, owner TEXT, balance INTEGER);
INSERT INTO account (owner, balance) VALUES ('ann', 100), ('bob', 50);

BEGIN;
UPDATE account SET balance = balance - 70 WHERE owner = 'ann';
UPDATE account SET balance = balance + 70 WHERE owner = 'bob';
SELECT * FROM account;
ROLLBACK;

SELECT * FROM account;
`],
];

const STORAGE_KEY = "minidb-playground-sql";
const MAX_ROWS = 500;
const $ = (id) => document.getElementById(id);
const editor = $("sql"), output = $("output"), statusLine = $("status");
const runButton = $("run"), resetButton = $("reset"), examples = $("examples"), objectSelect = $("object");
const openButton = $("open"), exportButton = $("export"), fileInput = $("file");
let database = { name: null, format: "minidb" };  // what bridge.info() says
let shownPage = null, shownFor = null;  // the page whose layout is shown, and the tree it was shown with

// ---- the worker ---------------------------------------------------------------

const worker = new Worker("worker.js?v=__BUILD__", { type: "module" });
let nextId = 0;
const pending = new Map();
worker.onmessage = ({ data }) => {
  const { resolve, reject } = pending.get(data.id);
  pending.delete(data.id);
  data.error !== undefined ? reject(new Error(data.error)) : resolve(data.result);
};
function call(fn, ...args) {
  return new Promise((resolve, reject) => {
    const id = nextId++;
    pending.set(id, { resolve, reject });
    worker.postMessage({ id, fn, args });
  });
}

// ---- editor -----------------------------------------------------------------

function load(key) {
  try { return localStorage.getItem(key); } catch { return null; }
}
function save(key, value) {
  try { localStorage.setItem(key, value); } catch { /* private mode: fine */ }
}

const placeholder = new Option("示例", "");
placeholder.disabled = true;
examples.add(placeholder);  // (shown when the editor holds something else)
for (const [name] of EXAMPLES) examples.add(new Option(name, name));
const saved = load(STORAGE_KEY);
editor.value = saved !== null && saved.trim() ? saved : EXAMPLES[0][1];
const matching = EXAMPLES.find(([, text]) => text === editor.value);
if (matching) examples.value = matching[0];
else examples.value = "";

examples.addEventListener("change", () => {
  const example = EXAMPLES.find(([name]) => name === examples.value);
  editor.value = example[1];
  save(STORAGE_KEY, editor.value);
  run();
});
editor.addEventListener("input", () => {
  save(STORAGE_KEY, editor.value);
  if (!EXAMPLES.some(([, text]) => text === editor.value)) examples.value = "";
});
editor.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
    event.preventDefault();
    run();
  } else if (event.key === "Tab" && !event.shiftKey) {
    event.preventDefault();
    editor.setRangeText("  ", editor.selectionStart, editor.selectionEnd, "end");
    editor.dispatchEvent(new Event("input"));
  }
});
runButton.addEventListener("click", run);
resetButton.addEventListener("click", async () => {
  await call("reset");
  output.replaceChildren();
  shownPage = null;
  await refreshInfo();
  statusLine.textContent = "数据库已清空";
  await refreshTree();
});
objectSelect.addEventListener("change", refreshTree);
runButton.title = /Mac|iPhone|iPad/.test(navigator.platform) ? "⌘ + Enter" : "Ctrl + Enter";

// ---- opening and exporting SQLite files ------------------------------------------

const SCHEMA_QUERY = "SELECT type, name, tbl_name, rootpage FROM sqlite_schema;\n";

openButton.addEventListener("click", () => fileInput.click());
fileInput.addEventListener("change", () => {
  if (fileInput.files.length) openFile(fileInput.files[0]);
  fileInput.value = "";
});
let dragDepth = 0;
const dropHint = $("drop");
const carriesFiles = (event) => event.dataTransfer && [...event.dataTransfer.types].includes("Files");
document.addEventListener("dragenter", (event) => {
  if (!carriesFiles(event) || openButton.disabled) return;
  dragDepth++;
  dropHint.hidden = false;
});
document.addEventListener("dragleave", () => {
  if (dragDepth && --dragDepth === 0) dropHint.hidden = true;
});
document.addEventListener("dragover", (event) => {
  if (carriesFiles(event)) event.preventDefault();
});
document.addEventListener("drop", (event) => {
  if (!carriesFiles(event)) return;
  event.preventDefault();
  dragDepth = 0;
  dropHint.hidden = true;
  if (event.dataTransfer.files.length && !openButton.disabled) openFile(event.dataTransfer.files[0]);
});

async function openFile(file) {
  if (running) return;
  running = true;
  setBusy(true);
  statusLine.classList.remove("error");
  statusLine.textContent = `正在打开 ${file.name}…`;
  try {
    const data = new Uint8Array(await file.arrayBuffer());
    await call("open_file", data, file.name);
    shownPage = null;
    await refreshInfo();
    output.replaceChildren();
    statusLine.textContent = `已打开 ${file.name}`;
    // An example (or nothing of the user's) in the editor: show the schema instead.
    if (!editor.value.trim() || EXAMPLES.some(([, text]) => text === editor.value)) {
      editor.value = SCHEMA_QUERY;
      examples.value = "";
      save(STORAGE_KEY, editor.value);
    }
  } catch (error) {
    statusLine.classList.add("error");
    statusLine.textContent = `打不开 ${file.name}：${error.message}`;
    return;
  } finally {
    running = false;
    setBusy(false);
  }
  if (editor.value === SCHEMA_QUERY) await run();
  else await refreshTree();
}

exportButton.addEventListener("click", async () => {
  try {
    const data = await call("export");
    const url = URL.createObjectURL(new Blob([data], { type: "application/vnd.sqlite3" }));
    const link = element("a");
    link.href = url;
    link.download = database.name || "minidb.sqlite";
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 10_000);
    statusLine.classList.remove("error");
    statusLine.textContent = `已导出 ${link.download}（${formatBytes(data.length)}）`;
  } catch (error) {
    statusLine.classList.add("error");
    statusLine.textContent = `导出失败：${error.message}`;
  }
});

async function refreshInfo() {
  database = JSON.parse(await call("info"));
  const sqlite = database.format === "sqlite";
  $("dbinfo").textContent = sqlite
    ? `${database.name || "SQLite 数据库"} · SQLite 格式 · ${database.pages} 页 · ${formatBytes(database.bytes)}`
    : "内存数据库（MiniDB 格式）";
  exportButton.disabled = !sqlite;
  $("pagearea").hidden = !sqlite;
  // (SQLite's tables are B+ trees, its indexes B-trees with entries on every level)
  $("treelabel").textContent = sqlite ? "B 树" : "B+ 树";
}

function formatBytes(n) {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(n < 10240 ? 1 : 0)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

function setBusy(busy) {
  for (const button of [runButton, openButton, resetButton]) button.disabled = busy;
  exportButton.disabled = busy || database.format !== "sqlite";
}

// ---- running SQL --------------------------------------------------------------

let running = false;
async function run() {
  if (running || runButton.disabled) return;
  running = true;
  setBusy(true);
  statusLine.classList.remove("error");
  statusLine.textContent = "运行中…";
  const started = performance.now();
  try {
    const results = JSON.parse(await call("run", editor.value));
    output.replaceChildren(...results.map(renderResult));
    const failed = results.some((r) => r.error !== undefined);
    statusLine.textContent = `${results.length} 条语句${failed ? "，出错停止" : ""} · ${Math.round(performance.now() - started)} ms`;
    await refreshInfo();
    await refreshTree();
  } catch (error) {
    statusLine.classList.add("error");
    statusLine.textContent = String(error.message || error);
  } finally {
    running = false;
    setBusy(false);
  }
}

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function renderResult(result) {
  const box = element("div", "result");
  box.append(element("div", "sql", result.sql));
  if (result.plan && result.plan.length) {
    const plan = element("div", "plan");
    plan.append(element("b", "", "计划"));
    plan.append(result.plan.map(([table, how]) => `${table}: ${how}`).join("  ·  "));
    box.append(plan);
  }
  if (result.error !== undefined) {
    box.append(element("div", "error", result.error));
  } else if (result.columns.length) {
    box.append(renderTable(result.columns, result.rows));
  } else {
    box.append(element("div", "note", result.rowcount >= 0 ? `${result.rowcount} 行受影响` : "完成"));
  }
  return box;
}

function renderTable(columns, rows) {
  const wrap = element("div", "table-wrap");
  const table = element("table");
  const head = table.createTHead().insertRow();
  for (const column of columns) head.append(element("th", "", column));
  const body = table.createTBody();
  for (const row of rows.slice(0, MAX_ROWS)) {
    const tr = body.insertRow();
    for (const value of row) {
      if (value === null) tr.append(element("td", "null", "NULL"));
      else if (typeof value === "number") tr.append(element("td", "num", String(value)));
      else if (typeof value === "object" && "blob" in value) tr.append(element("td", "", `x'${value.blob}'`));
      else if (typeof value === "object") tr.append(element("td", "num", value.real));
      else tr.append(element("td", "", value));
    }
  }
  wrap.append(table);
  const box = element("div");
  box.append(wrap);
  if (rows.length > MAX_ROWS) box.append(element("div", "note", `只显示前 ${MAX_ROWS} 行（共 ${rows.length} 行）`));
  else if (!rows.length) box.append(element("div", "note", "0 行"));
  return box;
}

// ---- the B+ tree ------------------------------------------------------------------

let treeGeneration = 0;  // a newer refresh makes an older one stop

async function refreshTree() {
  const generation = ++treeGeneration;
  const current = objectSelect.value;
  const objects = JSON.parse(await call("objects"));
  if (generation !== treeGeneration) return;
  objectSelect.replaceChildren(...objects.map((o) =>
    new Option(o.kind === "index" ? `索引 ${o.name}（${o.table}）` : `表 ${o.name}`, o.name)));
  const tree = $("tree");
  if (!objects.length) {
    $("treeinfo").textContent = "";
    tree.replaceChildren(element("p", "empty", "还没有表。"));
    return;
  }
  const name = objects.some((o) => o.name === current) ? current : objects[0].name;
  objectSelect.value = name;
  const data = JSON.parse(await call("tree", name));
  if (generation !== treeGeneration) return;
  drawTree(tree, data);
  if (database.format === "sqlite") {
    const map = JSON.parse(await call("file_map"));
    if (generation !== treeGeneration) return;
    drawFileMap(map, name);
    const keep = shownPage && shownPage <= database.pages && shownFor === name;
    shownFor = name;
    await showPage(keep ? shownPage : data.root);
  }
}

function drawTree(container, data) {
  const WIDTH = 132, GAP = 10, VGAP = 34;
  const levels = data.levels;
  const x = new Map();  // page -> left edge
  levels[levels.length - 1].forEach((node, i) => x.set(node.page, i * (WIDTH + GAP)));
  for (let l = levels.length - 2; l >= 0; l--) {
    let right = -Infinity;
    for (const node of levels[l]) {
      const shown = node.children.filter((c) => x.has(c)).map((c) => x.get(c));
      let left = shown.length ? (Math.min(...shown) + Math.max(...shown)) / 2 : right + GAP;
      left = Math.max(left, right + GAP);
      x.set(node.page, left);
      right = left + WIDTH;
    }
  }
  const pages = levels.reduce((n, level) => n + level.length, 0);
  const sqlite = database.format === "sqlite";
  $("treeinfo").textContent =
    `${data.depth} 层 · ${data.keys} 个 key · 显示 ${pages} 页${data.hidden ? `（另有 ${data.hidden} 页未画出）` : ""}`;

  const canvas = element("div");
  canvas.style.position = "relative";
  const boxes = new Map();
  for (const level of levels) {
    for (const node of level) {
      const box = element("div", `node ${node.leaf ? "leaf" : "internal"}`);
      box.style.position = "absolute";
      box.style.left = `${x.get(node.page)}px`;
      box.style.width = `${WIDTH}px`;
      box.style.maxWidth = "none";
      box.title = `第 ${node.page} 页 · ${node.leaf ? "叶子" : "内部节点"} · ${node.count} 个 key · 填充 ${Math.round(node.fill * 100)}%`
        + (sqlite ? " · 点击查看页面布局" : "");
      box.dataset.page = node.page;
      if (sqlite) {
        box.classList.add("clickable");
        box.addEventListener("click", () => showPage(node.page));
      }
      box.append(element("div", "head", `p${node.page} · ${node.count} key${node.leaf ? "" : " · 内部"}`));
      const keys = node.keys.slice();
      keys.forEach((key, i) => {
        if (node.elided && i === keys.length - 1) box.append(element("div", "key more", "…"));
        box.append(element("div", "key", key));
      });
      const fill = element("div", "fill");
      const bar = element("i");
      bar.style.width = `${Math.min(100, node.fill * 100)}%`;
      fill.append(bar);
      box.append(fill);
      canvas.append(box);
      boxes.set(node.page, box);
    }
  }
  container.replaceChildren(canvas);
  // Heights are known only now: place the levels, then draw the edges.
  let top = 0;
  const tops = [];
  for (const level of levels) {
    tops.push(top);
    let tallest = 0;
    for (const node of level) {
      const box = boxes.get(node.page);
      box.style.top = `${top}px`;
      tallest = Math.max(tallest, box.offsetHeight);
    }
    top += tallest + VGAP;
  }
  const width = Math.max(...[...x.values()]) + WIDTH + 4;
  const height = top - VGAP + 4;
  canvas.style.width = `${width}px`;
  canvas.style.height = `${height}px`;
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", "edges");
  svg.setAttribute("width", width);
  svg.setAttribute("height", height);
  const line = (x1, y1, x2, y2, dashed) => {
    const l = document.createElementNS("http://www.w3.org/2000/svg", "line");
    for (const [k, v] of Object.entries({ x1, y1, x2, y2 })) l.setAttribute(k, v);
    if (dashed) l.setAttribute("stroke-dasharray", "3 3");
    svg.append(l);
  };
  levels.forEach((level, l) => {
    for (const node of level) {
      const box = boxes.get(node.page);
      for (const child of node.children) {
        if (!boxes.has(child)) continue;
        line(x.get(node.page) + WIDTH / 2, tops[l] + box.offsetHeight,
             x.get(child) + WIDTH / 2, tops[l + 1]);
      }
    }
  });
  const leaves = levels[levels.length - 1];
  for (let i = 0; i + 1 < leaves.length; i++) {  // the sibling chain
    const y = tops[levels.length - 1] + 11;
    line(x.get(leaves[i].page) + WIDTH, y, x.get(leaves[i + 1].page), y, true);
  }
  canvas.prepend(svg);
  if (width < container.clientWidth) canvas.style.margin = "0 auto";
  // Start with the root in view: it sits above the middle of what is drawn.
  const root = levels[0][0];
  container.scrollLeft = Math.max(0, x.get(root.page) + WIDTH / 2 - container.clientWidth / 2);
  container.scrollTop = 0;
}

// ---- start ------------------------------------------------------------------------

call("ready").then(async () => {
  statusLine.textContent = "就绪 · Ctrl/⌘ + Enter 运行";
  setBusy(false);
  if (EXAMPLES.some(([, text]) => text === editor.value)) await run();
}, (error) => {
  statusLine.classList.add("error");
  statusLine.textContent = `加载失败：${error.message}`;
});

// ---- SQLite's format: the file's pages and one page's layout -------------------------

const PAGE_KINDS = {
  "table-leaf": "表叶子页", "table-interior": "表内部页", "index-leaf": "索引叶子页",
  "index-interior": "索引内部页", "overflow": "溢出页", "freelist-trunk": "空闲列表主干页",
  "freelist-leaf": "空闲页", "lock-byte": "锁字节页", "unknown": "未识别",
};
const REGION_KINDS = {
  "file-header": "文件头", "page-header": "页头", "pointers": "单元格指针", "unallocated": "未分配",
  "cell": "单元格", "freeblock": "空闲块", "fragment": "碎片",
};
const BTREE_KINDS = new Set(["table-leaf", "table-interior", "index-leaf", "index-interior"]);
let fileMap = null;

function drawFileMap(data, selected) {
  fileMap = data;
  const map = $("filemap");
  const owner = fileMap.owners.indexOf(selected);
  const counts = {};
  const squares = fileMap.pages.map(([kind, who], i) => {
    counts[kind] = (counts[kind] || 0) + 1;
    const square = element("i", `p-${kind}${who === owner ? " mine" : ""}`);
    square.dataset.page = i + 1;
    square.title = `第 ${i + 1} 页 · ${PAGE_KINDS[kind]}${who >= 0 ? ` · ${fileMap.owners[who]}` : ""}`;
    return square;
  });
  map.replaceChildren(...squares);
  map.classList.toggle("dense", fileMap.pages.length > 2000);
  $("mapinfo").textContent = `${fileMap.pages.length} 页，每格一页；高亮的是 ${selected} 的页 · `
    + Object.entries(counts).map(([kind, n]) => `${PAGE_KINDS[kind]} ${n}`).join(" · ");
}

$("filemap").addEventListener("click", (event) => {
  const page = Number(event.target.dataset && event.target.dataset.page);
  if (page) showPage(page);
});

async function showPage(pgno) {
  shownPage = pgno;
  for (const node of document.querySelectorAll(".node.current, #filemap i.current")) node.classList.remove("current");
  for (const node of document.querySelectorAll(`.node[data-page="${pgno}"], #filemap i[data-page="${pgno}"]`)) {
    node.classList.add("current");
  }
  const kind = fileMap ? fileMap.pages[pgno - 1][0] : "unknown";
  $("pagetitle").textContent = `第 ${pgno} 页`;
  const bar = $("pagebar"), cells = $("cells"), legend = $("legend");
  if (!BTREE_KINDS.has(kind)) {
    $("pageinfo").textContent = `${PAGE_KINDS[kind]}（只画 B 树页的布局）`;
    bar.replaceChildren();
    legend.replaceChildren();
    cells.replaceChildren();
    return;
  }
  const layout = JSON.parse(await call("page", pgno));
  if (pgno !== shownPage) return;  // (another page was asked for meanwhile)
  if (layout.error) {
    $("pageinfo").textContent = layout.error;
    return;
  }
  $("pagescale").replaceChildren(...[0, 1, 2, 3, 4].map((q) => element("span", "", String(layout.size * q / 4))));
  const unallocated = layout.regions.find((r) => r.kind === "unallocated");
  const freeblocks = layout.regions.filter((r) => r.kind === "freeblock").reduce((n, r) => n + r.end - r.start, 0);
  $("pageinfo").textContent = `${PAGE_KINDS[layout.kind]} · ${layout.cells} 个单元格 · 内容区从 ${layout.content_start} 开始 · `
    + `空闲 ${layout.free} 字节（未分配 ${unallocated.end - unallocated.start}，空闲块 ${freeblocks}，碎片 ${layout.fragmented}）`
    + (layout.right ? ` · 最右子页 ${layout.right}` : "");
  bar.replaceChildren(...layout.regions.filter((r) => r.end > r.start).map((r) => {
    const part = element("i", `r-${r.kind}${r.kind === "cell" && r.cell % 2 ? " odd" : ""}`);
    part.style.left = `${(r.start / layout.size) * 100}%`;
    part.style.width = `${((r.end - r.start) / layout.size) * 100}%`;
    const what = r.kind === "cell" ? `单元格 #${r.cell}` : REGION_KINDS[r.kind];
    part.title = `${what} · 字节 ${r.start}–${r.end - 1} · ${r.end - r.start} 字节`;
    if (r.kind === "cell") part.dataset.cell = r.cell;
    return part;
  }));
  const present = new Set(layout.regions.map((r) => r.kind));
  legend.replaceChildren(...Object.entries(REGION_KINDS).filter(([kind]) => present.has(kind)).map(([kind, name]) => {
    const item = element("span");
    item.append(element("i", `r-${kind}`), name);
    return item;
  }));
  const table = element("table");
  const head = table.createTHead().insertRow();
  const leafTable = layout.kind === "table-leaf", interior = layout.kind.endsWith("interior");
  const columns = ["#", "偏移", "字节", leafTable ? "rowid" : layout.kind === "table-interior" ? "rowid 上界" : "索引项 | rowid"];
  if (layout.kind !== "table-interior") columns.push("负载");
  if (interior) columns.push("左子页");
  columns.push("溢出页");
  for (const name of columns) head.append(element("th", "", name));
  const body = table.createTBody();
  for (const cell of layout.listed) {
    const row = body.insertRow();
    row.dataset.cell = cell.index;
    const values = [cell.index, cell.offset, cell.size, cell.key];
    if (layout.kind !== "table-interior") values.push(cell.payload);
    if (interior) values.push(cell.child);
    values.push(cell.overflow ? `${cell.overflow[0]}（共 ${cell.overflow[1]} 页）` : "");
    values.forEach((value, i) => row.append(element("td", i < 3 || typeof value === "number" ? "num" : "", String(value))));
  }
  const wrap = element("div", "table-wrap");
  wrap.append(table);
  cells.replaceChildren(wrap);
  if (layout.listed.length < layout.cells) {
    cells.append(element("div", "note", `只列出前 ${layout.listed.length} 个单元格（共 ${layout.cells} 个）`));
  }
}

$("pagebar").addEventListener("click", (event) => {
  const cell = event.target.dataset && event.target.dataset.cell;
  if (cell === undefined) return;
  const row = document.querySelector(`#cells tr[data-cell="${cell}"]`);
  if (!row) return;
  for (const other of document.querySelectorAll("#cells tr.current")) other.classList.remove("current");
  row.classList.add("current");
  row.scrollIntoView({ block: "nearest" });
});
