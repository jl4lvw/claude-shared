/* /r 遠隔指示 PWA. Google Todo 風 minimal UI. */
"use strict";

const APP_BUILD_TIME = "20260506-191900";

// API base: Caddy で /tasksapi/* → 127.0.0.1:8086 に reverse_proxy
// LAN 直叩き / sfuji.f5.si / localhost いずれでも動くよう相対パスで揃える。
const API_BASE = (() => {
  const { origin, pathname } = window.location;
  // /tasks/r/... or /tasks/r → /tasksapi
  if (pathname.includes("/tasks/")) return origin + "/tasksapi/r";
  // localhost dev
  return origin + "/tasksapi/r";
})();

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => document.querySelectorAll(sel);

const state = {
  tab: "unconsumed", // unconsumed | consumed | archived
  items: [],
  editing: null, // detail object
};

async function api(path, opts = {}) {
  const url = API_BASE + path;
  const headers = { "Content-Type": "application/json", ...(opts.headers || {}) };
  const res = await fetch(url, { ...opts, headers });
  if (!res.ok) {
    let msg = `HTTP ${res.status}`;
    try {
      const j = await res.json();
      msg = j.detail ? (typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail)) : msg;
    } catch (_) {}
    throw new Error(msg);
  }
  if (res.status === 204) return null;
  return res.json();
}

function setBadge(ok, text) {
  const b = $("#conn-badge");
  b.textContent = text;
  b.classList.toggle("ok", !!ok);
  b.classList.toggle("ng", !ok);
}

function fmtJST(iso) {
  if (!iso) return "";
  try {
    const d = new Date(iso);
    const z = (n) => String(n).padStart(2, "0");
    return `${d.getMonth() + 1}/${z(d.getDate())} ${z(d.getHours())}:${z(d.getMinutes())}`;
  } catch (_) {
    return iso;
  }
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  }[c]));
}

async function loadList() {
  const params = new URLSearchParams();
  if (state.tab === "unconsumed") {
    params.set("status", "unconsumed");
    params.set("archived", "0");
  } else if (state.tab === "consumed") {
    params.set("status", "consumed");
    params.set("archived", "0");
  } else {
    params.set("status", "all");
    params.set("archived", "1");
  }
  try {
    const items = await api("/?" + params.toString());
    state.items = items;
    renderList();
    setBadge(true, "OK");
  } catch (e) {
    setBadge(false, "ERR");
    $("#list").innerHTML = `<div class="empty">読み込み失敗: ${escapeHtml(e.message)}</div>`;
  }
}

function renderList() {
  const list = $("#list");
  if (!state.items.length) {
    const msg = {
      unconsumed: "未取り込みの指示はありません",
      consumed: "取り込み済みの指示はありません",
      archived: "アーカイブはありません",
    }[state.tab];
    list.innerHTML = `<div class="empty">${msg}</div>`;
  } else {
    list.innerHTML = state.items.map(itemHtml).join("");
  }
  $("#count-info").textContent = `${state.items.length} 件`;
}

function itemHtml(it) {
  const cls = ["item"];
  if (it.consumed_at) cls.push("consumed");
  if (it.archived) cls.push("archived");
  const meta = [];
  meta.push(`作成: ${fmtJST(it.created_at)}`);
  if (it.consumed_at) meta.push(`取込: ${fmtJST(it.consumed_at)}`);
  if (it.updated_at && it.updated_at !== it.created_at) meta.push(`更新: ${fmtJST(it.updated_at)}`);

  const actions = [];
  if (state.tab === "unconsumed") {
    actions.push(`<button data-act="edit" data-code="${it.code}" title="編集">✏️</button>`);
    actions.push(`<button data-act="archive" data-code="${it.code}" title="アーカイブ">🗄️</button>`);
  } else if (state.tab === "consumed") {
    actions.push(`<button data-act="restore" data-code="${it.code}" title="再投入">↩️</button>`);
    actions.push(`<button data-act="archive" data-code="${it.code}" title="アーカイブ">🗄️</button>`);
  } else {
    actions.push(`<button data-act="archive" data-code="${it.code}" title="復元">📤</button>`);
  }

  return `
    <div class="${cls.join(" ")}">
      <span class="code-chip">${escapeHtml(it.code)}</span>
      <div class="item-main">
        <div class="body">${escapeHtml(it.body)}</div>
        <div class="meta">${meta.join(" · ")}</div>
      </div>
      <div class="actions">${actions.join("")}</div>
    </div>
  `;
}

