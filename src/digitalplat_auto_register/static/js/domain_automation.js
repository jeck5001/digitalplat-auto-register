(function () {
  "use strict";

  let overview = {
    tokens: [],
    subscriptions: [],
    jobs: [],
    domains: [],
    cloudflare: null,
    renewal: {},
    stats: {},
  };

  let activeTab = "domains";

  /* =============== Utilities =============== */
  function escapeHtml(str) {
    if (str == null) return "";
    return String(str)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#039;");
  }

  function formatTime(isoStr) {
    if (!isoStr) return "-";
    try {
      const d = new Date(isoStr);
      if (isNaN(d.getTime())) return isoStr;
      return (
        d.getFullYear() +
        "-" +
        String(d.getMonth() + 1).padStart(2, "0") +
        "-" +
        String(d.getDate()).padStart(2, "0") +
        " " +
        String(d.getHours()).padStart(2, "0") +
        ":" +
        String(d.getMinutes()).padStart(2, "0") +
        ":" +
        String(d.getSeconds()).padStart(2, "0")
      );
    } catch {
      return isoStr;
    }
  }

  function showToast(message, type) {
    type = type || "ok";
    let container = document.querySelector(".toast-container");
    if (!container) {
      container = document.createElement("div");
      container.className = "toast-container";
      document.body.appendChild(container);
    }
    const toast = document.createElement("div");
    toast.className = "toast " + type;
    toast.textContent = message;
    container.appendChild(toast);
    setTimeout(() => {
      toast.style.opacity = "0";
      toast.style.transform = "translateY(-6px)";
      setTimeout(() => toast.remove(), 250);
    }, 3200);
  }

  function copyToClipboard(text, label) {
    if (!navigator.clipboard) {
      const ta = document.createElement("textarea");
      ta.value = text;
      document.body.appendChild(ta);
      ta.select();
      document.execCommand("copy");
      ta.remove();
    } else {
      navigator.clipboard.writeText(text);
    }
    showToast("已复制 " + (label || text) + " 到剪贴板", "ok");
  }

  async function api(path, options) {
    options = options || {};
    const headers = Object.assign({ "Content-Type": "application/json" }, options.headers || {});
    const res = await fetch(path, {
      method: options.method || "GET",
      headers,
      body: options.body ? JSON.stringify(options.body) : undefined,
    });
    if (!res.ok) {
      let detail = res.statusText;
      try {
        const body = await res.json();
        detail = body.detail || body.error || detail;
      } catch {}
      throw new Error(detail);
    }
    return res.json();
  }

  function statusBadge(status) {
    const s = String(status || "unknown").toLowerCase();
    const map = {
      ok: ["badge-ok", "正常"],
      completed: ["badge-ok", "已完成"],
      success: ["badge-ok", "成功"],
      active: ["badge-ok", "已激活"],
      valid: ["badge-ok", "有效"],
      renewed: ["badge-ok", "已续期"],
      running: ["badge-running", "进行中"],
      registering: ["badge-registering", "注册中"],
      pending: ["badge-warn", "待生效"],
      untested: ["badge-gray", "未测试"],
      unmanaged: ["badge-gray", "未托管"],
      failed: ["badge-err", "失败"],
      invalid: ["badge-err", "无效"],
      error: ["badge-err", "异常"],
      paused: ["badge-warn", "已暂停"],
      skipped: ["badge-gray", "已跳过"],
    };
    const info = map[s] || ["badge-gray", s];
    return '<span class="badge ' + info[0] + '">' + escapeHtml(info[1]) + "</span>";
  }

  /* =============== Tab switching =============== */
  function showDomainTab(tabName, tabBtn) {
    activeTab = tabName;
    document.querySelectorAll(".tabs .tab").forEach((btn) => btn.classList.remove("active"));
    if (tabBtn) {
      tabBtn.classList.add("active");
    } else {
      const target = document.querySelector('.tabs .tab[data-tab="' + tabName + '"]');
      if (target) target.classList.add("active");
    }
    document.querySelectorAll(".tab-content").forEach((el) => el.classList.remove("active"));
    const content = document.getElementById("tab-" + tabName);
    if (content) content.classList.add("active");
  }

  /* =============== State refresh =============== */
  async function refresh() {
    try {
      overview = await api("/api/domain-automation");
      const conn = document.getElementById("connection");
      if (conn) {
        conn.className = "conn connected";
        conn.innerHTML = '<span class="conn-dot"></span><span>已连接</span>';
      }
      renderAll();
    } catch (e) {
      console.error("refresh failed", e);
      const conn = document.getElementById("connection");
      if (conn) {
        conn.className = "conn";
        conn.innerHTML = '<span class="conn-dot"></span><span>连接失败</span>';
      }
    }
  }

  function renderAll() {
    try { renderStats(); } catch (e) { console.error("renderStats error", e); }
    try { renderTokens(); } catch (e) { console.error("renderTokens error", e); }
    try { renderSubscriptions(); } catch (e) { console.error("renderSubscriptions error", e); }
    try { renderJobs(); } catch (e) { console.error("renderJobs error", e); }
    try { renderDomains(); } catch (e) { console.error("renderDomains error", e); }
    try { renderCloudflare(); } catch (e) { console.error("renderCloudflare error", e); }
    try { renderRenewal(); } catch (e) { console.error("renderRenewal error", e); }
    try { updateTokenFilterOptions(); } catch (e) { console.error("updateTokenFilterOptions error", e); }
  }

  function renderStats() {
    const stats = overview.stats || {};
    const domains = overview.domains || [];

    const totalDomains = domains.length;
    const cfActive = domains.filter((d) => d.cloudflare_status === "active").length;
    const cfPending = domains.filter((d) => d.cloudflare_status === "pending").length;
    const unmanaged = domains.filter((d) => !d.cloudflare_status || d.cloudflare_status === "unmanaged").length;
    const expiring = domains.filter((d) => d.renewal_days_remaining != null && d.renewal_days_remaining <= 30).length;

    const elDomains = document.getElementById("stat-domains");
    const elCf = document.getElementById("stat-cloudflare");
    const elPending = document.getElementById("stat-cf-pending");
    const elUnmanaged = document.getElementById("stat-unmanaged");
    const elExpiring = document.getElementById("stat-expiring");

    if (elDomains) elDomains.textContent = totalDomains;
    if (elCf) elCf.textContent = cfActive;
    if (elPending) elPending.textContent = cfPending;
    if (elUnmanaged) elUnmanaged.textContent = unmanaged;
    if (elExpiring) elExpiring.textContent = expiring;
  }

  function updateTokenFilterOptions() {
    const select = document.getElementById("domain-filter-token");
    if (!select) return;
    const currentVal = select.value;
    const tokens = overview.tokens || [];
    let html = '<option value="">全部 Token</option>';
    tokens.forEach((t) => {
      html += '<option value="' + escapeHtml(t.id) + '">' + escapeHtml(t.name) + '</option>';
    });
    select.innerHTML = html;
    select.value = currentVal;
  }

  /* =============== Tokens table =============== */
  function renderTokens() {
    const list = document.getElementById("token-list");
    if (!list) return;
    const tokens = overview.tokens || [];
    if (!tokens.length) {
      list.innerHTML =
        '<div class="empty"><div class="empty-icon">◈</div><div class="empty-title">尚未配置 API Token</div><div class="empty-hint">请在上方添加你的第一个 DigitalPlat API Token</div></div>';
      return;
    }
    list.innerHTML = tokens
      .map((t) => {
        return (
          '<div class="list-item" style="padding: var(--s-3) 0;">' +
          '<div class="list-item-main">' +
          '<div class="list-item-title" style="display:flex; align-items:center; gap:var(--s-2);">' +
          '<strong>' + escapeHtml(t.name) + '</strong>' +
          statusBadge(t.status || "untested") +
          '</div>' +
          '<div class="list-item-sub mono" style="margin-top:4px; font-size:11px;">' +
          escapeHtml(t.masked_token) + ' · ' + escapeHtml(t.environment || "live") +
          ' · 持有 ' + (t.registered_domains_count || 0) + ' 个域名' +
          '</div>' +
          (t.last_error ? '<div style="color:var(--err);font-size:11px;margin-top:3px">' + escapeHtml(t.last_error) + "</div>" : "") +
          "</div>" +
          '<div class="list-item-actions">' +
          '<button class="btn btn-secondary btn-sm" onclick="testToken(\'' + escapeHtml(t.id) + "')\">测试</button>" +
          '<button class="btn btn-danger btn-sm" onclick="deleteToken(\'' + escapeHtml(t.id) + "')\">删除</button>" +
          "</div>" +
          "</div>"
        );
      })
      .join("");

    const select = document.getElementById("job-token-mode");
    if (select) {
      const current = select.value;
      let opts = '<option value="all">使用全部启用 Token (' + (overview.stats.enabled_tokens || 0) + " 个)</option>";
      tokens
        .filter((t) => t.enabled)
        .forEach((t) => {
          opts += '<option value="' + escapeHtml(t.id) + '">仅使用：' + escapeHtml(t.name) + "</option>";
        });
      select.innerHTML = opts;
      select.value = current || "all";
    }
  }

  /* =============== Subscriptions =============== */
  function renderSubscriptions() {
    const list = document.getElementById("subscription-list");
    const select = document.getElementById("job-subscription");
    const subs = overview.subscriptions || [];

    if (select) {
      const current = select.value;
      if (!subs.length) {
        select.innerHTML = '<option value="">请先创建前缀订阅</option>';
      } else {
        select.innerHTML = subs
          .map((s) => '<option value="' + escapeHtml(s.id) + '">' + escapeHtml(s.name) + " (" + escapeHtml(s.prefix) + "*." + escapeHtml(s.suffix) + ")</option>")
          .join("");
        select.value = current || (subs[0] ? subs[0].id : "");
      }
    }

    if (!list) return;
    if (!subs.length) {
      list.innerHTML =
        '<div class="empty"><div class="empty-icon">◈</div><div class="empty-title">尚未配置前缀订阅</div><div class="empty-hint">请在上方创建模式订阅</div></div>';
      return;
    }

    list.innerHTML = subs
      .map(
        (s) =>
          '<div class="list-item" style="padding: var(--s-3) 0;">' +
          '<div class="list-item-main">' +
          '<div class="list-item-title" style="font-weight:600;">' + escapeHtml(s.name) + '</div>' +
          '<div class="list-item-sub mono" style="margin-top:4px; font-size:11px;">' +
          escapeHtml(s.prefix) + (s.separator || "") + "{" + s.random_length + "位}." + escapeHtml(s.suffix) +
          ' · <span style="color:var(--accent)">' + escapeHtml(s.slot_type) + '</span>' +
          ' · ' + (s.auto_cloudflare ? '<span style="color:var(--ok)">自动托管 CF</span>' : '手动 NS') +
          '</div>' +
          '</div>' +
          '<div class="list-item-actions">' +
          '<button class="btn btn-danger btn-sm" onclick="deleteSubscription(\'' + escapeHtml(s.id) + "')\">删除</button>" +
          '</div>' +
          '</div>'
      )
      .join("");
  }

  /* =============== Jobs & Stepper =============== */
  function renderStep(step) {
    const icon = step.status === "success" ? "✓" : step.status === "failed" ? "!" : "·";
    return (
      '<div class="step ' + escapeHtml(step.status) + '">' +
      '<div class="step-dot">' + icon + "</div>" +
      '<div class="step-main">' +
      '<div class="step-title">' + escapeHtml(step.label) + "</div>" +
      (step.message ? '<div class="step-msg">' + escapeHtml(step.message) + "</div>" : "") +
      "</div>" +
      "</div>"
    );
  }

  function renderJobs() {
    const list = document.getElementById("job-list");
    if (!list) return;
    const jobs = overview.jobs || [];
    if (!jobs.length) {
      list.innerHTML =
        '<div class="empty"><div class="empty-icon">◈</div><div class="empty-title">暂无注册任务</div><div class="empty-hint">在左侧选择前缀订阅并启动任务</div></div>';
      return;
    }

    list.innerHTML = jobs
      .map((job) => {
        const attempts = (job.attempts || [])
          .slice(-10)
          .reverse()
          .map((attempt) => {
            const steps = (attempt.steps || []).map(renderStep).join("");
            return (
              '<div class="attempt-card" style="background:var(--bg-2);border:1px solid var(--line-1);border-radius:var(--r-2);padding:var(--s-3);margin-top:var(--s-2);">' +
              '<div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:var(--s-2);">' +
              '<strong class="mono" style="color:var(--ink-1);font-size:13px;">' + escapeHtml(attempt.candidate_domain) + "</strong>" +
              statusBadge(attempt.status) +
              "</div>" +
              '<div class="stepper">' + steps + "</div>" +
              (attempt.error ? '<div class="step-error" style="margin-top:var(--s-2);">' + escapeHtml(attempt.error) + "</div>" : "") +
              "</div>"
            );
          })
          .join("");

        return (
          '<div class="job-card" style="padding:var(--s-4);border-bottom:1px solid var(--line-1);">' +
          '<div style="display:flex; justify-content:space-between; align-items:flex-start;">' +
          '<div>' +
          '<div style="display:flex; align-items:center; gap:var(--s-2);">' +
          '<span class="mono" style="font-weight:700;font-size:13px;">' + escapeHtml(job.id) + '</span>' +
          statusBadge(job.status) +
          '</div>' +
          '<div class="hint mono" style="margin-top:4px;font-size:11px;">' +
          '模式：' + escapeHtml(job.subscription_name) + ' · 目标 ' + job.successful_domains + '/' + job.target_count +
          ' · 尝试 ' + job.completed_attempts + '/' + job.max_attempts +
          ' · 创建于 ' + formatTime(job.created_at) +
          '</div>' +
          '</div>' +
          '</div>' +
          (job.error ? '<div class="step-error" style="margin-top:var(--s-2);">' + escapeHtml(job.error) + "</div>" : "") +
          (attempts ? '<div class="attempts" style="margin-top:var(--s-3);">' + attempts + "</div>" : "") +
          "</div>"
        );
      })
      .join("");
  }

  /* =============== Domains table & Filtering =============== */
  function cloudflareDetail(d) {
    const status = d.cloudflare_status || "unmanaged";
    let badge = statusBadge(status);
    if (status === "active") {
      badge = '<span class="badge badge-ok"><i class="fa-solid fa-circle-check"></i> CF 已激活</span>';
    } else if (status === "pending") {
      badge = '<span class="badge badge-warn"><i class="fa-solid fa-clock"></i> CF 待生效</span>';
    } else if (status === "failed") {
      badge = '<span class="badge badge-err"><i class="fa-solid fa-circle-xmark"></i> CF 失败</span>';
    } else {
      badge = '<span class="badge badge-neutral"><i class="fa-solid fa-circle-minus"></i> 未托管</span>';
    }

    const zoneId = d.cloudflare_zone_id ? '<div class="hint mono" style="font-size:10px;margin-top:2px;" title="' + escapeHtml(d.cloudflare_zone_id) + '">Zone: ' + escapeHtml(d.cloudflare_zone_id.slice(0, 10)) + '…</div>' : '';
    const errorText = d.cloudflare_error ? '<div style="color:var(--err);font-size:10px;margin-top:2px" title="' + escapeHtml(d.cloudflare_error) + '">' + escapeHtml(d.cloudflare_error.slice(0, 30)) + '…</div>' : '';

    return badge + zoneId + errorText;
  }

  function renewalDetail(d) {
    const status = d.renewal_status || "untested";
    const days = d.renewal_days_remaining;
    let daysBadge = "";
    if (days != null) {
      if (days <= 30) {
        daysBadge = ' <span style="color:var(--err);font-weight:700">(' + days + '天)</span>';
      } else if (days <= 90) {
        daysBadge = ' <span style="color:var(--warn);font-weight:600">(' + days + '天)</span>';
      } else {
        daysBadge = ' <span style="color:var(--ink-3)">(' + days + '天)</span>';
      }
    }

    return (
      '<div style="font-size:12px;color:var(--ink-1); font-weight:500;">' + escapeHtml(d.expiry_date || "未知") + daysBadge + '</div>' +
      '<div class="hint" style="margin-top:2px;font-size:10px;">状态: ' + statusBadge(status) +
      (d.renewed_at ? ' · ' + formatTime(d.renewed_at) : '') +
      '</div>' +
      (d.renewal_error ? '<div style="color:var(--err);font-size:10px;margin-top:2px">' + escapeHtml(d.renewal_error.slice(0, 30)) + '…</div>' : '')
    );
  }

  function getFilteredDomains() {
    const query = (document.getElementById("domain-search") ? document.getElementById("domain-search").value : "").trim().toLowerCase();
    const filterStatus = document.getElementById("domain-filter-status") ? document.getElementById("domain-filter-status").value : "";
    const filterExpiry = document.getElementById("domain-filter-expiry") ? document.getElementById("domain-filter-expiry").value : "";
    const filterToken = document.getElementById("domain-filter-token") ? document.getElementById("domain-filter-token").value : "";
    const sort = document.getElementById("domain-sort") ? document.getElementById("domain-sort").value : "registered_desc";

    let list = (overview.domains || []).slice();

    if (query) {
      list = list.filter((d) => {
        const domain = (d.domain || "").toLowerCase();
        const token = (d.token_name || "").toLowerCase();
        const zone = (d.cloudflare_zone_id || "").toLowerCase();
        const ns = ((d.nameservers || []).join(" ")).toLowerCase();
        return domain.includes(query) || token.includes(query) || zone.includes(query) || ns.includes(query);
      });
    }

    if (filterStatus === "cf_active") {
      list = list.filter((d) => d.cloudflare_status === "active");
    } else if (filterStatus === "cf_pending") {
      list = list.filter((d) => d.cloudflare_status === "pending");
    } else if (filterStatus === "unmanaged") {
      list = list.filter((d) => !d.cloudflare_status || d.cloudflare_status === "unmanaged");
    } else if (filterStatus === "cf_failed") {
      list = list.filter((d) => d.cloudflare_status === "failed");
    }

    if (filterExpiry === "expiring_30") {
      list = list.filter((d) => d.renewal_days_remaining != null && d.renewal_days_remaining <= 30);
    } else if (filterExpiry === "expiring_90") {
      list = list.filter((d) => d.renewal_days_remaining != null && d.renewal_days_remaining <= 90);
    } else if (filterExpiry === "renewal_failed") {
      list = list.filter((d) => d.renewal_status === "failed");
    } else if (filterExpiry === "renewed") {
      list = list.filter((d) => d.renewal_status === "renewed" || d.renewed_at);
    }

    if (filterToken) {
      list = list.filter((d) => String(d.token_id || "") === String(filterToken));
    }

    if (sort === "registered_desc") {
      list.sort((a, b) => (b.registered_at || "").localeCompare(a.registered_at || ""));
    } else if (sort === "registered_asc") {
      list.sort((a, b) => (a.registered_at || "").localeCompare(b.registered_at || ""));
    } else if (sort === "expiry_asc") {
      list.sort((a, b) => {
        const da = a.renewal_days_remaining != null ? a.renewal_days_remaining : 99999;
        const db = b.renewal_days_remaining != null ? b.renewal_days_remaining : 99999;
        return da - db;
      });
    } else if (sort === "domain_asc") {
      list.sort((a, b) => (a.domain || "").localeCompare(b.domain || ""));
    }

    return list;
  }

  function filterDomains() {
    renderDomains();
  }

  function toggleSelectAllDomains() {
    const checked = document.getElementById("domain-select-all") ? document.getElementById("domain-select-all").checked : false;
    document.querySelectorAll(".domain-checkbox").forEach((cb) => {
      cb.checked = checked;
    });
  }

  function getSelectedDomains() {
    return Array.from(document.querySelectorAll(".domain-checkbox:checked")).map((cb) => cb.value);
  }

  function renderDomains() {
    const allDomains = overview.domains || [];
    const filtered = getFilteredDomains();
    const countEl = document.getElementById("domain-count");
    if (countEl) {
      countEl.textContent = "显示 " + filtered.length + " / 共 " + allDomains.length + " 个";
    }

    const tbody = document.getElementById("domain-table");
    if (!tbody) return;

    if (!filtered.length) {
      tbody.innerHTML =
        '<tr><td colspan="7"><div class="empty"><div class="empty-icon">◈</div><div class="empty-title">无匹配的域名</div><div class="empty-hint">请尝试调整搜索条件或筛选选项</div></div></td></tr>';
      return;
    }

    tbody.innerHTML = filtered
      .map(
        (d) =>
          "<tr>" +
          '<td><input type="checkbox" class="domain-checkbox" value="' + escapeHtml(d.domain) + '"></td>' +
          '<td>' +
          '<div style="display:flex; align-items:center; gap:6px;">' +
          '<strong style="color:var(--ink-1);font-size:13px; cursor:pointer;" onclick="copyToClipboard(\'' + escapeHtml(d.domain) + '\')" title="点击复制域名">' +
          escapeHtml(d.domain) +
          ' <i class="fa-regular fa-copy" style="font-size:10px; color:var(--ink-3);"></i></strong>' +
          '</div>' +
          '<div class="hint mono" style="margin-top:3px;font-size:11px">' + ((d.nameservers || []).map(escapeHtml).join(" · ") || "尚未设置 NS") + "</div>" +
          '</td>' +
          '<td><div class="row-title" style="font-size:12px;">' + escapeHtml(d.token_name || "-") + '</div><div class="hint" style="font-size:10px;">' + escapeHtml(d.slot_type || "-") + "</div></td>" +
          "<td>" + cloudflareDetail(d) + "</td>" +
          "<td>" + renewalDetail(d) + "</td>" +
          '<td class="mono" style="font-size:11px">' + escapeHtml(formatTime(d.registered_at)) + "</td>" +
          '<td>' +
          '<div style="display:flex; gap:4px; justify-content:flex-end; flex-wrap:wrap;">' +
          '<button class="btn btn-ghost btn-sm" onclick="viewDomainDetail(\'' + escapeHtml(d.domain) + '\')" title="查看详情与编辑NS">详情</button>' +
          (d.cloudflare_status === "active"
            ? '<button class="btn btn-secondary btn-sm" onclick="refreshDomain(\'' + escapeHtml(d.domain) + '\')" title="检测CF真实状态">检测</button>'
            : '<button class="btn btn-secondary btn-sm" onclick="hostCloudflare(\'' + escapeHtml(d.domain) + '\')" title="托管到 Cloudflare">托管CF</button>') +
          '<button class="btn btn-danger btn-sm" onclick="deleteDomain(\'' + escapeHtml(d.domain) + '\')" title="删除域名记录">删除</button>' +
          '</div>' +
          "</td>" +
          "</tr>"
      )
      .join("");
  }

  /* =============== Settings (Cloudflare & Renewal) =============== */
  function renderCloudflare() {
    const cf = overview.cloudflare;
    const el = document.getElementById("cloudflare-state");
    if (!el) return;
    if (!cf || !cf.account_id) {
      el.textContent = "尚未配置 Cloudflare 凭据";
      return;
    }
    const acc = document.getElementById("cf-account-id");
    if (acc && !acc.value) acc.value = cf.account_id || "";
    el.innerHTML =
      '<span class="badge ' + (cf.enabled ? "badge-ok" : "badge-err") + '">' + (cf.enabled ? "已启用" : "未验证") + "</span>" +
      '<span style="margin-left:8px;" class="mono">' + escapeHtml(cf.account_id) + "</span>" +
      (cf.last_tested_at ? '<span class="hint" style="margin-left:8px;">测试于 ' + formatTime(cf.last_tested_at) + "</span>" : "") +
      (cf.last_error ? '<div style="color:var(--err);font-size:11px;margin-top:4px">' + escapeHtml(cf.last_error) + "</div>" : "");
  }

  function renderRenewal() {
    const rn = overview.renewal || {};
    const el = document.getElementById("renewal-state");
    if (!el) return;
    const isRunning = rn.running;
    el.innerHTML =
      '<span class="badge ' + (rn.enabled ? (isRunning ? "badge-running" : "badge-ok") : "badge-gray") + '">' +
      (rn.enabled ? (isRunning ? "正在续期中…" : "自动续期已开启") : "自动续期已关闭") +
      "</span>" +
      (rn.last_run_at ? '<span class="hint" style="margin-left:8px;">上次运行：' + formatTime(rn.last_run_at) + "</span>" : "") +
      (rn.last_error ? '<div style="color:var(--err);font-size:11px;margin-top:4px">' + escapeHtml(rn.last_error) + "</div>" : "");
  }

  /* =============== Actions & Operations =============== */
  async function addToken() {
    const name = document.getElementById("token-name").value.trim();
    const token = document.getElementById("token-value").value.trim();
    if (!name || !token) {
      showToast("请填写 Token 名称与 Token 密钥", "warn");
      return;
    }
    try {
      await api("/api/domain-automation/tokens", {
        method: "POST",
        body: { name, token },
      });
      document.getElementById("token-value").value = "";
      showToast("Token 已添加");
      await refresh();
    } catch (e) {
      showToast(e.message, "error");
    }
  }

  async function testToken(id) {
    try {
      showToast("正在测试 Token…", "info");
      await api("/api/domain-automation/tokens/" + id + "/test", { method: "POST" });
      showToast("Token 测试成功，状态正常");
      await refresh();
    } catch (e) {
      showToast(e.message, "error");
    }
  }

  async function deleteToken(id) {
    if (!window.confirm("确认删除这个 API Token？")) return;
    try {
      await api("/api/domain-automation/tokens/" + id, { method: "DELETE" });
      showToast("Token 已删除");
      await refresh();
    } catch (e) {
      showToast(e.message, "error");
    }
  }

  function toggleNameservers() {
    const auto = document.getElementById("sub-auto-cloudflare").checked;
    const el = document.getElementById("manual-nameservers");
    if (el) el.style.display = auto ? "none" : "block";
  }

  async function saveCloudflare() {
    const account_id = document.getElementById("cf-account-id").value.trim();
    const api_token = document.getElementById("cf-token").value.trim();
    if (!account_id || !api_token) {
      showToast("请填写 Cloudflare Account ID 和 API Token", "warn");
      return;
    }
    try {
      await api("/api/domain-automation/cloudflare", {
        method: "PUT",
        body: { account_id, api_token },
      });
      document.getElementById("cf-token").value = "";
      showToast("Cloudflare 配置已保存");
      await refresh();
    } catch (e) {
      showToast(e.message, "error");
    }
  }

  async function testCloudflare() {
    try {
      showToast("正在测试 Cloudflare 连接…", "info");
      await api("/api/domain-automation/cloudflare/test", { method: "POST" });
      showToast("Cloudflare 验证成功，连接正常");
      await refresh();
    } catch (e) {
      showToast(e.message, "error");
    }
  }

  async function deleteCloudflare() {
    if (!window.confirm("确认删除 Cloudflare 配置？")) return;
    try {
      await api("/api/domain-automation/cloudflare", { method: "DELETE" });
      document.getElementById("cf-account-id").value = "";
      showToast("Cloudflare 配置已删除");
      await refresh();
    } catch (e) {
      showToast(e.message, "error");
    }
  }

  async function saveRenewal() {
    try {
      await api("/api/domain-automation/renewal", {
        method: "PUT",
        body: {
          enabled: document.getElementById("renew-enabled").checked,
          renew_before_days: Number(document.getElementById("renew-before").value),
          interval_seconds: Number(document.getElementById("renew-interval-hours").value) * 3600,
          renewal_type: document.getElementById("renew-type").value,
          renewal_years: Number(document.getElementById("renew-years").value),
          delay_min_seconds: Number(document.getElementById("renew-delay-min").value),
          delay_max_seconds: Number(document.getElementById("renew-delay-max").value),
        },
      });
      showToast("续期配置已保存");
      await refresh();
    } catch (e) {
      showToast(e.message, "error");
    }
  }

  async function runRenewal(force) {
    try {
      showToast("正在检查域名续期…", "info");
      const result = await api("/api/domain-automation/renewal/run", {
        method: "POST",
        body: { force: !!force },
      });
      showToast("续期检查完成：续期 " + result.renewed + " · 跳过 " + result.skipped + " · 失败 " + result.failed);
      await refresh();
    } catch (e) {
      showToast(e.message, "error");
    }
  }

  async function hostCloudflare(domain) {
    try {
      showToast("正在配置 " + domain + " 到 Cloudflare…", "info");
      await api("/api/domain-automation/domains/" + encodeURIComponent(domain) + "/cloudflare", { method: "POST" });
      showToast(domain + " 已更新 Cloudflare 托管");
      await refresh();
    } catch (e) {
      showToast(e.message, "error");
    }
  }

  async function refreshDomain(domain) {
    try {
      showToast("正在检测 " + domain + " 真实状态…", "info");
      await api("/api/domain-automation/domains/" + encodeURIComponent(domain) + "/refresh", { method: "POST" });
      showToast(domain + " 状态已更新");
      await refresh();
    } catch (e) {
      showToast("检测失败: " + e.message, "error");
    }
  }

  async function bulkRefreshCloudflare() {
    try {
      showToast("正在批量检测所有域名的 Cloudflare 真实状态…", "info");
      const res = await api("/api/domain-automation/domains/bulk-refresh-cf", { method: "POST" });
      showToast("检测完成：已刷新 " + res.refreshed_count + " 个域名（活跃 " + res.active_count + " · 待生效 " + res.pending_count + "）");
      await refresh();
    } catch (e) {
      showToast("批量检测失败: " + e.message, "error");
    }
  }

  async function cleanupCloudflareDuplicates() {
    if (!window.confirm("确认执行 Cloudflare 重复/失败托管清理？\n\n系统将自动合并重复域名记录，并重置失败的托管状态以便重新配置。")) return;
    try {
      showToast("正在清理重复与失败托管记录…", "info");
      const res = await api("/api/domain-automation/domains/cleanup-cf", { method: "POST" });
      showToast("清理完成：合并重复记录 " + res.merged_duplicates + " 条，重置失败状态 " + res.reset_failed_states + " 条");
      await refresh();
    } catch (e) {
      showToast("清理失败: " + e.message, "error");
    }
  }

  async function deleteDomain(domain) {
    const hasCf = overview.cloudflare && overview.cloudflare.enabled;
    let deleteCf = false;
    if (hasCf) {
      const choice = window.confirm("确认删除域名 " + domain + "？\n\n点击【确定】同时从 Cloudflare 移除 Zone；\n点击【取消】仅从本地记录删除（或取消操作）。");
      if (choice) {
        deleteCf = true;
      } else {
        if (!window.confirm("是否仅从本地记录删除域名 " + domain + "（保留 CF Zone）？")) return;
        deleteCf = false;
      }
    } else {
      if (!window.confirm("确认删除域名 " + domain + " 记录？")) return;
    }

    try {
      await api("/api/domain-automation/domains/" + encodeURIComponent(domain) + "?delete_cf_zone=" + deleteCf, {
        method: "DELETE",
      });
      showToast("域名 " + domain + " 已删除" + (deleteCf ? "（已联动移除 CF Zone）" : ""));
      await refresh();
    } catch (e) {
      showToast("删除失败: " + e.message, "error");
    }
  }

  async function bulkDeleteDomains() {
    const selected = getSelectedDomains();
    if (!selected.length) {
      showToast("请先在表格中勾选要删除的域名", "warn");
      return;
    }

    const hasCf = overview.cloudflare && overview.cloudflare.enabled;
    let deleteCf = false;
    if (hasCf) {
      const choice = window.confirm("确认批量删除选中的 " + selected.length + " 个域名？\n\n点击【确定】同时从 Cloudflare 移除对应 Zone；\n点击【取消】仅从本地记录删除。");
      if (choice) {
        deleteCf = true;
      } else {
        if (!window.confirm("是否仅从本地记录删除选中的 " + selected.length + " 个域名（保留 CF Zone）？")) return;
        deleteCf = false;
      }
    } else {
      if (!window.confirm("确认批量删除选中的 " + selected.length + " 个域名？")) return;
    }

    try {
      const res = await api("/api/domain-automation/domains/bulk-delete", {
        method: "POST",
        body: { domains: selected, delete_cf_zone: deleteCf },
      });
      showToast("已删除 " + res.deleted_count + " 个域名" + (res.cloudflare_zones_deleted ? "（移除 " + res.cloudflare_zones_deleted + " 个 CF Zone）" : ""));
      await refresh();
    } catch (e) {
      showToast("批量删除失败: " + e.message, "error");
    }
  }

  async function cleanupInvalidDomains() {
    if (!window.confirm("确认一键清理所有失败或无效的域名记录？")) return;
    try {
      const res = await api("/api/domain-automation/domains/cleanup", { method: "POST" });
      showToast("已清理 " + res.cleaned_count + " 条无效记录");
      await refresh();
    } catch (e) {
      showToast("清理失败: " + e.message, "error");
    }
  }

  async function syncDomains() {
    try {
      showToast("正在从 DigitalPlat 同步域名…", "info");
      const result = await api("/api/domain-automation/domains/sync", { method: "POST" });
      showToast("同步完成，新增 " + result.synced + " 个域名，现有 " + result.total + " 个");
      await refresh();
    } catch (e) {
      showToast(e.message, "error");
    }
  }

  /* =============== Bulk NS modal =============== */
  function showBulkNsModal() {
    const selected = getSelectedDomains();
    if (!selected.length) {
      showToast("请先在表格中勾选要修改 Nameservers 的域名", "warn");
      return;
    }
    const sub = document.getElementById("bulk-ns-modal-sub");
    if (sub) {
      sub.textContent = "已选中 " + selected.length + " 个域名，将统一更新为下方指定的 DNS 服务器";
    }
    document.getElementById("bulk-ns-modal").classList.add("active");
  }

  function closeBulkNsModal() {
    const modal = document.getElementById("bulk-ns-modal");
    if (modal) modal.classList.remove("active");
  }

  async function saveBulkNameservers() {
    const selected = getSelectedDomains();
    if (!selected.length) {
      showToast("未勾选任何域名", "warn");
      return;
    }
    const ns1 = document.getElementById("bulk-ns1").value.trim();
    const ns2 = document.getElementById("bulk-ns2").value.trim();
    if (!ns1 || !ns2) {
      showToast("请填写两个有效的 Nameservers", "warn");
      return;
    }

    try {
      showToast("正在批量更新 " + selected.length + " 个域名的 Nameservers…", "info");
      const res = await api("/api/domain-automation/domains/bulk-nameservers", {
        method: "POST",
        body: {
          domains: selected,
          nameservers: [ns1, ns2],
        },
      });
      showToast("批量更新完成：成功 " + res.updated_count + " 个，失败 " + res.failed_count + " 个");
      closeBulkNsModal();
      await refresh();
    } catch (e) {
      showToast("批量更新失败: " + e.message, "error");
    }
  }

  /* =============== Export =============== */
  function exportDomains(format) {
    const filtered = getFilteredDomains();
    if (!filtered.length) {
      showToast("当前无任何域名可导出", "warn");
      return;
    }

    if (format === "csv") {
      let csv = "\uFEFF域名,所属Token,容量类型,Cloudflare状态,Zone ID,到期时间,剩余天数,Nameservers,注册时间\n";
      filtered.forEach((d) => {
        const line = [
          '"' + (d.domain || "") + '"',
          '"' + (d.token_name || "") + '"',
          '"' + (d.slot_type || "") + '"',
          '"' + (d.cloudflare_status || "unmanaged") + '"',
          '"' + (d.cloudflare_zone_id || "") + '"',
          '"' + (d.expiry_date || "") + '"',
          '"' + (d.renewal_days_remaining != null ? d.renewal_days_remaining : "") + '"',
          '"' + ((d.nameservers || []).join(" | ")) + '"',
          '"' + (d.registered_at || "") + '"',
        ].join(",");
        csv += line + "\n";
      });

      const blob = new Blob([csv], { type: "text/csv;charset=utf-8;" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = "digitalplat-domains-" + new Date().toISOString().slice(0, 10) + ".csv";
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
      showToast("已导出 " + filtered.length + " 个域名到 CSV");
    }
  }

  /* =============== Domain Detail Modal =============== */
  function viewDomainDetail(domainName) {
    const domain = (overview.domains || []).find((d) => d.domain === domainName);
    if (!domain) {
      showToast("未找到该域名记录", "error");
      return;
    }

    document.getElementById("domain-modal-title").textContent = domain.domain;
    document.getElementById("domain-modal-sub").textContent = "所属 Token: " + (domain.token_name || "-") + " · 容量类型: " + (domain.slot_type || "-");

    const nsList = domain.nameservers || [];
    const ns1 = nsList[0] || "ns1.cloudflare.com";
    const ns2 = nsList[1] || "ns2.cloudflare.com";

    const steps = (domain.cloudflare_steps || []).map((s) => {
      const icon = s.status === "success" ? "✓" : s.status === "failed" ? "!" : "·";
      return '<div style="font-size:12px; margin-bottom:4px;"><span class="step-dot" style="display:inline-block;width:16px;height:16px;line-height:16px;text-align:center;border-radius:50%;background:var(--bg-3);margin-right:6px;font-size:10px;">' + icon + '</span><strong>' + escapeHtml(s.label) + ':</strong> ' + escapeHtml(s.message) + '</div>';
    }).join("");

    let html =
      '<div class="batch-config-box">' +
      '<div class="batch-config-box-title"><i class="fa-solid fa-circle-info" style="color:var(--accent)"></i> 域名基础状态</div>' +
      '<div class="batch-config-grid">' +
      '<div class="config-item"><div class="config-item-label">注册状态</div><div class="config-item-value">' + statusBadge(domain.status || "ok") + '</div></div>' +
      '<div class="config-item"><div class="config-item-label">注册时间</div><div class="config-item-value mono">' + escapeHtml(formatTime(domain.registered_at)) + '</div></div>' +
      '<div class="config-item"><div class="config-item-label">到期时间</div><div class="config-item-value mono">' + escapeHtml(domain.expiry_date || "未知") + (domain.renewal_days_remaining != null ? ' (剩余 ' + domain.renewal_days_remaining + ' 天)' : '') + '</div></div>' +
      '<div class="config-item"><div class="config-item-label">续期状态</div><div class="config-item-value">' + statusBadge(domain.renewal_status || "untested") + '</div></div>' +
      '</div>' +
      '</div>';

    html +=
      '<div class="batch-config-box">' +
      '<div class="batch-config-box-title"><i class="fa-solid fa-cloud" style="color:var(--info)"></i> Cloudflare 托管详情</div>' +
      '<div class="batch-config-grid">' +
      '<div class="config-item"><div class="config-item-label">托管状态</div><div class="config-item-value">' + statusBadge(domain.cloudflare_status || "unmanaged") + '</div></div>' +
      '<div class="config-item"><div class="config-item-label">Zone ID</div><div class="config-item-value mono">' + escapeHtml(domain.cloudflare_zone_id || "尚未创建") + '</div></div>' +
      '<div class="config-item"><div class="config-item-label">CF Nameservers</div><div class="config-item-value mono">' + escapeHtml((domain.cloudflare_nameservers || []).join(" · ") || "-") + '</div></div>' +
      '<div class="config-item"><div class="config-item-label">最后检测时间</div><div class="config-item-value mono">' + escapeHtml(formatTime(domain.cloudflare_checked_at) || "-") + '</div></div>' +
      '</div>' +
      (steps ? '<div style="margin-top:10px;padding-top:10px;border-top:1px dashed var(--line-1);">' + steps + '</div>' : '') +
      (domain.cloudflare_error ? '<div class="step-error" style="margin-top:10px;"><strong>CF 错误：</strong>' + escapeHtml(domain.cloudflare_error) + '</div>' : '') +
      '</div>';

    html +=
      '<div class="batch-config-box">' +
      '<div class="batch-config-box-title"><i class="fa-solid fa-server" style="color:var(--accent)"></i> 修改 Nameservers 解析服务器</div>' +
      '<div class="form-row" style="margin-bottom:0;">' +
      '<div><label>Nameserver 1</label><input type="text" id="edit-domain-ns1" value="' + escapeHtml(ns1) + '"></div>' +
      '<div><label>Nameserver 2</label><input type="text" id="edit-domain-ns2" value="' + escapeHtml(ns2) + '"></div>' +
      '</div>' +
      '<div style="display:flex; gap: var(--s-3); align-items:center; margin-top:10px; flex-wrap:wrap;">' +
      '<button class="btn btn-primary btn-sm" onclick="saveDomainNameservers(\'' + escapeHtml(domain.domain) + '\')"><i class="fa-solid fa-check"></i> 更新 Nameservers</button>' +
      '<button class="btn btn-secondary btn-sm" onclick="refreshDomain(\'' + escapeHtml(domain.domain) + '\')"><i class="fa-solid fa-arrows-rotate"></i> 检测真实状态</button>' +
      '<button class="btn btn-ghost btn-sm" onclick="hostCloudflare(\'' + escapeHtml(domain.domain) + '\')"><i class="fa-solid fa-cloud-arrow-up"></i> 重新托管到 CF</button>' +
      '<button class="btn btn-danger btn-sm" onclick="closeDomainModal(); deleteDomain(\'' + escapeHtml(domain.domain) + '\')"><i class="fa-solid fa-trash"></i> 删除此域名</button>' +
      '</div>' +
      '</div>';

    document.getElementById("domain-modal-content").innerHTML = html;
    document.getElementById("domain-detail-modal").classList.add("active");
  }

  async function saveDomainNameservers(domain) {
    const ns1 = document.getElementById("edit-domain-ns1").value.trim();
    const ns2 = document.getElementById("edit-domain-ns2").value.trim();
    if (!ns1 || !ns2) {
      showToast("请填写两个有效的 Nameservers", "warn");
      return;
    }

    try {
      showToast("正在更新 Nameservers…", "info");
      await api("/api/domain-automation/domains/" + encodeURIComponent(domain) + "/nameservers", {
        method: "PATCH",
        body: { nameservers: [ns1, ns2] },
      });
      showToast("Nameservers 更新成功");
      await refresh();
      viewDomainDetail(domain);
    } catch (e) {
      showToast("更新失败: " + e.message, "error");
    }
  }

  function closeDomainModal() {
    const modal = document.getElementById("domain-detail-modal");
    if (modal) modal.classList.remove("active");
  }

  async function addSubscription() {
    try {
      const autoCloudflare = document.getElementById("sub-auto-cloudflare").checked;
      await api("/api/domain-automation/subscriptions", {
        method: "POST",
        body: {
          name: document.getElementById("sub-name").value,
          prefix: document.getElementById("sub-prefix").value,
          suffix: document.getElementById("sub-suffix").value,
          slot_type: document.getElementById("sub-slot").value,
          random_length: Number(document.getElementById("sub-length").value),
          separator: document.getElementById("sub-separator").value,
          auto_cloudflare: autoCloudflare,
          nameservers: autoCloudflare
            ? []
            : [document.getElementById("sub-ns1").value, document.getElementById("sub-ns2").value],
        },
      });
      showToast("前缀订阅已创建");
      await refresh();
    } catch (e) {
      showToast(e.message, "error");
    }
  }

  async function deleteSubscription(id) {
    if (!window.confirm("确认删除这个前缀订阅？")) return;
    try {
      await api("/api/domain-automation/subscriptions/" + id, { method: "DELETE" });
      showToast("前缀订阅已删除");
      await refresh();
    } catch (e) {
      showToast(e.message, "error");
    }
  }

  async function startJob() {
    const button = document.getElementById("start-job");
    button.disabled = true;
    try {
      const tokenMode = document.getElementById("job-token-mode").value;
      const job = await api("/api/domain-automation/jobs", {
        method: "POST",
        body: {
          subscription_id: document.getElementById("job-subscription").value,
          target_count: Number(document.getElementById("job-count").value),
          token_ids: tokenMode === "all" ? null : [tokenMode],
          max_attempts: Number(document.getElementById("job-attempts").value),
          delay_min_seconds: Number(document.getElementById("job-delay-min").value),
          delay_max_seconds: Number(document.getElementById("job-delay-max").value),
        },
      });
      showToast("任务已启动：" + job.id);
      await refresh();
    } catch (e) {
      showToast(e.message, "error");
    } finally {
      button.disabled = false;
    }
  }

  /* =============== Expose globals =============== */
  Object.assign(window, {
    showDomainTab,
    refresh,
    addToken,
    testToken,
    deleteToken,
    toggleNameservers,
    saveCloudflare,
    testCloudflare,
    deleteCloudflare,
    saveRenewal,
    runRenewal,
    hostCloudflare,
    refreshDomain,
    bulkRefreshCloudflare,
    cleanupCloudflareDuplicates,
    showBulkNsModal,
    closeBulkNsModal,
    saveBulkNameservers,
    exportDomains,
    copyToClipboard,
    deleteDomain,
    bulkDeleteDomains,
    cleanupInvalidDomains,
    syncDomains,
    filterDomains,
    toggleSelectAllDomains,
    viewDomainDetail,
    saveDomainNameservers,
    closeDomainModal,
    addSubscription,
    deleteSubscription,
    startJob,
  });

  /* Boot */
  refresh();
  setInterval(refresh, 3000);
})();
