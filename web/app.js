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

  async cacheStats() {
    const r = await fetch(url('/cache'));
    if (!r.ok) throw new Error(`cache ${r.status}`);
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
  node_verdict: '验证判定',
  ping: '心跳',
};

/** 事件的视觉分类，决定时间线条目左侧色条。 */
function eventTone(ev) {
  if (ev.kind === 'node_verdict') {
    return ev.verdict === 'pass' ? 'ok' : 'fail';
  }
  if (ev.kind === 'node_finished') {
    if (ev.status === 'success') return 'ok';
    if (ev.status === 'failed') return 'fail';
    if (ev.status === 'backtracked') return 'warn';
    if (ev.status === 'skipped') return 'muted';
    return 'running';
  }
  if (ev.kind === 'task_finished') return ev.succeeded ? 'ok' : 'fail';
  if (ev.kind === 'task_cancelled') return 'fail';
  if (ev.kind === 'task_started' || ev.kind === 'node_started') return 'running';
  return 'muted';
}

/** 时间线条目的标题：节点事件优先显示节点 id + 角色。 */
function eventTitle(ev) {
  const label = EVENT_LABELS[ev.kind] || ev.kind;
  if (ev.node_id) return `${label} · ${ev.node_id}${ev.agent_type ? ` (${ev.agent_type})` : ''}`;
  return label;
}

