"use strict";
importScripts("python/pyodide.js");

const siteKey = "samokat-demo:" + new URL(".", self.location.href).pathname;
const dbFile = "/history.sqlite3";

function openStorage() {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open(siteKey, 1);
    request.onupgradeneeded = () => request.result.createObjectStore("files");
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}

function snapshot(db, data) {
  return new Promise((resolve, reject) => {
    const tx = db.transaction("files", data ? "readwrite" : "readonly");
    const store = tx.objectStore("files");
    const request = data ? store.put(data, "sqlite") : store.get("sqlite");
    tx.oncomplete = () => resolve(request.result);
    tx.onabort = () => reject(tx.error || new Error("Не удалось сохранить данные"));
  });
}

async function start() {
  const indexURL = new URL("python/", self.location.href).href;
  const [py, db, response] = await Promise.all([
    loadPyodide({indexURL}), openStorage(), fetch("shop-engine.zip?v=meals-1", {cache:"no-cache"})
  ]);
  if (!response.ok) throw new Error("Не удалось загрузить модель");
  await py.loadPackage(["sqlite3", "tzdata"]);
  py.unpackArchive(await response.arrayBuffer(), "zip", {extractDir:"/shop"});
  py.runPython(`
import sys, os
sys.path.insert(0, '/shop')
os.environ['SAMOKAT_DB'] = '/history.sqlite3'
import store
from pages_bridge import dispatch
`);
  return {py, db, dispatch:py.globals.get("dispatch")};
}

let runtime;
async function handle({method, path, body}) {
  runtime ??= start();
  const {py, db, dispatch} = await runtime;
  const call = () => JSON.parse(dispatch(method, path, JSON.stringify(body ?? {})));
  if (path === "/api/catalog") return call();
  // Every tab restores the latest committed SQLite image while holding the same lock.
  return navigator.locks.request(siteKey, async () => {
    const saved = await snapshot(db);
    let migrated = false;
    if (saved) {
      py.FS.writeFile(dbFile, saved);
      migrated = py.runPython("store.needs_history_update()");
      if (migrated) py.runPython("store.initialize()");
    } else {
      if (py.FS.analyzePath(dbFile).exists) py.FS.unlink(dbFile);
      py.runPython("store.initialize()");
    }
    const result = call();
    if (!saved || migrated || (!result.error && (method !== "GET" || path.endsWith("/recommendations/page")))) {
      await snapshot(db, py.FS.readFile(dbFile));
    }
    return result;
  });
}

self.onmessage = async ({data}) => {
  try {
    self.postMessage({id:data.id, ...await handle(data)});
  } catch (error) {
    console.error(error);
    self.postMessage({id:data.id, error:"Не удалось открыть данные магазина. Проверьте доступ к хранилищу браузера."});
  }
};
