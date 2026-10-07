/**
 * DevAgent 控制台前端逻辑。
 *
 * 设计取舍：**零构建 + 原生 ES 模块**。
 * 理由：这是一个「演示与面试展示」型界面，需要 clone 后直接打开就能跑。
 * 引入 React + Vite 会带来 node_modules 依赖，而核心价值（展示多 Agent
 * 协作与上下文工程的效果）并不需要组件框架。
 *
 * 数据结构一目了然：一个 API 客户端 + 一个 SSE 订阅 + 三块渲染函数。
 */

const API_PREFIX = '/api/v1';

/** 运行时 API 基址：默认同源（便于用 `python -m http.server` 直接预览）。 */
let apiBase = '';

function url(path) {
  return `${apiBase}${API_PREFIX}${path}`;
}

function $(id) {
  return document.getElementById(id);
}

function fmtTime(ts) {
  const d = ts ? new Date(ts * 1000) : new Date();
  return d.toLocaleTimeString('zh-CN', { hour12: false });
}

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  })[c]);
}

/* ------------------------------------------------------------------ *
 * API 客户端
 * ------------------------------------------------------------------ */

const api = {
  async health() {
    const r = await fetch(url('/health'));
    if (!r.ok) throw new Error(`health ${r.status}`);
    return r.json();
  },

  async createTask(goal) {
    const r = await fetch(url('/tasks'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ goal }),
    });
    if (!r.ok) {
      const body = await r.json().catch(() => ({}));
      throw new Error(body.detail || `创建任务失败 (${r.status})`);
    }
    return r.json();
  },

  async listTasks() {
    const r = await fetch(url('/tasks?limit=50'));
    if (!r.ok) throw new Error(`list ${r.status}`);
    return r.json();
  },

  async getContext(taskId) {
    const r = await fetch(url(`/tasks/${taskId}/context`));
    if (!r.ok) throw new Error(`context ${r.status}`);
    return r.json();
  },

  async metrics() {
    const r = await fetch(url('/metrics'));
    if (!r.ok) throw new Error(`metrics ${r.status}`);
    return r.json();
  },
};

/* ------------------------------------------------------------------ *
 * 状态
 * ------------------------------------------------------------------ */

const state = {
  activeTaskId: '',
  stream: null,
  tasks: [],
};

/* ------------------------------------------------------------------ *
 * 渲染
 * ------------------------------------------------------------------ */

function renderHealth(ok, detail) {
  const badge = $('health-badge');
  badge.className = `badge ${ok ? 'badge-ok' : 'badge-fail'}`;
  badge.textContent = ok ? `已连接 · ${detail}` : `未连接 · ${detail}`;
}

function renderTaskList() {
  const el = $('task-list');
  if (!state.tasks.length) {
    el.innerHTML = '<li class="task-item"><div class="goal" style="color:var(--text-dim)">暂无任务</div></li>';
    return;
  }
  el.innerHTML = state.tasks
    .map((t) => `
      <li class="task-item ${t.task_id === state.activeTaskId ? 'active' : ''}" data-id="${esc(t.task_id)}">
        <div class="goal">${esc(t.goal.slice(0, 60))}${t.goal.length > 60 ? '…' : ''}</div>
        <div class="task-meta">${esc(t.status)} · ${t.duration_ms}ms · ${t.total_tokens} tok</div>
      </li>`)
    .join('');
  el.querySelectorAll('.task-item[data-id]').forEach((li) => {
    li.addEventListener('click', () => selectTask(li.dataset.id));
  });
}

const EVENT_LABELS = {
  task_started: '任务开始',
  task_finished: '任务结束',
  task_cancelled: '任务取消',
  node_started: '节点开始',
  node_finished: '节点完成',
  ping: '心跳',
};

function timelineItem(ev) {
  const label = EVENT_LABELS[ev.kind] || ev.kind;
  const body = Object.entries(ev)
    .filter(([k]) => !['kind', 'task_id', 'timestamp'].includes(k))
    .map(([k, v]) => {
      const val = typeof v === 'object' ? JSON.stringify(v) : v;
      return `${k}: ${esc(String(val).slice(0, 160))}`;
    })
    .join(' · ');
  return `<li class="ev-${esc(ev.kind)}">
      <span class="ev-time">${fmtTime(ev.timestamp)}</span>
      <span class="ev-kind">${esc(label)}</span>
      <div class="ev-body">${body}</div>
    </li>`;
}

function appendTimeline(ev) {
  const list = $('timeline');
  const empty = list.querySelector('.empty');
  if (empty) empty.remove();
  list.insertAdjacentHTML('beforeend', timelineItem(ev));
  list.scrollTop = list.scrollHeight;
}

function resetTimeline() {
  $('timeline').innerHTML = '<li class="empty">等待事件…</li>';
  $('summary').classList.add('hidden');
}

function renderSummary(data) {
  const el = $('summary');
  el.classList.remove('hidden');
  const stats = [
    ['状态', data.status],
    ['成功', data.succeeded ? '是' : '否'],
    ['耗时', `${data.duration_ms} ms`],
    ['token', data.total_tokens],
    ['成本', `$${(data.total_cost_usd ?? 0).toFixed(4)}`],
  ];
  el.innerHTML = `
    <div class="summary-grid">
      ${stats.map(([k, v]) => `
        <div>
          <div class="stat-label">${esc(k)}</div>
          <div class="stat-value">${esc(v)}</div>
        </div>`).join('')}
    </div>
    ${data.error ? `<div class="ev-body" style="color:var(--fail);margin-top:8px">${esc(data.error)}</div>` : ''}`;
}

