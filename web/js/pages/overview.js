/**
 * 总览页（Dashboard）。
 *
 * 定位：**30 秒内回答「系统现在怎么样、值不值得信任」**。
 * 因此只放四类信息：健康状态、任务吞吐、上下文工程收益、成本。
 * 深度分析交给专门页面 —— 总览页堆图表是最常见的产品失误。
 */

import { api } from '../api.js';
import {
  alert,
  badge,
  button,
  card,
  emptyState,
  errorState,
  metricRow,
  skeletonStat,
  statCard,
  statusBadge,
  statusDotClass,
  toast,
} from '../components.js';
import { icon } from '../icons.js';
import { getLoad, getState, navigate, setLoad, subscribe } from '../store.js';
import {
  $, clear, el, fmtCompact, fmtCost, fmtDuration, fmtInt, fmtPct,
  fmtRelative, fromHTML, histMaxQuantile, histOverallMean, metric, mount, sumLabels,
} from '../util.js';

const LOAD_KEY = 'overview';

/**
 * 页面入口。
 * @param {HTMLElement} root
 * @param {{navigate: Function}} ctx
 * @returns {() => void} 清理函数
 */
export default async function renderOverview(root, ctx) {
  const container = el('div');
  root.append(container);

  const disposers = [];

  // 数据依赖：任务列表 + 指标 + 缓存
  async function loadAll({ silent = false } = {}) {
    if (!silent) {
      setLoad(LOAD_KEY, 'loading');
      paint();
    }
    try {
      const tasks = await api.listTasks({ limit: 200 }).catch((e) => {
        throw e;
      });
      const [metrics, cache] = await Promise.all([
        api.metrics().catch(() => null),
        api.cacheStats().catch(() => null),
      ]);
      const state = getState();
      const patch = {
        tasks: Array.isArray(tasks?.items) ? tasks.items : [],
      };
      // 只在拿到有效数据时覆盖，避免一次失败清空已有内容
      if (metrics) patch.metrics = metrics;
      if (cache) patch.cacheStats = cache;
      const { set } = await import('../store.js');
      set(patch);
      void state;
      setLoad(LOAD_KEY, 'ready');
    } catch (err) {
      setLoad(LOAD_KEY, 'error', err?.message || String(err));
    }
    paint();
  }

  function paint() {
    const status = getLoad(LOAD_KEY);
    clear(container);

    if (status === 'loading') {
      mount(container, skeletonLayout());
      return;
    }
    if (status === 'error') {
      mount(
        container,
        card({
          body: errorState({
            title: '总览数据加载失败',
            message: getState('error')?.[LOAD_KEY] || '',
            onRetry: () => loadAll(),
          }),
        })
      );
      return;
    }

    mount(container, ...buildSections(ctx));
  }

  // 任务列表变化时重绘（提交任务后返回总览应立即反映）
  disposers.push(subscribe('tasks', () => {
    if (getLoad(LOAD_KEY) === 'ready') paint();
  }));

  await loadAll();

  // 清理：取消订阅
  return () => disposers.forEach((d) => d());
}

/* ------------------------------------------------------------------ *
 * 骨架
 * ------------------------------------------------------------------ */

function skeletonLayout() {
  const wrap = el('div');
  const grid = el('div', { class: 'grid grid-cols-4', style: { 'margin-bottom': 'var(--space-5)' } });
  for (let i = 0; i < 4; i += 1) grid.append(skeletonStat());
  wrap.append(grid);
  wrap.append(el('div', { class: 'skeleton skeleton-block', style: { height: '240px' } }));
  return wrap;
}

/* ------------------------------------------------------------------ *
 * 各区块
 * ------------------------------------------------------------------ */

