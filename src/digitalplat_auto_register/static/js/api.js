/* ============================================================
   api.js — shared fetch helper, escapeHtml, formatting, badge, toast
   Loaded by every page. All page-specific JS lives in <page>.js.
   ============================================================ */

(function (global) {
  "use strict";

  /* Escape user-controlled HTML */
  function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>'"]/g, (ch) => ({
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      "'": "&#39;",
      '"': "&quot;",
    })[ch]);
  }

  /* Thin wrapper around fetch with JSON handling + error unwrapping */
  async function api(path, options) {
    let opts = options || {};
    const headers = Object.assign({}, opts.headers || {});
    if (opts.body && typeof opts.body !== "string" && !(opts.body instanceof FormData)) {
      opts = Object.assign({}, opts, { body: JSON.stringify(opts.body) });
      headers["Content-Type"] = "application/json";
    }
    const response = await fetch(path, Object.assign({}, opts, { headers }));
    let data = null;
    const text = await response.text();
    try { data = text ? JSON.parse(text) : null; } catch (_) { data = text; }
    if (!response.ok) {
      const detail =
        (data && (data.detail || data.error || data.message)) ||
        ("HTTP " + response.status);
      const err = new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
      err.status = response.status;
      err.data = data;
      throw err;
    }
    return data;
  }

  /* Status label + class mapping */
  const STATUS_LABELS = {
    running: "运行中",
    registering: "注册中",
    succeeded: "成功",
    success: "成功",
    completed: "已完成",
    failed: "失败",
    pending: "待注册",
    paused: "已暂停",
    expired: "已过期",
    active: "活跃",
    valid: "有效",
    invalid: "异常",
    untested: "未测试",
    renewed: "已续期",
    unmanaged: "未托管",
    skipped: "已跳过",
  };

  /* Canonical badge css class per status */
  const STATUS_CLASS = {
    running: "info",
    registering: "info",
    pending: "warn",
    paused: "warn",
    succeeded: "ok",
    success: "ok",
    completed: "ok",
    active: "ok",
    valid: "ok",
    renewed: "ok",
    failed: "err",
    invalid: "err",
    expired: "warn",
    untested: "gray",
    unmanaged: "gray",
    skipped: "gray",
  };

  function statusBadge(status) {
    const cls = STATUS_CLASS[status] || "gray";
    const label = STATUS_LABELS[status] || status || "unknown";
    const anim = status === "running" || status === "registering" ? " badge-running" : "";
    return (
      '<span class="badge badge-' +
      escapeHtml(cls) + anim +
      '">' +
      escapeHtml(label) +
      "</span>"
    );
  }

  function formatTime(value) {
    if (!value) return "-";
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? escapeHtml(value) : date.toLocaleString();
  }

  function formatClock(value) {
    /* HH:MM:SS */
    if (!value) return "-";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return escapeHtml(value);
    return date.toLocaleTimeString();
  }

  function formatDuration(seconds) {
    if (!Number.isFinite(seconds)) return "-";
    if (seconds < 60) return seconds.toFixed(1) + "s";
    const m = Math.floor(seconds / 60);
    const s = Math.round(seconds % 60);
    return m + "m " + s + "s";
  }

  function truncateId(id, visible) {
    if (!id) return "-";
    const v = String(id);
    const n = visible || 12;
    return v.length > n ? v.slice(0, n) + "…" : v;
  }

  /* Toast */
  function showToast(message, type) {
    type = type || "success";
    const el = document.createElement("div");
    el.className = "toast" + (type === "error" ? " error" : type === "warn" ? " warn" : type === "info" ? " info" : "");
    el.textContent = message;
    document.body.appendChild(el);
    setTimeout(() => {
      el.classList.add("hiding");
      setTimeout(() => el.remove(), 220);
    }, 3200);
  }

  /* Connection indicator */
  function setConnected(el, ok, ts) {
    if (!el) return;
    el.classList.toggle("error", !ok);
    el.innerHTML = "";
    const dot = document.createElement("span");
    dot.className = "conn-dot";
    el.appendChild(dot);
    const text = document.createElement("span");
    text.textContent = ok ? "已连接 · " + (ts || new Date().toLocaleTimeString()) : "服务连接失败";
    el.appendChild(text);
  }

  /* Exports */
  global.DP = {
    escapeHtml,
    api,
    statusBadge,
    formatTime,
    formatClock,
    formatDuration,
    truncateId,
    showToast,
    setConnected,
    STATUS_LABELS,
    STATUS_CLASS,
  };
})(window);
