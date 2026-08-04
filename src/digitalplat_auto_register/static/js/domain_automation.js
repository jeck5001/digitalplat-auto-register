/* ============================================================
   domain_automation.js — /domain-automation page JS
   Depends on `DP` from api.js. Polls /api/domain-automation every 3s.
   ============================================================ */

(function () {
  "use strict";

  const { api, escapeHtml, statusBadge, formatTime, showToast, setConnected } = window.DP;

  /* Cached overview state */
  let overview = {
    tokens: [],
    subscriptions: [],
    jobs: [],
    domains: [],
    cloudflare: null,
    renewal: null,
    stats: {},
  };

  /* Step labels for domain registration pipeline */
  const ATTEMPT_STEP_LABELS = {
    candidate_generation: "生成候选域名",
    token_assignment: "分配 API Token",
    registration_request: "提交注册请求",
    registration_verification: "确认注册结果",
  };
  const ATTEMPT_STEP_ORDER = [
    "candidate_generation",
    "token_assignment",
    "registration_request",
    "registration_verification",
  ];

  /* =============== Refresh loop =============== */
  async function refresh() {
    try {
      overview = await api("/api/domain-automation");
      render();
      setConnected(document.getElementById("connection"), true);
    } catch (error) {
      setConnected(document.getElementById("connection"), false);
    }
  }

  function setText(id, val) {
    const el = document.getElementById(id);
    if (el) el.textContent = val;
  }

  /* =============== Top-level render =============== */
  function render() {
    const s = overview.stats || {};
    setText("stat-tokens", s.tokens || 0);
    setText("stat-enabled", s.enabled_tokens || 0);
    setText("stat-subscriptions", s.subscriptions || 0);
    setText("stat-running", s.running_jobs || 0);
    setText("stat-domains", s.registered_domains || 0);
    setText("stat-cloudflare", s.cloudflare_active || 0);
    renderTokens();
    renderCloudflare();
    renderRenewal();
    renderSubscriptions();
    renderJobs();
    renderDomains();
  }

  /* =============== Cloudflare panel =============== */
  function renderCloudflare() {
    const c = overview.cloudflare;
    const state = document.getElementById("cloudflare-state");
    if (!c) {
      state.innerHTML = '<span class="hint">尚未配置</span>';
      return;
    }
    const cid = document.getElementById("cf-account-id");
    if (cid) cid.value = c.account_id || "";
    state.innerHTML =
      statusBadge(c.last_status) +
      ' <code style="margin-left:6px">' + escapeHtml(c.token_masked) + "</code>" +
      (c.last_checked_at ? ' <span class="hint" style="margin-left:6px">' + formatTime(c.last_checked_at) + "</span>" : "") +
      (c.last_error ? ' <span style="color:var(--err);margin-left:6px;font-size:11px">' + escapeHtml(c.last_error) + "</span>" : "");
  }

  /* =============== Renewal panel =============== */
  function renderRenewal() {
    const r = overview.renewal || {};
    const setVal = (id, v) => { const el = document.getElementById(id); if (el) el.value = v; };
    const setChk = (id, v) => { const el = document.getElementById(id); if (el) el.checked = v; };

    setChk("renew-enabled", r.enabled !== false);
    setVal("renew-before", r.renew_before_days ?? 120);
    setVal("renew-interval-hours", Math.max(1, Math.round((r.interval_seconds || 86400) / 3600)));
    setVal("renew-type", r.renewal_type || "free");
    setVal("renew-years", r.renewal_years || 1);
    setVal("renew-delay-min", r.delay_min_seconds ?? 3);
    setVal("renew-delay-max", r.delay_max_seconds ?? 6);

    const s = r.last_summary || {};
    document.getElementById("renewal-state").innerHTML =
      statusBadge(r.last_status || "untested") +
      ' <span class="hint" style="margin-left:8px">' +
      (r.last_run_at ? "上次运行 " + formatTime(r.last_run_at) : "尚未运行") +
      (r.last_run_at
        ? " · 检查 " + (s.checked || 0) + " · 续期 " + (s.renewed || 0) + " · 跳过 " + (s.skipped || 0) + " · 失败 " + (s.failed || 0)
        : "") +
      (r.last_error ? ' · <span style="color:var(--err)">' + escapeHtml(r.last_error) + "</span>" : "") +
      "</span>";
  }

  /* =============== Token pool =============== */
  function renderTokens() {
    const root = document.getElementById("token-list");
    const toks = overview.tokens || [];
    if (!toks.length) {
      root.innerHTML =
        '<div class="empty"><div class="empty-icon">◉</div><div class="empty-title">尚未添加 API Token</div><div class="empty-hint">从 DigitalPlat 控制台获取 dp_live_ 开头的 Token</div></div>';
      return;
    }
    root.innerHTML = toks
      .map(
        (t) =>
          '<div class="list-row">' +
          "<div>" +
          '<div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">' +
          '<div class="row-title">' + escapeHtml(t.name) + "</div>" +
          statusBadge(t.last_status) +
          "</div>" +
          '<div class="row-meta"><code>' + escapeHtml(t.token_masked) + "</code>" +
          " · " + (t.domain_count == null ? "域名数未知" : t.domain_count + " 个域名") +
          (t.last_error ? ' · <span style="color:var(--err)">' + escapeHtml(t.last_error) + "</span>" : "") +
          "</div>" +
          "</div>" +
          '<div style="display:flex; gap: 6px;">' +
          '<button class="btn btn-secondary btn-sm" onclick="testToken(\'' + escapeHtml(t.id) + "')\">测试</button>" +
          '<button class="btn btn-danger btn-sm" onclick="deleteToken(\'' + escapeHtml(t.id) + "')\">删除</button>" +
          "</div>" +
          "</div>"
      )
      .join("");
  }

  /* =============== Subscriptions =============== */
  function renderSubscriptions() {
    const root = document.getElementById("subscription-list");
    const subs = overview.subscriptions || [];
    if (!subs.length) {
      root.innerHTML =
        '<div class="empty"><div class="empty-icon">▣</div><div class="empty-title">尚未创建前缀订阅</div><div class="empty-hint">订阅定义了生成候选域名的模式</div></div>';
    } else {
      root.innerHTML = subs
        .map((s) => {
          const separator = s.suffix === "dpdns.org" ? "" : s.separator;
          const routing = s.auto_cloudflare
            ? "Cloudflare 自动托管"
            : (s.nameservers || []).map(escapeHtml).join(" · ");
          return (
            '<div class="list-row">' +
            "<div>" +
            '<div class="row-title">' + escapeHtml(s.name) + (s.auto_cloudflare ? " " + statusBadge("active") : "") + "</div>" +
            '<div class="row-meta">' +
            escapeHtml(s.prefix || "[随机]") + escapeHtml(separator) + "x".repeat(Math.min(s.random_length, 8)) + "." + escapeHtml(s.suffix) +
            " · " + escapeHtml(s.slot_type) + "<br>" + routing +
            "</div>" +
            "</div>" +
            '<div><button class="btn btn-danger btn-sm" onclick="deleteSubscription(\'' + escapeHtml(s.id) + "')\">删除</button></div>" +
            "</div>"
          );
        })
        .join("");
    }

    /* Update job-launcher subscription select */
    const select = document.getElementById("job-subscription");
    if (select) {
      const current = select.value;
      select.innerHTML =
        '<option value="">选择一个前缀订阅</option>' +
        subs
          .map((s) => '<option value="' + escapeHtml(s.id) + '">' + escapeHtml(s.name) + " — " + escapeHtml(s.prefix || "[随机]") + "." + escapeHtml(s.suffix) + "</option>")
          .join("");
      if (subs.some((s) => s.id === current)) select.value = current;
    }
  }

  /* =============== Registration attempts (per-job stepper) =============== */
  function attemptSteps(attempt) {
    const byName = Object.fromEntries((attempt.steps || []).map((s) => [s.name, s]));
    return (
      '<div class="step-timeline" style="grid-template-columns: repeat(4, 1fr)">' +
      ATTEMPT_STEP_ORDER.map((name) => {
        const step = byName[name] || {
          label: ATTEMPT_STEP_LABELS[name],
          status: "pending",
          message: "等待执行",
        };
        const icon = step.status === "success" ? "✓" : step.status === "failed" ? "!" : "";
        return (
          '<div class="timeline-step ' + escapeHtml(step.status) + '">' +
          '<span class="step-dot">' + icon + "</span>" +
          '<div class="step-name">' + escapeHtml(step.label) + "</div>" +
          '<div class="step-meta">' + escapeHtml(step.message || "") + "</div>" +
          "</div>"
        );
      }).join("") +
      "</div>"
    );
  }

  /* =============== Jobs list =============== */
  function renderJobs() {
    const root = document.getElementById("job-list");
    const jobs = overview.jobs || [];
    if (!jobs.length) {
      root.innerHTML =
        '<div class="empty" style="padding: 48px 24px"><div class="empty-icon">▸</div><div class="empty-title">暂无注册任务</div><div class="empty-hint">创建订阅后点击「开始自动注册」</div></div>';
      return;
    }

    const openJobs = new Set([...document.querySelectorAll(".job[open]")].map((e) => e.dataset.id));
    const openAttempts = new Set([...document.querySelectorAll(".attempt[open]")].map((e) => e.dataset.id));

    root.innerHTML = jobs
      .map((j, index) => {
        const pct = Math.round((j.completed_attempts / Math.max(j.max_attempts, 1)) * 100);
        const attempts = (j.attempts || [])
          .map((a, i) => {
            const isAttemptOpen = openAttempts.has(a.id) || (!index && !i);
            return (
              '<details class="attempt" data-id="' + escapeHtml(a.id) + '"' + (isAttemptOpen ? " open" : "") + ">" +
              '<summary class="attempt-summary">' +
              "<div>" +
              '<div class="row-title">' + escapeHtml(a.domain) + "</div>" +
              '<div class="row-meta">' + formatTime(a.created_at) + "</div>" +
              "</div>" +
              '<div class="attempt-token">' + escapeHtml(a.token_name) + "</div>" +
              "<div>" + statusBadge(a.status) + "</div>" +
              '<div class="chevron">›</div>' +
              "</summary>" +
              attemptSteps(a) +
              (a.error ? '<div style="color:var(--err);font-size:12px;padding-bottom:14px;padding-left:4px;">' + escapeHtml(a.error) + "</div>" : "") +
              "</details>"
            );
          })
          .join("");

        const isJobOpen = openJobs.has(j.id) || index === 0;
        return (
          '<details class="job" data-id="' + escapeHtml(j.id) + '"' + (isJobOpen ? " open" : "") + ">" +
          '<summary class="job-summary">' +
          "<div>" +
          '<div class="row-title">任务 <code>' + escapeHtml(j.id) + "</code></div>" +
          '<div class="row-meta">成功 ' + j.successful_domains + "/" + j.target_count + " · 失败尝试 " + j.failed_attempts + "</div>" +
          "</div>" +
          "<div>" + statusBadge(j.status) + "</div>" +
          '<div class="job-progress"><div style="font-size:10px;color:var(--ink-3);margin-bottom:4px">尝试 ' + j.completed_attempts + "/" + j.max_attempts + '</div><div class="progress"><div class="progress-fill" style="width:' + pct + '%"></div></div></div>' +
          '<div class="chevron">›</div>' +
          "</summary>" +
          '<div class="job-body">' +
          (j.error ? '<div style="color:var(--err);font-size:12px;padding:12px 0">' + escapeHtml(j.error) + "</div>" : "") +
          (attempts || '<div class="empty" style="padding: 20px">等待生成候选域名</div>') +
          "</div>" +
          "</details>"
        );
      })
      .join("");
  }

  /* =============== Domains table =============== */
  function cloudflareDetail(d) {
    const steps = (d.cloudflare_steps || []).map((s) => escapeHtml(s.label) + "：" + escapeHtml(s.message)).join("<br>");
    return (
      statusBadge(d.cloudflare_status || "unmanaged") +
      (steps ? '<div class="hint" style="margin-top:5px">' + steps + "</div>" : "") +
      (d.cloudflare_error ? '<div style="color:var(--err);font-size:11px;margin-top:4px">' + escapeHtml(d.cloudflare_error) + "</div>" : "")
    );
  }

  function renewalDetail(d) {
    const status = d.renewal_status || "untested";
    return (
      statusBadge(status) +
      '<div class="hint" style="margin-top:5px">到期：' + escapeHtml(d.expiry_date || "未知") +
      (d.renewal_days_remaining == null ? "" : " · 剩余 " + d.renewal_days_remaining + " 天") +
      (d.renewed_at ? " · 上次续期 " + formatTime(d.renewed_at) : "") +
      "</div>" +
      (d.renewal_error ? '<div style="color:var(--err);font-size:11px;margin-top:4px">' + escapeHtml(d.renewal_error) + "</div>" : "")
    );
  }

  function renderDomains() {
    document.getElementById("domain-count").textContent = (overview.domains || []).length + " 个";
    const tbody = document.getElementById("domain-table");
    const domains = overview.domains || [];
    if (!domains.length) {
      tbody.innerHTML =
        '<tr><td colspan="6"><div class="empty"><div class="empty-icon">◈</div><div class="empty-title">暂无成功注册的域名</div></div></td></tr>';
      return;
    }
    tbody.innerHTML = domains
      .map(
        (d) =>
          "<tr>" +
          '<td><strong style="color:var(--ink-1)">' + escapeHtml(d.domain) + "</strong>" +
          '<div class="hint mono" style="margin-top:4px;font-size:10px">' + ((d.nameservers || []).map(escapeHtml).join(" · ") || "尚未设置 NS") + "</div></td>" +
          '<td><div class="row-title">' + escapeHtml(d.token_name) + '</div><div class="hint">' + escapeHtml(d.slot_type) + "</div></td>" +
          "<td>" + cloudflareDetail(d) + "</td>" +
          "<td>" + renewalDetail(d) + "</td>" +
          '<td class="mono" style="font-size:11px">' + escapeHtml(formatTime(d.registered_at)) + "</td>" +
          '<td><button class="btn btn-secondary btn-sm" onclick="hostCloudflare(\'' + escapeHtml(d.domain) + "')\">" +
          (d.cloudflare_status ? "重试 / 刷新" : "托管到 Cloudflare") +
          "</button></td>" +
          "</tr>"
      )
      .join("");
  }

  /* =============== Actions =============== */
  async function addToken() {
    try {
      await api("/api/domain-automation/tokens", {
        method: "POST",
        body: {
          name: document.getElementById("token-name").value,
          token: document.getElementById("token-value").value,
        },
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
      await refresh();
    } catch (e) {
      showToast(e.message, "error");
    }
  }

  async function deleteToken(id) {
    if (!window.confirm("确认删除这个 API Token？")) return;
    try {
      await api("/api/domain-automation/tokens/" + id, { method: "DELETE" });
      await refresh();
    } catch (e) {
      showToast(e.message, "error");
    }
  }

  function toggleNameservers() {
    const auto = document.getElementById("sub-auto-cloudflare").checked;
    document.getElementById("manual-nameservers").style.display = auto ? "none" : "block";
  }

  async function saveCloudflare() {
    try {
      await api("/api/domain-automation/cloudflare", {
        method: "PUT",
        body: {
          account_id: document.getElementById("cf-account-id").value,
          api_token: document.getElementById("cf-token").value,
        },
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
      showToast("正在测试 Cloudflare Token…", "info");
      await api("/api/domain-automation/cloudflare/test", { method: "POST" });
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
      showToast("正在配置 " + domain + "…", "info");
      await api("/api/domain-automation/domains/" + encodeURIComponent(domain) + "/cloudflare", { method: "POST" });
      await refresh();
    } catch (e) {
      showToast(e.message, "error");
    }
  }

  async function syncDomains() {
    try {
      const result = await api("/api/domain-automation/domains/sync", { method: "POST" });
      showToast("同步完成，新增 " + result.synced + " 个域名");
      await refresh();
    } catch (e) {
      showToast(e.message, "error");
    }
  }

  async function hostAllCloudflare() {
    const domains = (overview.domains || []).filter((d) => d.cloudflare_status !== "active");
    if (!domains.length) {
      showToast("没有待托管域名");
      return;
    }
    if (!window.confirm("确认依次托管 " + domains.length + " 个域名到 Cloudflare？")) return;
    for (const d of domains) {
      showToast("正在配置 " + d.domain + "…", "info");
      try {
        await api("/api/domain-automation/domains/" + encodeURIComponent(d.domain) + "/cloudflare", { method: "POST" });
      } catch (e) {
        showToast(d.domain + "：" + e.message, "error");
      }
    }
    await refresh();
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
      await refresh();
    } catch (e) {
      showToast(e.message, "error");
    }
  }

  async function startJob() {
    const button = document.getElementById("start-job");
    button.disabled = true;
    try {
      const job = await api("/api/domain-automation/jobs", {
        method: "POST",
        body: {
          subscription_id: document.getElementById("job-subscription").value,
          target_count: Number(document.getElementById("job-count").value),
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
    syncDomains,
    hostAllCloudflare,
    addSubscription,
    deleteSubscription,
    startJob,
  });

  /* Boot */
  refresh();
  setInterval(refresh, 3000);
})();