function buildSections(ctx) {
  const tasks = getState('tasks') || [];
  const metrics = getState('metrics');
  const cache = getState('cacheStats');
  const connected = getState('connected');
  const providers = getState('providers') || [];
  const observability = getState('observability');

  const sections = [];

  // 顶部告警：把「不健康」的信号顶到最前面，而不是埋在指标里
  const warnings = collectWarnings({ connected, providers, observability, cache });
  if (warnings.length) {
    sections.push(el('div', { class: 'section', style: { display: 'flex', 'flex-direction': 'column', gap: 'var(--space-2)' } },
      warnings));
  }

  sections.push(renderKpiRow(tasks, metrics));
  sections.push(buildMainGrid(tasks, metrics, cache, ctx));

  return sections;
}

/** 收集需要用户立即知晓的问题。 */
function collectWarnings({ connected, providers, observability, cache }) {
  const out = [];

  if (!connected) {
    out.push(
      alert({
        tone: 'danger',
        title: '后端未连接，数据不可用',
        body: el('div', {}, [
          el('div', {
            html: '界面处于离线状态，所有指标为空。请启动后端服务后点击侧栏底部的连接状态重新探测。',
          }),
        ]),
      })
    );
  } else if (!providers.length) {
    out.push(
      alert({
        tone: 'warning',
        title: '未配置模型提供商',
        body: '后端已连通，但没有可用的模型 Key。提交的任务会在「需求澄清」阶段失败。请配置 .env 中的 DEVAGENT_MODELS__* 后重启服务。',
      })
    );
  }

  if (connected && !observability) {
    out.push(
      alert({
        tone: 'info',
        title: '可观测性未开启，上下文工程指标将为空',
        body: '启动时加 --trace，或设置 DEVAGENT_OBSERVABILITY__METRICS_ENABLED=true，才能采集 token 节省、压缩比与预算利用率。',
      })
    );
  }

  // 语义缓存「开着但零收益」是一个真实存在的成本陷阱，值得单独提示
  if (cache && cache.mode === 'vector' && (cache.semantic_hits ?? 0) === 0 && (cache.hits ?? 0) > 0) {
    out.push(
      alert({
        tone: 'warning',
        title: '语义缓存命中为 0，正在白白支付嵌入成本',
        body: '语义命中为 0 说明请求之间差异较大，启用向量检索只增加嵌入调用成本而没有带来增量收益，建议关闭 DEVAGENT_CACHE__SEMANTIC。',
      })
    );
  }

  return out;
}

/* ------------------------------------------------------------------ *
 * KPI 行
 * ------------------------------------------------------------------ */

/**
 * 「已结束」的任务状态 —— 必须与后端 `TaskStatus` 一致，且**只列终态**。
 *
 * 这里曾经写作 `t.status === 'success' || t.status === 'failed'`：
 * `success` 是 **StepStatus** 的成员，任务级的对应值是 `succeeded`
 * （`devagent.enums.TaskStatus.SUCCEEDED = "succeeded"`）。
 * 后果是 `finished` 只数到失败任务 —— 全部成功时成功率显示「—」，
 * 9 成功 + 1 失败时算出 900%。这是一个"看起来只是没数据"的错误数字，
 * 比空白更危险。
 *
 * 因此本集合由跨语言契约测试守着（`test_contract_words.json` 的
 * `task_status`），任何人再写错字面量都会被测试拦下。
 */
export const FINISHED_TASK_STATUSES = Object.freeze([
  'succeeded', // TaskStatus.SUCCEEDED —— 任务成功
  'failed', // TaskStatus.FAILED
  'cancelled', // TaskStatus.CANCELLED —— 用户主动取消，也是终态
  'paused', // TaskStatus.PAUSED —— 预算耗尽后停住，不再推进
]);

/** 仍在推进、尚未产生终态的任务状态。 */
export const ACTIVE_TASK_STATUSES = Object.freeze(['pending', 'running']);

export const isFinishedTask = (t) => FINISHED_TASK_STATUSES.includes(t?.status);
export const isActiveTask = (t) => ACTIVE_TASK_STATUSES.includes(t?.status);

/**
 * 任务列表 → KPI 数字（**纯函数**，无 DOM，可直接单测）。
 *
 * 成功率只在「已结束」的任务上计算 —— 把运行中算进去会低估成功率；
 * 而分母同样只取已结束，因此结果天然落在 [0, 1]，不可能出现 900%。
 *
 * @param {Array<object>} tasks
 * @returns {{total:number, succeeded:number, finished:number, running:number,
 *   successRate:number|null, totalTokens:number, avgDuration:number}}
 *   `successRate` 为 `null` 表示「暂无已结束任务」，界面据此显示「—」
 */