function timelineItem(ev) {
  const tone = eventTone(ev);
  // 已经体现在标题里的字段不再重复展示
  const skip = new Set(['kind', 'task_id', 'timestamp', 'node_id', 'agent_type', 'verdict']);
  const body = Object.entries(ev)
    .filter(([k, v]) => !skip.has(k) && v !== '' && v != null && !(Array.isArray(v) && !v.length))
    .map(([k, v]) => {
      let val;
      if (Array.isArray(v)) {
        val = v.join('；');
      } else if (typeof v === 'object') {
        val = JSON.stringify(v);
      } else {
        val = v;
      }
      return `<span class="ev-field"><b>${esc(k)}</b> ${esc(String(val).slice(0, 200))}</span>`;
    })
    .join('');
  return `<li class="ev tone-${tone}">
      <span class="ev-time">${fmtTime(ev.timestamp)}</span>
      <span class="ev-title">${esc(eventTitle(ev))}</span>
      ${body ? `<div class="ev-body">${body}</div>` : ''}
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

/**
 * 上下文工程看板 —— 本项目最有展示价值的一块。
 *
 * 数据来自 `GET /tasks/{id}/context`，结构为：
 *   { task_id, metrics: { tokens_saved, chunks_dropped,
 *                         compression_ratio: {count, sum, mean, min, max},
 *                         utilization:       {count, sum, mean, min, max, p50, p90} } }
 * 这些数值是**进程级累计**（非单任务切片），因此文案上标注清楚，
 * 避免读者误以为只统计了当前任务。
 */
function renderContextMetrics(payload) {
  const container = $('context-metrics');
  const m = payload?.metrics || {};
  const saved = Math.round(m.tokens_saved ?? 0);
  const dropped = Math.round(m.chunks_dropped ?? 0);
  const ratio = m.compression_ratio || {};
  const util = m.utilization || {};

  const rows = [
    metricRow('上下文节省 token（累计）', saved.toLocaleString('zh-CN'), null),
    metricRow('装配淘汰片段数（累计）', String(dropped), null),
  ];

  if (ratio.count) {
    const mean = ratio.mean ?? 0;
    const pct = (1 - mean) * 100;   // 压缩比 <1 表示省下来了
    rows.push(metricRow(
      '压缩比 = 压缩后/压缩前',
      `${mean.toFixed(3)}  （省 ${pct.toFixed(1)}%，${ratio.count | 0} 次）`,
      pct / 100,
    ));
  }

  if (util.count) {
    rows.push(metricRow(
      '预算利用率',
      `${(util.mean ?? 0).toFixed(1)}%   p90 ${(util.p90 ?? 0).toFixed(1)}%   n=${util.count | 0}`,
      (util.mean ?? 0) / 100,
    ));
  }

  const hint = saved === 0 && dropped === 0 && !ratio.count && !util.count
    ? '<p class="empty">暂无上下文记录：可能未启用指标采集（启动时加 <code>--trace</code> 或设 '
      + '<code>DEVAGENT_OBSERVABILITY__METRICS_ENABLED=true</code>），或本任务输入未超压缩阈值。</p>'
    : '<p class="hint">数值为服务进程内的累计值，非单个任务切片。</p>';

  container.innerHTML = rows.join('') + hint;
}

/**
 * 模型缓存看板。
 *
 * 数据来自 `GET /cache`，两种模式结构不同：
 *   exact : { mode:'exact', hits, misses, size }
 *   vector: { mode:'vector', exact_hits, semantic_hits, misses,
 *             embed_failures, hits, hit_rate, semantic_share, size }
 *
 * 关键指标是 **semantic_share**（语义命中占全部命中的比例）：
 * 它直接回答「向量检索到底贡献了多少」。接近 0 就说明这次升级
 * 只带来了嵌入调用的成本而没有收益 —— 这正是需要被看见的事实，
 * 而不是一个漂亮的 hit_rate 就能掩盖的。
 */
function renderCacheStats(stats) {
  const container = $('cache-stats');
  if (!container) return;

  if (!stats || stats.mode === 'disabled') {
    container.innerHTML = '<p class="empty">缓存未启用或无统计数据。</p>';
    return;
  }

  const rows = [];
  const pct = (v) => `${((v ?? 0) * 100).toFixed(1)}%`;

  if (stats.mode === 'vector') {
    rows.push(metricRow('模式', '向量检索（exact + semantic）', null));
    rows.push(metricRow('精确命中', String(stats.exact_hits ?? 0), null));
    rows.push(metricRow('语义命中', String(stats.semantic_hits ?? 0), null));
    rows.push(metricRow('总命中率', pct(stats.hit_rate), stats.hit_rate ?? 0));
    rows.push(metricRow(
      '语义命中占比',
      `${pct(stats.semantic_share)}（升级带来的增量）`,
      stats.semantic_share ?? 0,
    ));
    if (stats.embed_failures) {
      rows.push(metricRow(
        '嵌入失败',
        `${stats.embed_failures} 次（已降级为精确匹配）`,
        null,
      ));
    }
  } else {
    rows.push(metricRow('模式', '精确匹配', null));
    rows.push(metricRow('命中', String(stats.hits ?? 0), null));
  }

  rows.push(metricRow('缓存条目数', String(stats.size ?? 0), null));

  const hints = [];
  if (stats.mode === 'exact') {
    hints.push('当前是精确匹配：措辞略有不同即视为未命中。'
      + '开启语义检索见 <code>DEVAGENT_CACHE__SEMANTIC=true</code>。');
  }
  if (stats.mode === 'vector' && (stats.semantic_hits ?? 0) === 0 && (stats.hits ?? 0) > 0) {
    hints.push('语义命中为 0 —— 说明请求之间差异较大，'
      + '此时开启向量检索只增加了嵌入成本而没有收益，可考虑关闭。');
  }
  if (stats.embed_failures) {
    hints.push('嵌入调用失败会让缓存静默降级：功能不受影响，但收益归零。'
      + '请检查嵌入模型的 API Key 与可用性。');
  }
  if (hints.length) {
    container.innerHTML = rows.join('')
      + hints.map((h) => `<p class="hint">${h}</p>`).join('');
    return;
  }
  container.innerHTML = rows.join('') + '<p class="hint">数值为服务进程内的累计值。</p>';
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
    if (kind === 'task_finished' || kind === 'task_cancelled') {
      es.close();
      state.stream = null;
      refreshTask(taskId);
    }
  };

  ['task_started', 'task_finished', 'task_cancelled', 'node_started', 'node_finished', 'node_verdict', 'ping']
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

  const cache = await api.cacheStats().catch(() => null);
  if (cache) renderCacheStats(cache);
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
  const cache = await api.cacheStats().catch(() => null);
  if (cache) renderCacheStats(cache);
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
    const providers = Array.isArray(h.providers) ? h.providers : [];
    const names = providers.length ? providers.join('/') : '未配置（仅离线冒烟）';
    renderHealth(true, `${names} · 指标=${h.observability_enabled ? '开' : '关'}`);
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
