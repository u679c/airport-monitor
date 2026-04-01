const ids = (id) => document.getElementById(id);

const HISTORY_WINDOW_SEC = 24 * 3600;
const FINE_BUCKET_MINUTES = 10;
const COARSE_BUCKET_MINUTES = 20;
const FILTER_STORAGE_KEY = "airport_monitor_node_filter";
const THEME_STORAGE_KEY = "airport_monitor_theme";
const CONFIG_COLLAPSED_KEY = "airport_monitor_config_collapsed";

const refs = {
  subscription_url: ids("subscription_url"),
  interval_sec: ids("interval_sec"),
  timeout_ms: ids("timeout_ms"),
  concurrency: ids("concurrency"),
  max_nodes: ids("max_nodes"),
  runOnceBtn: ids("runOnceBtn"),
  refreshBtn: ids("refreshBtn"),
  saveConfigBtn: ids("saveConfigBtn"),
  stats: ids("stats"),
  lastCheck: ids("lastCheck"),
  nextCheck: ids("nextCheck"),
  runningState: ids("runningState"),
  errorText: ids("errorText"),
  nodeList: ids("nodeList"),
  nodeFilterInput: ids("nodeFilterInput"),
  clearFilterBtn: ids("clearFilterBtn"),
  themeSelect: ids("themeSelect"),
  themeStylesheet: ids("themeStylesheet"),
  configPanel: ids("configPanel"),
  toggleConfigBtn: ids("toggleConfigBtn"),
};

let latestNodes = [];
const statusTextMap = {
  online: "在线",
  normal: "一般",
  slow: "偏慢",
  offline: "离线",
  empty: "无数据",
};

function applyTheme(theme) {
  const safeTheme = theme === "light" ? "light" : "dark";
  refs.themeStylesheet.setAttribute("href", `/static/themes/${safeTheme}.css`);
  refs.themeSelect.value = safeTheme;
  localStorage.setItem(THEME_STORAGE_KEY, safeTheme);
}

function applyConfigCollapsed(collapsed) {
  refs.configPanel.classList.toggle("collapsed", collapsed);
  refs.toggleConfigBtn.textContent = collapsed ? "+" : "-";
  localStorage.setItem(CONFIG_COLLAPSED_KEY, collapsed ? "1" : "0");
}

function getBucketConfig() {
  const width = refs.nodeList?.clientWidth || window.innerWidth || 0;
  const bucketMinutes = width >= 1100 ? FINE_BUCKET_MINUTES : COARSE_BUCKET_MINUTES;
  const buckets = Math.floor((24 * 60) / bucketMinutes);
  return { bucketMinutes, buckets };
}

function loadFilter() {
  const saved = localStorage.getItem(FILTER_STORAGE_KEY) || "";
  refs.nodeFilterInput.value = saved;
}

function saveFilter() {
  localStorage.setItem(FILTER_STORAGE_KEY, refs.nodeFilterInput.value.trim());
}

function renderStats(summary) {
  const cards = [
    ["节点总数", summary.total],
    ["可用节点", summary.online + summary.normal + summary.slow],
    ["离线节点", summary.offline],
    ["平均延迟", summary.avg_latency_ms == null ? "-" : `${summary.avg_latency_ms} ms`],
  ];
  refs.stats.innerHTML = cards
    .map(([k, v]) => `<div class="stat-card"><span>${k}</span><b>${v}</b></div>`)
    .join("");
}

function buildHistoryBars(history, buckets) {
  const now = Date.now() / 1000;
  const start = now - HISTORY_WINDOW_SEC;
  const bucketSize = HISTORY_WINDOW_SEC / buckets;
  const points = new Array(buckets).fill(null).map(() => ({ status: "empty", ts: null }));
  const latestTs = new Array(buckets).fill(-1);

  for (const item of history || []) {
    const ts = Number(item?.ts);
    const status = item?.status;
    if (!Number.isFinite(ts) || typeof status !== "string") {
      continue;
    }
    if (ts < start || ts > now) {
      continue;
    }
    const idx = Math.min(buckets - 1, Math.floor((ts - start) / bucketSize));
    if (ts >= latestTs[idx]) {
      latestTs[idx] = ts;
      points[idx] = { status, ts };
    }
  }

  return points;
}

function formatTs(ts) {
  if (!Number.isFinite(ts)) return "-";
  const d = new Date(ts * 1000);
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  const hh = String(d.getHours()).padStart(2, "0");
  const mm = String(d.getMinutes()).padStart(2, "0");
  const ss = String(d.getSeconds()).padStart(2, "0");
  return `${y}-${m}-${day} ${hh}:${mm}:${ss}`;
}

function getTooltipEl() {
  let el = document.getElementById("barTooltip");
  if (el) return el;
  el = document.createElement("div");
  el.id = "barTooltip";
  el.className = "bar-tooltip";
  document.body.appendChild(el);
  return el;
}