export function computeTaskKpis(tasks) {
  const list = Array.isArray(tasks) ? tasks : [];
  const total = list.length;
  const succeeded = list.filter((t) => t?.status === 'succeeded').length;
  const finished = list.filter(isFinishedTask).length;
  const running = list.filter(isActiveTask).length;

  return {
    total,
    succeeded,
    finished,
    running,
    successRate: finished ? succeeded / finished : null,
    totalTokens: list.reduce((sum, t) => sum + (Number(t?.total_tokens) || 0), 0),
    avgDuration: total
      ? list.reduce((s, t) => s + (Number(t?.duration_ms) || 0), 0) / total
      : 0,
  };
}

/**
 * 渲染 KPI 行。
 *
 * 之所以导出（而不是留在 buildKpiRow 里）：`buildKpiRow` 依赖 `getState()`
 * 读全局 store，在 node 里跑不了；把「取数 → 造卡片」的重活放在这个只吃参数的
 * 版本里，回归测试才能直接断言「全部成功 → 100%，且永不超过 100%」。
 *
 * @param {Array<object>} tasks
 * @param {object|null} metrics 全局指标快照（可能为 null）
 * @returns {HTMLElement}
 */
export function renderKpiRow(tasks, metrics) {
  const k = computeTaskKpis(tasks);
  const { total, succeeded, finished, running, successRate } = k;

  // 成本从指标里取（任务列表项不含成本字段）
  const totalCost = sumLabels(metric(metrics?.counters, 'llm_cost_usd'));

  const row = el('div', {
    class: 'grid grid-cols-4',
    style: { 'margin-bottom': 'var(--space-5)' },
  });

  row.append(
    statCard({
      label: '任务总数',
      value: fmtInt(total),
      icon: 'git-branch',
      foot: running
        ? el('span', { class: 'trend-flat' }, [
            fromHTML(icon('loader', 12)),
            `${running} 个进行中`,
          ])
        : total
          ? '全部已结束'
          : '尚无任务',
    }),
    statCard({
      label: '任务成功率',
      value: successRate == null ? '—' : fmtPct(successRate, 0),
      icon: 'target',
      tone: successRate == null ? undefined : successRate >= 0.8 ? 'success' : successRate >= 0.5 ? 'warning' : 'danger',
      foot: successRate == null
        ? '暂无已结束任务'
        : `${succeeded} 成功 / ${finished} 已结束`,
    }),
    statCard({
      label: '平均耗时',
      value: total ? fmtDuration(k.avgDuration).split(' ')[0] : '—',
      unit: total ? fmtDuration(k.avgDuration).split(' ')[1] : '',
      icon: 'clock',
      foot: total ? '基于全部任务' : '——',
    }),
    statCard({
      label: '累计 token',
      value: fmtCompact(k.totalTokens),
      icon: 'hash',
      foot: totalCost ? `模型成本 ${fmtCost(totalCost)}` : '开启可观测性可看成本',
    })
  );

  return row;
}

/* ------------------------------------------------------------------ *
 * 主区网格
 * ------------------------------------------------------------------ */

function buildMainGrid(tasks, metrics, cache, ctx) {
  const grid = el('div', { class: 'grid', style: { 'grid-template-columns': 'minmax(0, 1.6fr) minmax(0, 1fr)', gap: 'var(--space-4)', 'align-items': 'start' } });

  grid.append(buildRecentTasks(tasks, ctx));
  grid.append(el('div', { style: { display: 'flex', 'flex-direction': 'column', gap: 'var(--space-4)' } }, [
    buildContextValue(metrics),
    buildCacheCard(cache),
  ]));

  return grid;
}

