// Runs MiniDB in Pyodide, off the page's thread (a module worker: Pyodide
// 314 does not start in a classic one).  Messages: {id, fn, args} ->
// {id, result} or {id, error}; fn is a function of bridge.py.
import { loadPyodide } from "https://cdn.jsdelivr.net/pyodide/v314.0.7/full/pyodide.mjs";

let bridge = null;
const ready = (async () => {
  const pyodide = await loadPyodide();
  const response = await fetch("minidb.zip?v=__BUILD__");
  if (!response.ok) throw new Error(`minidb.zip: HTTP ${response.status}`);
  pyodide.unpackArchive(await response.arrayBuffer(), "zip", { extractDir: "/home/pyodide/app" });
  pyodide.runPython("import sys; sys.path.insert(0, '/home/pyodide/app')");
  bridge = pyodide.pyimport("bridge");
})();

onmessage = async ({ data }) => {
  try {
    await ready;
    const result = data.fn === "ready" ? null : bridge[data.fn](...data.args);
    postMessage({ id: data.id, result });
  } catch (error) {
    postMessage({ id: data.id, error: String(error && error.message || error) });
  }
};