function showBarTooltip(barEl, clientX, clientY) {
  const tooltip = getTooltipEl();
  const ts = Number(barEl.dataset.ts);
  const status = barEl.dataset.status || "empty";
  tooltip.innerHTML = `<div>${formatTs(ts)}</div><div>状态：${statusTextMap[status] || status}</div>`;
  tooltip.classList.add("show");
  tooltip.style.left = `${clientX + 12}px`;
  tooltip.style.top = `${clientY + 12}px`;
}

function hideBarTooltip() {
  const tooltip = getTooltipEl();
  tooltip.classList.remove("show");
}

function renderNodes(nodes) {
  const { bucketMinutes, buckets } = getBucketConfig();
  const keyword = refs.nodeFilterInput.value.trim().toLowerCase();
  const filtered = !keyword
    ? nodes
    : nodes.filter((n) => String(n.name || "").toLowerCase().includes(keyword));

  if (!filtered.length) {
    refs.nodeList.innerHTML = `<div class="node-card">${nodes.length ? "没有匹配的节点，请调整筛选关键字。" : "暂无节点数据，请先配置订阅并点击“立即检测”。"}</div>`;
    return;
  }

  refs.nodeList.innerHTML = filtered
    .map((n) => {
      const bars = buildHistoryBars(n.history, buckets)
        .map((h) => `<i class="bar ${h.status}" data-status="${h.status}" data-ts="${h.ts ?? ""}"></i>`)
        .join("");
      const latency = n.latency_ms == null ? "-" : `${n.latency_ms} ms`;
      return `
        <article class="node-card">
          <div class="node-top">
            <div>
              <div class="node-title">${n.name}</div>
              <div class="node-sub">${n.protocol.toUpperCase()} · ${n.host}:${n.port} · 24h可用率 ${n.success_rate}% · ${bucketMinutes}分钟粒度</div>
            </div>
            <div class="latency">${latency}</div>
          </div>
          <div class="bars" style="--bar-count:${buckets}">${bars}</div>
        </article>
      `;
    })
    .join("");
}

async function fetchStatus() {
  const res = await fetch("/api/status");
  const data = await res.json();

  refs.lastCheck.textContent = data.last_check;
  refs.nextCheck.textContent = data.next_check;
  refs.runningState.textContent = data.running ? "检测中" : "空闲";
  refs.errorText.textContent = data.last_error || "无";

  refs.subscription_url.value = data.config.subscription_url || "";
  refs.interval_sec.value = data.config.interval_sec;
  refs.timeout_ms.value = data.config.timeout_ms;
  refs.concurrency.value = data.config.concurrency;
  refs.max_nodes.value = data.config.max_nodes;

  latestNodes = data.nodes || [];
  renderStats(data.summary);
  renderNodes(latestNodes);
}

async function saveConfig() {
  const body = {
    subscription_url: refs.subscription_url.value.trim(),
    interval_sec: Number(refs.interval_sec.value || 60),
    timeout_ms: Number(refs.timeout_ms.value || 5000),
    concurrency: Number(refs.concurrency.value || 10),
    max_nodes: Number(refs.max_nodes.value || 100),
  };
  await fetch("/api/config", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  await fetchStatus();
}

async function runOnce() {
  await fetch("/api/run-once", { method: "POST" });
  setTimeout(fetchStatus, 500);
}

refs.saveConfigBtn.addEventListener("click", saveConfig);
refs.runOnceBtn.addEventListener("click", runOnce);
refs.refreshBtn.addEventListener("click", fetchStatus);
refs.nodeFilterInput.addEventListener("input", () => {
  saveFilter();
  renderNodes(latestNodes);
});
refs.clearFilterBtn.addEventListener("click", () => {
  refs.nodeFilterInput.value = "";
  saveFilter();
  renderNodes(latestNodes);
});
refs.themeSelect.addEventListener("change", () => {
  applyTheme(refs.themeSelect.value);
});
refs.toggleConfigBtn.addEventListener("click", () => {
  const collapsed = refs.configPanel.classList.contains("collapsed");
  applyConfigCollapsed(!collapsed);
});
window.addEventListener("resize", () => {
  renderNodes(latestNodes);
});
refs.nodeList.addEventListener("mousemove", (e) => {
  const bar = e.target.closest(".bar");
  if (!bar || !refs.nodeList.contains(bar)) {
    return;
  }
  refs.nodeList.querySelectorAll(".bar.active").forEach((x) => x.classList.remove("active"));
  bar.classList.add("active");
  showBarTooltip(bar, e.clientX, e.clientY);
});
refs.nodeList.addEventListener("mouseleave", () => {
  refs.nodeList.querySelectorAll(".bar.active").forEach((x) => x.classList.remove("active"));
  hideBarTooltip();
});
refs.nodeList.addEventListener("mouseout", (e) => {
  const to = e.relatedTarget;
  if (to && refs.nodeList.contains(to)) return;
  refs.nodeList.querySelectorAll(".bar.active").forEach((x) => x.classList.remove("active"));
  hideBarTooltip();
});

applyTheme(localStorage.getItem(THEME_STORAGE_KEY) || "dark");
applyConfigCollapsed(localStorage.getItem(CONFIG_COLLAPSED_KEY) === "1");
loadFilter();
fetchStatus();
setInterval(fetchStatus, 5000);