function buildRecentTasks(tasks, ctx) {
  const body = el('div');

  if (!tasks.length) {
    body.append(
      emptyState({
        icon: 'inbox',
        title: '还没有任务',
        desc: '提交第一个需求，观察多 Agent 如何协作完成它',
        action: button('去提交需求', {
          icon: 'plus',
          variant: 'primary',
          onClick: () => navigate('workbench'),
        }),
      })
    );
  } else {
    const recent = tasks.slice(0, 8);
    const wrap = el('div', { class: 'table-wrap table-wrap-stack' });
    const table = el('table', { class: 'table' });
    table.append(
      el('thead', {}, [
        el('tr', {}, [
          el('th', { text: '需求' }),
          el('th', { text: '状态' }),
          el('th', { class: 'num', text: '耗时' }),
          el('th', { class: 'num', text: 'token' }),
        ]),
      ])
    );
    const tbody = el('tbody');
    for (const t of recent) {
      const tr = el('tr', {
        dataset: { clickable: '1', id: t.task_id },
        attrs: {
          tabindex: '0',
          role: 'link',
          'aria-label': `查看任务：${t.goal}`,
        },
        on: {
          click: () => openTask(t.task_id, ctx),
          keydown: (e) => {
            if (e.key === 'Enter' || e.key === ' ') {
              e.preventDefault();
              openTask(t.task_id, ctx);
            }
          },
        },
      });
      tr.append(
        el('td', { class: 'primary', dataset: { label: '需求' } }, [
          el('div', { class: 'truncate', text: t.goal, style: { 'max-width': '42ch' }, attrs: { title: t.goal } }),
        ]),
        el('td', { dataset: { label: '状态' } }, [statusBadge(t.status)]),
        el('td', { class: 'num', dataset: { label: '耗时' }, text: fmtDuration(t.duration_ms) }),
        el('td', { class: 'num', dataset: { label: 'token' }, text: fmtInt(t.total_tokens) })
      );
      tbody.append(tr);
    }
    table.append(tbody);
    wrap.append(table);
    body.append(wrap);
  }

  return card({
    title: '最近任务',
    subtitle: tasks.length ? `共 ${tasks.length} 个` : '',
    actions: tasks.length
      ? button('全部任务', {
          icon: 'arrow-right',
          variant: 'ghost',
          small: true,
          onClick: () => navigate('workbench'),
        })
      : null,
    body,
    flush: true,
  });
}

/** 跳转到工作台并选中该任务。 */
function openTask(taskId, ctx) {
  navigate('workbench', { task: taskId });
  void ctx;
}

/* ------------------------------------------------------------------ *
 * 上下文工程价值卡
 * ------------------------------------------------------------------ */

