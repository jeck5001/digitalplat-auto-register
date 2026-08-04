/* ============================================================
   pool.js — account pool dashboard JS.
   Depends on `DP` from api.js.
   ============================================================ */

(function () {
  "use strict";

  const { api, escapeHtml, statusBadge, formatTime, showToast, setConnected, truncateId } = window.DP;

  /* =============== Tab switching =============== */
  function showTab(tabName, btn) {
    document.querySelectorAll(".tab-content").forEach((el) => el.classList.remove("active"));
    document.querySelectorAll(".tab").forEach((el) => el.classList.remove("active"));
    const target = document.getElementById("tab-" + tabName);
    if (target) target.classList.add("active");
    if (btn) btn.classList.add("active");
    if (tabName === "stats") loadDashboardStats();
    if (tabName === "logs") loadLogs();
    if (tabName === "health") loadHealth();
  }

  /* =============== Header stats =============== */
  async function loadStats() {
    try {
      const d = await api("/api/v2/pool");
      setText("total-accounts", d.total_accounts || 0);
      setText("available-accounts", d.available_accounts || 0);
      setText("active-accounts", d.active_accounts || 0);
      const health = (d.pool_health || 0) * 100;
      const el = document.getElementById("pool-health");
      if (el) el.textContent = health.toFixed(0) + "%";
      setConnected(document.getElementById("updated"), true);
    } catch (e) {
      setConnected(document.getElementById("updated"), false);
    }
  }

  function setText(id, val) {
    const el = document.getElementById(id);
    if (el) el.textContent = val;
  }

  /* =============== Accounts tab =============== */
  async function loadAccounts() {
    try {
      const accounts = await api("/api/v2/pool/accounts");
      const tbody = document.getElementById("accounts-table");
      if (!accounts || !accounts.length) {
        tbody.innerHTML =
          '<tr><td colspan="7"><div class="empty"><div class="empty-icon">◉</div><div class="empty-title">池中暂无账户</div><div class="empty-hint">使用「迁移」按钮从旧 JSON 导入</div></div></td></tr>';
        return;
      }
      tbody.innerHTML = accounts
        .map(
          (a) =>
            "<tr>" +
            '<td class="mono">' + escapeHtml(truncateId(a.id, 12)) + "</td>" +
            '<td><div class="td-title">' + escapeHtml(a.username) + "</div></td>" +
            '<td class="mono">' + escapeHtml(a.email) + "</td>" +
            "<td>" + statusBadge(a.status) + "</td>" +
            '<td style="font-variant-numeric: tabular-nums">' + (a.total_uses || 0) + "</td>" +
            '<td style="font-variant-numeric: tabular-nums">' + ((a.success_rate || 0) * 100).toFixed(0) + "%</td>" +
            "<td>" +
            '<button class="btn btn-danger btn-sm" onclick="deleteAccount(\'' + escapeHtml(a.id) + "')\">删除</button>" +
            "</td>" +
            "</tr>"
        )
        .join("");
    } catch (e) {
      document.getElementById("accounts-table").innerHTML =
        '<tr><td colspan="7" class="hint">加载失败：' + escapeHtml(e.message) + "</td></tr>";
    }
  }

  async function deleteAccount(id) {
    if (!window.confirm("确定要删除该账户吗？")) return;
    try {
      await api("/api/v2/pool/accounts/" + id, { method: "DELETE" });
      showToast("账户已删除");
      loadAccounts();
    } catch (e) {
      showToast("删除失败：" + e.message, "error");
    }
  }

  /* =============== Stats tab =============== */
  async function loadDashboardStats() {
    try {
      const d = await api("/api/v2/stats");
      setText("stat-total", d.total_registrations);
      setText("stat-rate", ((d.registration_success_rate || 0) * 100).toFixed(0) + "%");
      setText("stat-domains", d.total_domains_registered);
      setText("stat-avgtime", (d.avg_registration_duration || 0).toFixed(1) + "s");

      /* Recent registrations table */
      try {
        const recent = await api("/api/v2/stats/recent?limit=20");
        const tbody = document.getElementById("recent-registrations-table");
        if (!recent || !recent.length) {
          tbody.innerHTML = '<tr><td colspan="5" class="hint">暂无注册记录</td></tr>';
        } else {
          tbody.innerHTML = recent
            .map(
              (r) =>
                "<tr>" +
                '<td class="mono">' + escapeHtml(formatTime(r.timestamp)) + "</td>" +
                '<td class="mono">' + escapeHtml(r.event_type || "-") + "</td>" +
                "<td>" + (r.success ? '<span class="badge badge-ok">成功</span>' : '<span class="badge badge-err">失败</span>') + "</td>" +
                '<td style="font-variant-numeric: tabular-nums">' + (r.duration_seconds ? r.duration_seconds.toFixed(1) + "s" : "-") + "</td>" +
                '<td style="color:var(--err);font-size:11px">' + escapeHtml((r.error || "").slice(0, 80) || "-") + "</td>" +
                "</tr>"
            )
            .join("");
        }
      } catch (e) {
        document.getElementById("recent-registrations-table").innerHTML =
          '<tr><td colspan="5" class="hint">活动加载失败</td></tr>';
      }
    } catch (e) {
      showToast("统计加载失败：" + e.message, "error");
    }
  }

  /* =============== Logs tab =============== */
  async function loadLogs() {
    const level = document.getElementById("log-level").value;
    const url = "/api/v2/logs?limit=50" + (level ? "&level=" + encodeURIComponent(level) : "");
    try {
      const logs = await api(url);
      const tbody = document.getElementById("logs-table");
      if (!logs || !logs.length) {
        tbody.innerHTML = '<tr><td colspan="4" class="hint">暂无日志</td></tr>';
        return;
      }
      tbody.innerHTML = logs
        .map((l) => {
          const levelCls = l.level === "ERROR" ? "err" : l.level === "WARNING" ? "warn" : "gray";
          return (
            "<tr>" +
            '<td class="mono">' + escapeHtml((l.timestamp || "").slice(11, 19)) + "</td>" +
            '<td><span class="badge badge-' + levelCls + '">' + escapeHtml(l.level) + "</span></td>" +
            '<td style="max-width: 480px; word-break: break-all">' + escapeHtml((l.message || "").slice(0, 120)) + "</td>" +
            '<td class="mono" style="font-size: 10px; max-width: 240px; overflow-wrap: anywhere">' +
            escapeHtml(JSON.stringify(l.context || {}).slice(0, 50)) +
            "</td>" +
            "</tr>"
          );
        })
        .join("");
    } catch (e) {
      document.getElementById("logs-table").innerHTML =
        '<tr><td colspan="4" class="hint">加载失败：' + escapeHtml(e.message) + "</td></tr>";
    }
  }

  /* =============== Health tab =============== */
  async function loadHealth() {
    try {
      const d = await api("/api/v2/pool/health");
      const tbody = document.getElementById("health-table");
      const entries = Object.entries(d || {});
      if (!entries.length) {
        tbody.innerHTML = '<tr><td colspan="4" class="hint">暂无健康数据</td></tr>';
        return;
      }
      tbody.innerHTML = entries
        .map(([id, h]) => {
          const healthy = !!h.healthy;
          const issues = (h.issues || []).join(", ") || "无";
          const rate = ((h.success_rate || 0) * 100).toFixed(0) + "%";
          return (
            "<tr>" +
            '<td class="mono">' + escapeHtml(truncateId(id, 12)) + "</td>" +
            "<td>" + (healthy ? '<span class="badge badge-ok">健康</span>' : '<span class="badge badge-err">异常</span>') + "</td>" +
            '<td style="max-width: 380px">' + escapeHtml(issues) + "</td>" +
            '<td style="font-variant-numeric: tabular-nums">' + rate + "</td>" +
            "</tr>"
          );
        })
        .join("");
    } catch (e) {
      document.getElementById("health-table").innerHTML =
        '<tr><td colspan="4" class="hint">加载失败：' + escapeHtml(e.message) + "</td></tr>";
    }
  }

  /* =============== Migration =============== */
  async function migrate() {
    if (!window.confirm("确定要从旧系统迁移数据吗？这会扫描现有 JSON 并写入池数据库。")) return;
    try {
      const d = await api("/api/v2/migrate", { method: "POST" });
      showToast("迁移完成：" + (d.migrated || 0) + " 个账户");
      loadStats();
      loadAccounts();
    } catch (e) {
      showToast("迁移失败：" + e.message, "error");
    }
  }

  /* =============== Expose globals =============== */
  Object.assign(window, {
    showTab,
    loadStats,
    loadAccounts,
    deleteAccount,
    loadDashboardStats,
    loadLogs,
    loadHealth,
    migrate,
  });

  /* Boot */
  loadStats();
  loadAccounts();
  setInterval(loadStats, 30000);
})();
