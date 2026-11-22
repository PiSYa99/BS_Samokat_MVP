"use strict";
(() => {
  const worker = new Worker("pages-worker.js");
  const pending = new Map();
  let nextId = 0;
  let failure;
  worker.onmessage = ({data}) => {
    const task = pending.get(data.id);
    if (!task) return;
    pending.delete(data.id);
    if (data.error) task.reject(Object.assign(new Error(data.error), {status:data.status}));
    else task.resolve(data.value);
  };
  worker.onerror = () => {
    failure = new Error("Не удалось запустить Python в браузере.");
    for (const task of pending.values()) task.reject(failure);
    pending.clear();
  };
  window.pagesRequest = (path, method, body) => new Promise((resolve, reject) => {
    if (failure) return reject(failure);
    const id = ++nextId;
    pending.set(id, {resolve, reject});
    worker.postMessage({id, path, method, body});
  });
})();