async function addInstruction() {
  const body = $("#t-body").value.trim();
  const code = $("#t-code").value.trim();
  if (!body) {
    $("#t-body").focus();
    return;
  }
  if (code && !/^\d{3,4}$/.test(code)) {
    alert("番号は 3 桁または 4 桁の数字で指定してください");
    return;
  }
  $("#btn-add").disabled = true;
  try {
    const payload = { body };
    if (code) payload.code = code;
    await api("/", { method: "POST", body: JSON.stringify(payload) });
    $("#t-body").value = "";
    $("#t-code").value = "";
    state.tab = "unconsumed";
    syncTabs();
    await loadList();
  } catch (e) {
    alert("追加失敗: " + e.message);
  } finally {
    $("#btn-add").disabled = false;
  }
}

async function openEditor(code) {
  try {
    const detail = await api("/" + encodeURIComponent(code));
    state.editing = detail;
    $("#m-code").textContent = code;
    $("#m-body").value = detail.body;
    $("#m-reason").value = "";
    const vlist = $("#m-versions-list");
    vlist.innerHTML = (detail.versions || []).map((v) => `
      <div class="v">
        <div class="v-meta">${fmtJST(v.created_at)} · ${escapeHtml(v.reason || "")}</div>
        <div class="v-body">${escapeHtml(v.body)}</div>
      </div>
    `).join("") || '<div class="v-meta">履歴なし</div>';
    $("#modal").classList.remove("hidden");
  } catch (e) {
    alert("読込失敗: " + e.message);
  }
}

async function saveEditor() {
  if (!state.editing) return;
  const body = $("#m-body").value.trim();
  const reason = $("#m-reason").value.trim();
  if (!body) {
    alert("本文を入力してください");
    return;
  }
  try {
    await api("/" + encodeURIComponent(state.editing.code), {
      method: "PATCH",
      body: JSON.stringify({ body, reason }),
    });
    closeEditor();
    await loadList();
  } catch (e) {
    alert("保存失敗: " + e.message);
  }
}

function closeEditor() {
  $("#modal").classList.add("hidden");
  state.editing = null;
}

async function actRestore(code) {
  try {
    await api("/" + encodeURIComponent(code) + "/restore", { method: "POST" });
    await loadList();
  } catch (e) {
    alert("再投入失敗: " + e.message);
  }
}

async function actArchive(code) {
  try {
    await api("/" + encodeURIComponent(code) + "/archive", { method: "POST" });
    await loadList();
  } catch (e) {
    alert("アーカイブ操作失敗: " + e.message);
  }
}

function syncTabs() {
  $$(".tab").forEach((b) => b.classList.toggle("active", b.dataset.tab === state.tab));
}

function bind() {
  $("#btn-add").addEventListener("click", addInstruction);
  $("#t-body").addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
      e.preventDefault();
      addInstruction();
    }
  });
  $("#btn-refresh").addEventListener("click", loadList);

  $$(".tab").forEach((b) => b.addEventListener("click", () => {
    state.tab = b.dataset.tab;
    syncTabs();
    loadList();
  }));

  $("#list").addEventListener("click", (ev) => {
    const btn = ev.target.closest("button[data-act]");
    if (!btn) return;
    const act = btn.dataset.act;
    const code = btn.dataset.code;
    if (act === "edit") openEditor(code);
    else if (act === "restore") actRestore(code);
    else if (act === "archive") actArchive(code);
  });

  $("#m-close").addEventListener("click", closeEditor);
  $("#m-cancel").addEventListener("click", closeEditor);
  $("#m-save").addEventListener("click", saveEditor);
  $("#modal").addEventListener("click", (ev) => {
    if (ev.target.id === "modal") closeEditor();
  });
}

document.addEventListener("DOMContentLoaded", () => {
  bind();
  loadList();
});

// Service worker (キャッシュ刷新は ?v= で吸収)
if ("serviceWorker" in navigator) {
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("sw.js?v=" + APP_BUILD_TIME).catch(() => {});
  });
}
