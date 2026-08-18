/* ============================================================
   console.js — main console page JS (dashboard / batch / accounts / domains / history)
   Depends on `DP` from api.js.
   ============================================================ */

(function () {
  "use strict";

  const { api, escapeHtml, statusBadge, formatTime, formatDuration, showToast, setConnected } = window.DP;

  /* Track if user manually closed the detail modal (stops auto-refresh) */
  let userClosedModal = false;

  /* Step labels for the 6-step registration pipeline */
  const STEP_LABELS = {
    turnstile_token_acquisition: "Turnstile 验证",
    email_creation: "创建临时邮箱",
    browser_navigation: "打开注册页",
    form_submission: "提交注册表单",
    verification_email_retrieval: "获取验证邮件",
    verification_completion: "完成邮箱验证",
  };

  /* =============== Tab switching =============== */
  function showTab(tabName, btn) {
    document.querySelectorAll(".tab-content").forEach((el) => el.classList.remove("active"));
    document.querySelectorAll(".tab").forEach((el) => el.classList.remove("active"));
    const target = document.getElementById("tab-" + tabName);
    if (target) target.classList.add("active");
    if (btn) btn.classList.add("active");
    refresh();
    if (tabName === "accounts") loadAccounts();
    if (tabName === "batch") loadAccounts();
    if (tabName === "domains") loadDomains();
  }

  /* =============== Overview polling =============== */
  async function refresh() {
    try {
      const data = await api("/api/overview");
      const stats = (data.account_overview && data.account_overview.accounts) || {};
      setText("stat-total", stats.total || 0);
      setText("stat-active", stats.active || 0);
      setText("stat-registering", stats.registering || 0);
      setText("stat-pending", stats.pending || 0);
      setText("stat-failed", stats.failed || 0);
      setText("stat-batch", (data.account_overview && data.account_overview.active_batch_jobs) || 0);
      setConnected(document.getElementById("updated"), true);
      renderRecentAccounts();
      renderRecentBatches(data.batch_jobs || []);
      renderBatches(data.batch_jobs || []);
      renderJobs(data.jobs || []);

      const accountsTab = document.getElementById("tab-accounts");
      if (accountsTab && accountsTab.classList.contains("active")) {
        loadAccounts();
      }
    } catch (error) {
      setConnected(document.getElementById("updated"), false);
    }
  }

  function setText(id, val) {
    const el = document.getElementById(id);
    if (el) el.textContent = val;
  }

  function progressPercent(item) {
    return Math.min(
      100,
      Math.round(((item.completed_accounts || 0) / Math.max(item.total_accounts || 0, 1)) * 100)
    );
  }

  /* =============== Dashboard renders =============== */
  async function renderRecentAccounts() {
    try {
      const result = await api("/api/accounts?limit=5");
      const tbody = document.querySelector("#recent-accounts-table tbody");
      if (!result.accounts || !result.accounts.length) {
        tbody.innerHTML = '<tr><td colspan="3" class="hint">暂无账号</td></tr>';
        return;
      }
      tbody.innerHTML = result.accounts
        .map(
          (a) =>
            "<tr>" +
            '<td><div class="td-title">' + escapeHtml(a.username) + "</div></td>" +
            '<td class="mono">' + escapeHtml(a.email || "-") + "</td>" +
            "<td>" + statusBadge(a.status) + "</td>" +
            "</tr>"
        )
        .join("");
    } catch (e) {
      document.querySelector("#recent-accounts-table tbody").innerHTML =
        '<tr><td colspan="3" class="hint">加载失败</td></tr>';
    }
  }

  function renderRecentBatches(batches) {
    const tbody = document.querySelector("#recent-batches-table tbody");
    if (!batches || !batches.length) {
      tbody.innerHTML = '<tr><td colspan="4" class="hint">暂无批量任务</td></tr>';
      return;
    }
    tbody.innerHTML = batches
      .slice(0, 5)
      .map((b) => {
        const pct = progressPercent(b);
        return (
          "<tr>" +
          '<td><button class="btn-link mono" onclick="viewBatch(\'' + escapeHtml(b.id) + "')\">" +
          escapeHtml(b.id) +
          "</button></td>" +
          "<td>" + b.total_accounts + "</td>" +
          '<td><span style="color:var(--ok)">' + b.successful_accounts + "</span>" +
          (b.failed_accounts ? ' / <span style="color:var(--err)">' + b.failed_accounts + " 失败</span>" : "") +
          "</td>" +
          '<td><div class="task-progress"><div style="display:flex;justify-content:space-between;font-size:11px;color:var(--ink-3);margin-bottom:6px"><span>' +
          statusBadge(b.status) +
          "</span><span>" +
          pct +
          '%</span></div><div class="progress"><div class="progress-fill" style="width:' +
          pct +
          '%"></div></div></div></td>' +
          "</tr>"
        );
      })
      .join("");
  }

  function renderBatches(batches) {
    const tbody = document.querySelector("#batch-history-table tbody");
    if (!batches || !batches.length) {
      tbody.innerHTML = '<tr><td colspan="8" class="hint">暂无批量任务</td></tr>';
      const s = document.getElementById("active-batch-section");
      if (s) s.style.display = "none";
      return;
    }
    tbody.innerHTML = batches
      .map((b) => {
        const pct = progressPercent(b);
        return (
          "<tr>" +
          '<td><button class="btn-link mono" onclick="viewBatch(\'' + escapeHtml(b.id) + "')\">" +
          escapeHtml(b.id) +
          "</button></td>" +
          "<td>" + b.total_accounts + "</td>" +
          '<td><div class="task-progress"><div style="display:flex;justify-content:space-between;font-size:11px;color:var(--ink-3);margin-bottom:6px"><span>' +
          b.completed_accounts + " / " + b.total_accounts +
          "</span><span>" + pct + '%</span></div><div class="progress"><div class="progress-fill" style="width:' + pct + '%"></div></div></div></td>' +
          '<td style="color:var(--ok); font-variant-numeric: tabular-nums">' + b.successful_accounts + "</td>" +
          '<td style="color:var(--err); font-variant-numeric: tabular-nums">' + b.failed_accounts + "</td>" +
          "<td>" + statusBadge(b.status) + "</td>" +
          '<td class="mono">' + formatTime(b.created_at) + "</td>" +
          '<td><button class="btn btn-ghost btn-sm" onclick="viewBatch(\'' + escapeHtml(b.id) + "')\">详情</button></td>" +
          "</tr>"
        );
      })
      .join("");

    const activeBatch = batches.find((b) => b.status === "running");
    const activeSection = document.getElementById("active-batch-section");
    if (activeBatch) {
      activeSection.style.display = "block";
      const pct = Math.round((activeBatch.completed_accounts / activeBatch.total_accounts) * 100) || 0;
      const delayMin = activeBatch.delay_min_seconds !== undefined && activeBatch.delay_min_seconds !== null
        ? activeBatch.delay_min_seconds
        : (activeBatch.delay_between_registrations !== undefined ? activeBatch.delay_between_registrations : 15);
      const delayMax = activeBatch.delay_max_seconds !== undefined && activeBatch.delay_max_seconds !== null
        ? activeBatch.delay_max_seconds
        : delayMin;
      const delayText = delayMin === delayMax ? delayMin + "s" : delayMin + "s ~ " + delayMax + "s";
      const prefixText = activeBatch.username_prefix || "自动生成";
      const referralText = activeBatch.referral_code || "默认";
      const concurrentText = activeBatch.max_concurrent || 1;

      document.getElementById("active-batch-info").innerHTML =
        '<div style="display:flex;justify-content:space-between;align-items:center;gap:16px;flex-wrap:wrap;margin-bottom:16px">' +
        '<div><div class="hint" style="margin-bottom:4px">任务 ID</div><code>' + escapeHtml(activeBatch.id) + "</code></div>" +
        '<button class="btn btn-secondary" onclick="viewBatch(\'' + escapeHtml(activeBatch.id) + "')\">查看每个账号进度</button>" +
        "</div>" +
        '<div style="margin-bottom:8px"><strong>整体进度：</strong>' + activeBatch.completed_accounts + " / " + activeBatch.total_accounts + "</div>" +
        '<div class="progress progress-lg"><div class="progress-fill" style="width:' + pct + '%"></div></div>' +
        '<div class="hint" style="margin-top:8px">' + pct + "% · 成功 " + activeBatch.successful_accounts + " · 失败 " + activeBatch.failed_accounts + "</div>" +
        '<div class="active-batch-meta">' +
        '<span><strong>邀请码:</strong> <code>' + escapeHtml(referralText) + '</code></span>' +
        '<span><strong>前缀:</strong> <code>' + escapeHtml(prefixText) + '</code></span>' +
        '<span><strong>间隔:</strong> ' + escapeHtml(delayText) + '</span>' +
        '<span><strong>并发:</strong> ' + escapeHtml(concurrentText) + '</span>' +
        '</div>';
    } else {
      activeSection.style.display = "none";
    }
  }

  function renderJobs(jobs) {
    const tbody = document.querySelector("#history-table tbody");
    if (!jobs || !jobs.length) {
      tbody.innerHTML = '<tr><td colspan="6" class="hint">暂无记录</td></tr>';
      return;
    }
    tbody.innerHTML = jobs
      .map(
        (j) =>
          "<tr>" +
          '<td class="mono">' + escapeHtml(j.id) + "</td>" +
          '<td class="mono">' + escapeHtml(j.account_id || "-") + "</td>" +
          "<td>" + statusBadge(j.status) + "</td>" +
          "<td>" + (j.result ? escapeHtml(j.result.username || "-") : "-") + "</td>" +
          '<td class="mono">' + (j.result && j.result.duration ? j.result.duration.toFixed(1) + "s" : "-") + "</td>" +
          '<td class="mono">' + escapeHtml(j.created_at) + "</td>" +
          "</tr>"
      )
      .join("");
  }

  /* =============== Step timeline =============== */
  function stepTimeline(progress) {
    const current = progress && progress.current_step;
    return (
      '<div class="step-timeline">' +
      ((progress && progress.steps) || [])
        .map((step) => {
          const isCurrent = step.name === current && progress.status !== "failed";
          const state = isCurrent ? "current" : step.status;
          const icon = step.status === "success" ? "✓" : step.status === "failed" ? "!" : "";
          const duration = Number.isFinite(step.duration) ? step.duration.toFixed(1) + " 秒" : "";
          const stateText = isCurrent
            ? "正在执行"
            : step.status === "success"
            ? "已完成"
            : step.status === "failed"
            ? "失败"
            : step.status === "skipped"
            ? "未执行"
            : "等待执行";
          return (
            '<div class="timeline-step ' + escapeHtml(state) + '">' +
            '<span class="step-dot">' + icon + "</span>" +
            '<div class="step-name" title="' + escapeHtml(step.label || step.name) + '">' +
            escapeHtml(step.label || STEP_LABELS[step.name] || step.name) +
            "</div>" +
            '<div class="step-meta">' + stateText + (duration ? " · " + duration : "") + "</div>" +
            "</div>"
          );
        })
        .join("") +
      "</div>"
    );
  }

  function accountProgressRow(account, open) {
    const progress = account.progress || { steps: [], completed_steps: 0, total_steps: 6 };
    const pct = Math.round(
      ((progress.completed_steps || 0) / Math.max(progress.total_steps || 6, 1)) * 100
    );
    return (
      '<details class="account-progress" data-account-id="' + escapeHtml(account.id) + '"' + (open ? " open" : "") + ">" +
      "<summary>" +
      '<div class="account-name"><strong>' + escapeHtml(account.username) + '</strong><div class="account-email">' +
      escapeHtml(account.email || account.id) +
      "</div></div>" +
      "<div>" + statusBadge(account.status) + "</div>" +
      '<div class="task-progress"><div style="display:flex;justify-content:space-between;font-size:11px;color:var(--ink-3);margin-bottom:6px"><span>' +
      (progress.completed_steps || 0) + " / " + (progress.total_steps || 6) + " 步</span><span>" + pct + '%</span></div>' +
      '<div class="progress"><div class="progress-fill" style="width:' + pct + '%"></div></div></div>' +
      '<div class="account-chevron">›</div>' +
      "</summary>" +
      stepTimeline(progress) +
      (progress.error
        ? '<div class="step-error"><strong>失败原因：</strong>' + escapeHtml(progress.error) + "</div>"
        : "") +
      (account.status === "active" && account.password
        ? '<div class="hint mono" style="padding:0 6px 18px">邮箱：' + escapeHtml(account.email || "-") +
          " · 密码：<code>" + escapeHtml(account.password) + "</code></div>"
        : "") +
      "</details>"
    );
  }

  /* =============== Batch Configuration Box =============== */
  function batchConfigHtml(data) {
    const delayMin = data.delay_min_seconds !== undefined && data.delay_min_seconds !== null
      ? data.delay_min_seconds
      : (data.delay_between_registrations !== undefined ? data.delay_between_registrations : 15);
    const delayMax = data.delay_max_seconds !== undefined && data.delay_max_seconds !== null
      ? data.delay_max_seconds
      : delayMin;
    const delayText = delayMin === delayMax ? delayMin + " 秒" : delayMin + " ~ " + delayMax + " 秒";
    const prefixText = data.username_prefix ? data.username_prefix : "留空（自动生成）";
    const concurrentText = data.max_concurrent === 1 ? "1（推荐串行）" : String(data.max_concurrent);
    const referralText = data.referral_code || "4qn8iw8r1o";
    const countText = (data.total_accounts || (data.account_ids ? data.account_ids.length : 0)) + " 个";

    const meta = data.metadata || {};
    let turnstileHtml = "";
    if (meta.turnstile_sitekey || meta.turnstile_endpoint) {
      turnstileHtml =
        '<div class="config-item"><div class="config-item-label">Turnstile Site Key</div><div class="config-item-value mono">' +
        escapeHtml(meta.turnstile_sitekey || "使用默认环境变量") +
        '</div></div>' +
        '<div class="config-item"><div class="config-item-label">Solver Endpoint</div><div class="config-item-value mono">' +
        escapeHtml(meta.turnstile_endpoint || "使用默认环境变量") +
        '</div></div>';
    }

    return (
      '<div class="batch-config-box">' +
      '<div class="batch-config-box-title"><i class="fa-solid fa-sliders" style="color:var(--accent)"></i> 任务详细配置</div>' +
      '<div class="batch-config-grid">' +
      '<div class="config-item"><div class="config-item-label">注册数量</div><div class="config-item-value">' + escapeHtml(countText) + '</div></div>' +
      '<div class="config-item"><div class="config-item-label">邀请码</div><div class="config-item-value mono">' + escapeHtml(referralText) + '</div></div>' +
      '<div class="config-item"><div class="config-item-label">用户名前缀</div><div class="config-item-value mono">' + escapeHtml(prefixText) + '</div></div>' +
      '<div class="config-item"><div class="config-item-label">最小 ~ 最大间隔</div><div class="config-item-value">' + escapeHtml(delayText) + '</div></div>' +
      '<div class="config-item"><div class="config-item-label">并发数</div><div class="config-item-value">' + escapeHtml(concurrentText) + '</div></div>' +
      '<div class="config-item"><div class="config-item-label">创建时间</div><div class="config-item-value mono">' + escapeHtml(formatTime(data.created_at) || "-") + '</div></div>' +
      turnstileHtml +
      '</div>' +
      '</div>'
    );
  }

  /* =============== Batch modal =============== */
  async function viewBatch(batchId) {
    const modal = document.getElementById("account-detail-modal");
    const wasOpen = modal.classList.contains("active");
    if (!wasOpen) userClosedModal = false;
    try {
      const data = await api("/api/batch/" + batchId);
      const openAccounts = new Set(
        Array.from(document.querySelectorAll(".account-progress[open]")).map((el) => el.dataset.accountId)
      );
      const accountsHtml = data.accounts && data.accounts.length
        ? data.accounts
            .map((account, index) =>
              accountProgressRow(account, openAccounts.has(account.id) || (!wasOpen && index === 0))
            )
            .join("")
        : '<div class="empty"><div class="empty-icon">—</div><div class="empty-title">无账号</div></div>';
      const pct = progressPercent(data);
      document.getElementById("detail-modal-title").textContent = "批量注册任务详情";
      document.getElementById("account-detail-content").innerHTML =
        '<div class="batch-summary">' +
        '<div><div class="summary-label">任务 ID</div><div class="summary-value task-id">' + escapeHtml(data.id) + "</div></div>" +
        '<div><div class="summary-label">整体进度</div><div class="summary-value">' + pct + "%</div></div>" +
        '<div><div class="summary-label">已完成</div><div class="summary-value">' + data.completed_accounts + " / " + data.total_accounts + "</div></div>" +
        '<div><div class="summary-label">成功</div><div class="summary-value ok">' + data.successful_accounts + "</div></div>" +
        '<div><div class="summary-label">失败</div><div class="summary-value err">' + data.failed_accounts + "</div></div>" +
        "</div>" +
        batchConfigHtml(data) +
        '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px"><strong>账号注册明细</strong>' + statusBadge(data.status) + "</div>" +
        (data.error ? '<div class="step-error">' + escapeHtml(data.error) + "</div>" : "") +
        "<div>" + accountsHtml + "</div>";
      modal.classList.add("active");
      if (!userClosedModal && (data.status === "running" || data.status === "pending")) {
        setTimeout(() => {
          if (!userClosedModal) viewBatch(batchId);
        }, 3000);
      }
    } catch (error) {
      showToast("加载失败：" + error.message, "error");
    }
  }

  /* =============== Accounts tab =============== */
  async function loadAccounts() {
    const search = document.getElementById("account-search").value;
    const status = document.getElementById("account-filter").value;
    let url = "/api/accounts?";
    if (status) url += "status=" + status + "&";
    if (search) url = "/api/accounts/search?q=" + encodeURIComponent(search);

    try {
      const data = await api(url);
      const tbody = document.querySelector("#accounts-table tbody");
      if (!data.accounts || !data.accounts.length) {
        tbody.innerHTML = '<tr><td colspan="7"><div class="empty"><div class="empty-icon">◌</div><div class="empty-title">暂无账号</div><div class="empty-hint">点击右上角「+ 添加账号」开始录入</div></div></td></tr>';
        return;
      }
      tbody.innerHTML = data.accounts
        .map(
          (a) =>
            "<tr>" +
            '<td><input type="checkbox" class="account-checkbox" value="' + escapeHtml(a.id) + '"></td>' +
            '<td class="mono">' + escapeHtml(String(a.id).slice(0, 8)) + "…</td>" +
            '<td><div class="td-title">' + escapeHtml(a.username) + "</div></td>" +
            '<td class="mono">' + escapeHtml(a.email || "-") + "</td>" +
            "<td>" + statusBadge(a.status) + "</td>" +
            '<td class="mono">' + escapeHtml(a.registered_at || "-") + "</td>" +
            "<td>" +
            '<div style="display:flex;gap:6px">' +
            '<button class="btn btn-ghost btn-sm" onclick="viewAccount(\'' + escapeHtml(a.id) + "')\">详情</button>" +
            (a.status !== "active" ? '<button class="btn btn-secondary btn-sm" onclick="registerAccount(\'' + escapeHtml(a.id) + "')\">注册</button>" : "") +
            '<button class="btn btn-danger btn-sm" onclick="deleteAccount(\'' + escapeHtml(a.id) + "')\">删除</button>" +
            "</div>" +
            "</td>" +
            "</tr>"
        )
        .join("");
    } catch (e) {
      document.querySelector("#accounts-table tbody").innerHTML =
        '<tr><td colspan="7" class="hint">加载失败：' + escapeHtml(e.message) + "</td></tr>";
    }
  }

  function toggleSelectAll() {
    const checked = document.getElementById("select-all").checked;
    document.querySelectorAll(".account-checkbox").forEach((cb) => (cb.checked = checked));
  }

  async function viewAccount(accountId) {
    userClosedModal = false;
    try {
      const data = await api("/api/accounts/" + accountId);
      document.getElementById("detail-modal-title").textContent = "账号详情";
      let html =
        '<div class="batch-summary" style="grid-template-columns: repeat(3, 1fr)">' +
        '<div><div class="summary-label">用户名</div><div class="summary-value">' + escapeHtml(data.username) + "</div></div>" +
        '<div><div class="summary-label">账号状态</div><div class="summary-value">' + statusBadge(data.status) + "</div></div>" +
        '<div><div class="summary-label">注册时间</div><div class="summary-value" style="font-size:14px">' + escapeHtml(formatTime(data.registered_at || data.created_at)) + "</div></div>" +
        "</div>";

      html += '<div style="display:grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 8px 24px;">';
      const fieldOrder = ["id", "username", "email", "password", "status", "referral_code", "fullname", "phone", "address_line1", "city", "state", "postal_code", "country", "registered_at", "created_at", "error"];
      for (const key of fieldOrder) {
        if (data[key] !== undefined && data[key] !== null) {
          const isPassword = key === "password" && data.status === "active";
          const value = escapeHtml(Array.isArray(data[key]) ? JSON.stringify(data[key]) : (data[key] || "-"));
          html += "<div class=\"kv\"" + (isPassword ? ' style="background: var(--accent-soft); padding: 8px 10px; border-radius: 6px;"' : "") + "><div class=\"kv-label\">" + escapeHtml(key) + "</div><div class=\"kv-value" + (isPassword ? " mono" : "") + "\">" + value + "</div></div>";
        }
      }
      html += "</div>";

      try {
        const progress = await api("/api/accounts/" + accountId + "/progress");
        html += '<div style="margin-top:20px;padding-top:18px;border-top:1px solid var(--line-1)"><strong style="color:var(--ink-1)">注册流程</strong>';
        html += stepTimeline(progress);
        if (progress.error) html += '<div class="step-error"><strong>失败原因：</strong>' + escapeHtml(progress.error) + "</div>";
        html += "</div>";
      } catch (e) {
        /* ignore progress errors */
      }

      document.getElementById("account-detail-content").innerHTML = html;
      document.getElementById("account-detail-modal").classList.add("active");

      if (!userClosedModal && (data.status === "registering" || data.status === "pending")) {
        setTimeout(() => {
          if (!userClosedModal) viewAccount(accountId);
        }, 2000);
      }
    } catch (e) {
      showToast("加载失败：" + e.message, "error");
    }
  }

  async function registerAccount(accountId) {
    if (!window.confirm("确认注册此账号？")) return;
    try {
      await api("/api/accounts/" + accountId + "/register", { method: "POST" });
      showToast("注册已开始");
      loadAccounts();
    } catch (e) {
      showToast("错误：" + e.message, "error");
    }
  }

  async function deleteAccount(accountId) {
    if (!window.confirm("确认删除此账号？")) return;
    try {
      await api("/api/accounts/" + accountId, { method: "DELETE" });
      showToast("账号已删除");
      loadAccounts();
    } catch (e) {
      showToast("删除失败：" + e.message, "error");
    }
  }

  async function bulkDelete() {
    const checked = Array.from(document.querySelectorAll(".account-checkbox:checked")).map((cb) => cb.value);
    if (!checked.length) {
      showToast("请选择要删除的账号", "warn");
      return;
    }
    if (!window.confirm("确认删除选中的 " + checked.length + " 个账号？")) return;
    try {
      const data = await api("/api/accounts/bulk-delete", {
        method: "POST",
        body: { account_ids: checked },
      });
      showToast("已删除 " + data.deleted + " 个账号");
      loadAccounts();
    } catch (e) {
      showToast("批量删除失败：" + e.message, "error");
    }
  }

  function showAddAccountModal() {
    document.getElementById("add-account-modal").classList.add("active");
  }

  async function createAccount() {
    const body = {
      username: document.getElementById("new-username").value,
      email: document.getElementById("new-email").value,
      password: document.getElementById("new-password").value || undefined,
      referral_code: document.getElementById("new-referral").value || undefined,
    };
    if (!body.username || !body.email) {
      showToast("用户名和邮箱必填", "warn");
      return;
    }
    try {
      await api("/api/accounts", { method: "POST", body });
      closeModal("add-account-modal");
      showToast("账号已创建");
      loadAccounts();
    } catch (e) {
      showToast("创建失败：" + e.message, "error");
    }
  }

  async function exportAccounts() {
    const checked = Array.from(document.querySelectorAll(".account-checkbox:checked")).map((cb) => cb.value);
    const body = checked.length ? { account_ids: checked } : {};
    try {
      const response = await fetch("/api/accounts/export", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = "accounts_export.json";
      a.click();
      URL.revokeObjectURL(url);
      showToast("导出已开始下载");
    } catch (e) {
      showToast("导出失败：" + e.message, "error");
    }
  }

  function closeModal(id) {
    document.getElementById(id).classList.remove("active");
    if (id === "account-detail-modal") {
      userClosedModal = true;
    }
  }

  /* =============== Batch start =============== */
  async function startBatch() {
    const btn = document.getElementById("start-batch-btn");
    btn.disabled = true;
    btn.textContent = "创建中…";

    const body = {
      count: parseInt(document.getElementById("batch-count").value, 10),
      referral_code: document.getElementById("batch-referral").value || undefined,
      username_prefix: document.getElementById("batch-prefix").value || undefined,
      delay: parseFloat(document.getElementById("batch-delay").value),
      delay_max: parseFloat(document.getElementById("batch-delay-max").value),
      max_concurrent: parseInt(document.getElementById("batch-concurrent").value, 10),
      turnstile_sitekey: (document.getElementById("batch-turnstile-sitekey") || {}).value || undefined,
      turnstile_endpoint: (document.getElementById("batch-turnstile-endpoint") || {}).value || undefined,
    };

    try {
      const data = await api("/api/batch", { method: "POST", body });
      showToast("批量任务已创建：" + data.batch_job_id);
      refresh();
    } catch (error) {
      showToast("错误：" + error.message, "error");
    } finally {
      btn.disabled = false;
      btn.textContent = "开始批量注册";
    }
  }

  /* =============== Domains tab =============== */
  async function loadDomainAccounts() {
    try {
      const data = await api("/api/accounts?status=active&limit=100");
      const select = document.getElementById("domain-account");
      select.innerHTML = '<option value="">— 选择已注册的账号 —</option>';
      if (data.accounts) {
        for (const account of data.accounts) {
          const option = document.createElement("option");
          option.value = account.username;
          option.textContent = account.username + " (" + (account.email || "无邮箱") + ")";
          option.dataset.password = account.password || "";
          select.appendChild(option);
        }
      }
    } catch (e) {
      console.error("Failed to load accounts:", e);
    }
  }

  async function loadDomains() {
    await loadDomainAccounts();
    try {
      const data = await api("/api/domains");
      const tbody = document.querySelector("#domains-table tbody");
      if (!data.domains || !data.domains.length) {
        tbody.innerHTML = '<tr><td colspan="4" class="hint">暂无域名</td></tr>';
        return;
      }
      tbody.innerHTML = data.domains
        .map(
          (d) =>
            "<tr>" +
            '<td><strong style="color:var(--ink-1)">' + escapeHtml(d.domain) + "</strong></td>" +
            '<td class="mono">' + escapeHtml(d.username) + "</td>" +
            '<td class="mono">' + escapeHtml(d.registered_at || "-") + "</td>" +
            '<td class="mono" style="font-size:11px">' + (d.nameservers || []).map(escapeHtml).join("<br>") + "</td>" +
            "</tr>"
        )
        .join("");
    } catch (e) {
      document.querySelector("#domains-table tbody").innerHTML =
        '<tr><td colspan="4" class="hint">加载失败</td></tr>';
    }
  }

  async function checkDomain() {
    const accountSelect = document.getElementById("domain-account");
    const username = accountSelect.value;
    const password = accountSelect.options[accountSelect.selectedIndex]
      ? accountSelect.options[accountSelect.selectedIndex].dataset.password
      : "";
    const prefix = document.getElementById("domain-prefix").value.trim();
    const suffix = document.getElementById("domain-suffix").value;
    const resultDiv = document.getElementById("domain-result");

    if (!username || !prefix) {
      showToast("请选择账号并输入域名前缀", "warn");
      return;
    }

    document.getElementById("check-domain-btn").disabled = true;
    resultDiv.innerHTML = '<p class="hint">检查中…</p>';

    try {
      const data = await api("/api/domains/check", {
        method: "POST",
        body: { username, password, domain_prefix: prefix, domain_suffix: suffix },
      });
      if (data.available) {
        resultDiv.innerHTML = '<p style="color:var(--ok)">✓ ' + escapeHtml(data.domain) + " 可注册！</p>";
      } else {
        resultDiv.innerHTML = '<p style="color:var(--warn)">✗ ' + escapeHtml(data.domain) + " 不可用 — " + escapeHtml(data.message) + "</p>";
      }
    } catch (e) {
      resultDiv.innerHTML = '<p style="color:var(--err)">错误：' + escapeHtml(e.message) + "</p>";
    } finally {
      document.getElementById("check-domain-btn").disabled = false;
    }
  }

  async function registerDomain() {
    const accountSelect = document.getElementById("domain-account");
    const username = accountSelect.value;
    const password = accountSelect.options[accountSelect.selectedIndex]
      ? accountSelect.options[accountSelect.selectedIndex].dataset.password
      : "";
    const prefix = document.getElementById("domain-prefix").value.trim();
    const suffix = document.getElementById("domain-suffix").value;
    const ns1 = document.getElementById("domain-ns1").value.trim() || "ns1.cloudflare.com";
    const ns2 = document.getElementById("domain-ns2").value.trim() || "ns2.cloudflare.com";
    const resultDiv = document.getElementById("domain-result");

    if (!username || !prefix) {
      showToast("请选择账号并输入域名前缀", "warn");
      return;
    }
    if (!window.confirm("确认注册 " + prefix + "." + suffix + " ？")) return;

    document.getElementById("register-domain-btn").disabled = true;
    resultDiv.innerHTML = '<p class="hint">注册中…可能需要 30-60 秒</p>';

    try {
      const data = await api("/api/domains/register", {
        method: "POST",
        body: {
          username, password,
          domain_prefix: prefix,
          domain_suffix: suffix,
          nameservers: [ns1, ns2],
        },
      });
      if (data.success) {
        resultDiv.innerHTML = '<p style="color:var(--ok)">✓ 注册成功！' + escapeHtml(data.domain) + "</p>";
        showToast("域名注册成功：" + data.domain);
        loadDomains();
      } else {
        resultDiv.innerHTML = '<p style="color:var(--err)">注册失败：' + escapeHtml(data.error || data.message) + "</p>";
        showToast("注册失败：" + (data.error || data.message), "error");
      }
    } catch (e) {
      resultDiv.innerHTML = '<p style="color:var(--err)">错误：' + escapeHtml(e.message) + "</p>";
      showToast("错误：" + e.message, "error");
    } finally {
      document.getElementById("register-domain-btn").disabled = false;
    }
  }

  /* =============== Search debounce =============== */
  function bindSearch() {
    let searchTimeout;
    document.getElementById("account-search").addEventListener("input", () => {
      clearTimeout(searchTimeout);
      searchTimeout = setTimeout(loadAccounts, 300);
    });
  }

  /* Modal close on backdrop click */
  function bindModalBackdrop() {
    document.querySelectorAll(".modal-overlay").forEach((overlay) => {
      overlay.addEventListener("click", (e) => {
        if (e.target === overlay) closeModal(overlay.id);
      });
    });
  }

  /* =============== Expose globals used by inline handlers =============== */
  Object.assign(window, {
    showTab,
    refresh,
    startBatch,
    viewBatch,
    loadAccounts,
    toggleSelectAll,
    viewAccount,
    registerAccount,
    deleteAccount,
    bulkDelete,
    showAddAccountModal,
    createAccount,
    exportAccounts,
    closeModal,
    loadDomains,
    loadDomainAccounts,
    checkDomain,
    registerDomain,
  });

  /* Boot */
  bindSearch();
  bindModalBackdrop();
  refresh();
  loadAccounts();
  setInterval(refresh, 3000);
})();