function buildContextValue(metrics) {
  const body = el('div');

  if (!metrics) {
    body.append(
      emptyState({
        small: true,
        icon: 'layers',
        title: '暂无指标',
        desc: '需要开启可观测性才能采集上下文工程数据',
      })
    );
  } else {
    const counters = metrics.counters;
    const histograms = metrics.histograms;

    const ratioHist = metric(histograms, 'context_compression_ratio');
    const utilHist = metric(histograms, 'context_utilization');
    const ratioMean = histOverallMean(ratioHist);
    const utilMean = histOverallMean(utilHist);
    const utilP90 = histMaxQuantile(utilHist, 'p90');

    const tokenMap = metric(counters, 'llm_tokens') || {};
    const totalTokens = sumLabels(tokenMap);
    const inputTokens = Object.entries(tokenMap)
      .filter(([k]) => /direction="input"/.test(k))
      .reduce((s, [, v]) => s + (Number(v) || 0), 0);

    const blocked = sumLabels(metric(counters, 'hallucination_blocked'));
    const backtracks = sumLabels(metric(counters, 'backtracks'));

    const list = el('div', { class: 'metric-list' });
    let hasData = false;

    if (totalTokens) {
      hasData = true;
      list.append(
        metricRow({ name: '累计 token 消耗', value: fmtCompact(totalTokens) }),
        metricRow({
          name: '输入占比（决定上下文成本的部分）',
          value: fmtPct(inputTokens / totalTokens),
          ratio: inputTokens / totalTokens,
        })
      );
    }

    if (ratioMean != null) {
      hasData = true;
      const savedPct = 1 - ratioMean;
      list.append(
        metricRow({
          name: '平均压缩比',
          value: `${ratioMean.toFixed(3)}（省 ${fmtPct(savedPct)}）`,
          ratio: Math.max(0, Math.min(1, savedPct)),
          tone: savedPct > 0.3 ? 'success' : '',
        })
      );
    }

    if (utilMean != null) {
      hasData = true;
      list.append(
        metricRow({
          name: `平均预算利用率${utilP90 != null ? `（最紧张角色 p90 ${fmtPct(utilP90, 0)}）` : ''}`,
          value: fmtPct(utilMean, 1),
          ratio: Math.min(1, utilMean),
          tone: utilMean > 0.9 ? 'warning' : '',
        })
      );
    }

    // 可靠性信号：这两个数字是「独立验证层真的在起作用」的证据
    if (blocked || backtracks) {
      hasData = true;
      list.append(
        metricRow({ name: '验证层拦截的幻觉输出', value: fmtInt(blocked) }),
        metricRow({ name: '回退重试次数', value: fmtInt(backtracks) })
      );
    }

    if (hasData) body.append(list);
    else {
      body.append(
        emptyState({
          small: true,
          icon: 'layers',
          title: '暂无上下文记录',
          desc: '可能未启用指标采集，或还没有产生超阈值的上下文压缩。',
        })
      );
    }
  }

  return card({
    title: '上下文工程收益',
    subtitle: '本项目核心竞争力所在',
    actions: button('详情', {
      icon: 'arrow-right',
      variant: 'ghost',
      small: true,
      onClick: () => navigate('context'),
    }),
    body,
  });
}

/* ------------------------------------------------------------------ *
 * 缓存卡
 * ------------------------------------------------------------------ */

function buildCacheCard(cache) {
  const body = el('div');

  if (!cache || cache.mode === 'disabled') {
    body.append(
      emptyState({
        small: true,
        icon: 'database',
        title: '缓存未启用',
        desc: '启用后可复用相同或语义相近的模型响应，降低成本与延迟',
      })
    );
  } else {
    const list = el('div', { class: 'metric-list' });
    const isVector = cache.mode === 'vector';

    list.append(
      metricRow({
        name: '模式',
        value: isVector ? '向量检索' : '精确匹配',
      }),
      metricRow({
        name: '缓存条目数',
        value: fmtInt(cache.size ?? 0),
      })
    );

    if (isVector) {
      list.append(
        metricRow({
          name: '总命中率',
          value: fmtPct(cache.hit_rate ?? 0),
          ratio: cache.hit_rate ?? 0,
        }),
        metricRow({
          name: '语义命中占比',
          value: fmtPct(cache.semantic_share ?? 0),
          ratio: cache.semantic_share ?? 0,
          tone: (cache.semantic_share ?? 0) === 0 ? 'warning' : 'success',
        })
      );
    } else {
      list.append(
        metricRow({
          name: '命中',
          value: fmtInt(cache.hits ?? 0),
        })
      );
    }

    if (cache.embed_failures) {
      body.append(
        el('div', { style: { 'margin-bottom': 'var(--space-3)' } }, [
          alert({
            tone: 'warning',
            title: `${cache.embed_failures} 次嵌入调用失败`,
            body: '缓存已静默降级为精确匹配：功能不受影响，但语义检索的收益归零。请检查嵌入模型的 Key 与可用性。',
          }),
        ])
      );
    }

    body.append(list);

    const foot = el('div', { class: 'hint-text', style: { 'margin-top': 'var(--space-3)' } });
    foot.append(el('span', { text: '数值为服务进程内累计，非单次请求切片。' }));
    body.append(foot);
  }

  return card({
    title: '模型响应缓存',
    actions: button('详情', {
      icon: 'arrow-right',
      variant: 'ghost',
      small: true,
      onClick: () => navigate('context'),
    }),
    body,
  });
}

export { toast, badge, statusDotClass };