/** 上下文工程看板 —— 本项目最有展示价值的一块。 */
function renderContextMetrics(payload) {
  const container = $('context-metrics');
  const m = payload?.metrics || {};
  const saved = m.tokens_saved ?? 0;
  const dropped = m.chunks_dropped ?? 0;
  const ratio = m.compression_ratio || {};
  const util = m.utilization || {};

  const rows = [];

  if (saved > 0) {
    rows.push(metricRow('上下文节省 token', String(saved), null));
  }
  if (dropped > 0) {
    rows.push(metricRow('装配淘汰片段数', String(dropped), null));
  }
  if (ratio.count) {
    rows.push(metricRow(
      '压缩比（压缩后/压缩前）',
      `${(ratio.mean ?? 0).toFixed(3)}  (${ratio.count} 次)`,
      ratio.mean ?? 0,
    ));
  }
  if (util.count) {
    rows.push(metricRow(
      '预算利用率',
      `${(util.mean ?? 0).toFixed(1)}%  (p90 ${(util.p90 ?? 0).toFixed(1)}%)`,
      (util.mean ?? 0) / 100,
    ));
  }

  if (!rows.length) {
    container.innerHTML = '<p class="empty">当前任务暂无上下文压缩记录（输入未超阈值或未启用指标采集）</p>';
    return;
  }
  container.innerHTML = rows.join('');
}

function metricRow(name, value, ratio) {
  const bar = ratio == null
    ? ''
    : `<div class="bar"><div class="bar-fill" style="width:${Math.min(100, Math.max(0, ratio * 100))}%"></div></div>`;
  return `<div class="metric-row">
      <div class="metric-head"><span class="metric-name">${esc(name)}</span><span class="metric-val">${esc(value)}</span></div>
      ${bar}
    </div>`;
}

/* ------------------------------------------------------------------ *
 * SSE 订阅
 * ------------------------------------------------------------------ */

function subscribe(taskId) {
  if (state.stream) {
    state.stream.close();
    state.stream = null;
  }
  resetTimeline();

  const es = new EventSource(url(`/tasks/${taskId}/events`));
  state.stream = es;

  // SSE 的具名事件需要逐个 addEventListener；用一个统一处理函数减少重复。
  const handle = (kind) => (e) => {
    let payload = {};
    try {
      payload = JSON.parse(e.data);
    } catch {
      payload = { raw: e.data };
    }
    if (kind !== 'ping') appendTimeline({ kind, ...payload });
    if (kind === 'task_finished') {
      es.close();
      state.stream = null;
      refreshTask(taskId);
    }
  };

  ['task_started', 'task_finished', 'task_cancelled', 'node_started', 'node_finished', 'ping']
    .forEach((kind) => es.addEventListener(kind, handle(kind)));

  es.onerror = () => {
    // EventSource 会自动重连；若任务已结束则服务端会关闭连接，
    // 此时主动关闭避免无限重连打日志。
    if (es.readyState === EventSource.CLOSED) {
      state.stream = null;
    }
  };
}

async function refreshTask(taskId) {
  const list = await api.listTasks().catch(() => null);
  if (list) {
    state.tasks = list.items;
    renderTaskList();
  }
  const detail = await fetch(url(`/tasks/${taskId}`)).then((r) => r.json()).catch(() => null);
  if (detail) renderSummary(detail);

  const ctx = await api.getContext(taskId).catch(() => null);
  if (ctx) renderContextMetrics(ctx);

  const m = await api.metrics().catch(() => null);
  if (m) $('raw-metrics').textContent = JSON.stringify(m, null, 2);
}

async function selectTask(taskId) {
  state.activeTaskId = taskId;
  renderTaskList();
  subscribe(taskId);
  await refreshTask(taskId);
}

/* ------------------------------------------------------------------ *
 * 初始化
 * ------------------------------------------------------------------ */

async function refreshMetrics() {
  const m = await api.metrics().catch(() => null);
  if (m) $('raw-metrics').textContent = JSON.stringify(m, null, 2);
  if (state.activeTaskId) {
    const ctx = await api.getContext(state.activeTaskId).catch(() => null);
    if (ctx) renderContextMetrics(ctx);
  }
}

async function bootstrap() {
  // API 基址：优先用 URL 参数，便于「前端静态托管 + 后端另开端口」的开发模式
  const params = new URLSearchParams(location.search);
  apiBase = params.get('api') || '';
  $('api-base').value = apiBase;

  $('api-base').addEventListener('change', (e) => {
    apiBase = e.target.value.trim().replace(/\/$/, '');
    location.search = apiBase ? `?api=${encodeURIComponent(apiBase)}` : '';
  });

  try {
    const h = await api.health();
    renderHealth(true, `${h.providers.length} 个提供商 · obs=${h.observability_enabled}`);
  } catch (err) {
    renderHealth(false, String(err.message || err));
  }

  $('task-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const goal = $('goal').value.trim();
    if (!goal) return;
    const btn = e.target.querySelector('button[type=submit]');
    btn.disabled = true;
    try {
      const task = await api.createTask(goal);
      state.tasks.unshift({
        task_id: task.task_id, goal: task.goal, status: task.status,
        duration_ms: 0, total_tokens: 0,
      });
      await selectTask(task.task_id);
    } catch (err) {
      alert(String(err.message || err));
    } finally {
      btn.disabled = false;
    }
  });

  $('refresh-context').addEventListener('click', refreshMetrics);

  const list = await api.listTasks().catch(() => null);
  if (list) {
    state.tasks = list.items;
    renderTaskList();
    if (state.tasks.length) await selectTask(state.tasks[0].task_id);
  }

  refreshMetrics();
  // 指标轮询 10s：SSE 负责过程事件，指标需要一个轻量兜底刷新
  setInterval(refreshMetrics, 10_000);
}

bootstrap();
