/**
 * 上下文工程看板。
 *
 * 这是本项目的**差异化页面**：把「上下文工程」从抽象概念变成可核查的数据。
 *
 * 四个视角：
 *   1. 收益概览 —— 省了多少 token、压缩比多少
 *   2. 五层能力 —— Routing / Isolation / Compression / Assembly / Budget 各自的实时指标
 *   3. 缓存效率 —— 精确命中 vs 语义命中的拆分（回答「向量检索值不值」）
 *   4. 装配决策明细 —— 选中与淘汰的片段及原因
 *
 * 数据源全部来自 `/metrics`、`/cache`、`/tasks/{id}/context`，
 * 不做任何估算或推断 —— 展示不可核查的数字比不展示更糟。
 */

import { api } from '../api.js?v=20261008-live';
import {
  alert,
  button,
  card,
  emptyState,
  errorState,
  metricRow,
  skeletonBlock,
  skeletonStat,
  statCard,
  toast,
} from '../components.js?v=20261008-live';
import { icon } from '../icons.js?v=20261008-live';
import { getState, navigate, set } from '../store.js?v=20261008-live';
import {
  clear, copyText, el, fmtCompact, fmtCost, fmtInt, fmtPct, fromHTML, histMaxQuantile,
  histOverallMean, metric, mount, sumLabels,
} from '../util.js?v=20261008-live';

/** 五层上下文能力 —— 与 docs/02 的设计一一对应。
 *  `metrics` 用真实的后端指标名（后缀匹配，兼容带/不带前缀两种形态）。 */
const LAYERS = [
  {
    id: 'routing',
    name: 'Routing 路由',
    icon: 'route',
    desc: '按任务复杂度分级选择模型档位 —— 简单任务不喂给大模型，这是最直接的省钱手段',
    counters: ['llm_calls'],
    histograms: ['llm_latency_ms'],
  },
  {
    id: 'isolation',
    name: 'Isolation 隔离',
    icon: 'shield',
    desc: '每个 Agent 只见自己该见的上下文，杜绝跨角色污染与幻觉传播',
    counters: ['node_executions', 'verdict_total'],
    histograms: [],
  },
  {
    id: 'compression',
    name: 'Compression 压缩',
    icon: 'package',
    desc: '超阈值时对历史片段做摘要压缩，保留语义、丢掉冗余',
    counters: [],
    histograms: ['context_compression_ratio'],
  },
  {
    id: 'assembly',
    name: 'Assembly 装配',
    icon: 'layers',
    desc: '加权打分排序 + 冗余惩罚贪心选择 + 位置编排（关键信息放首尾）',
    counters: ['llm_tokens', 'backtracks'],
    histograms: [],
  },
  {
    id: 'budget',
    name: 'Budget 预算',
    icon: 'gauge',
    desc: '按角色分配 token 预算，逼近上限时主动降级而不是硬撞上限',
    counters: [],
    histograms: ['context_utilization'],
  },
];

export default async function renderContext(root, ctx) {
  const container = el('div', { class: 'page-stack context-page' });
  root.append(container);
  let disposed = false;

  async function load() {
    mount(container, buildSkeleton());
    try {
      const [metrics, cache] = await Promise.all([
        api.metrics(),
        api.cacheStats().catch(() => null),
      ]);
      set({ metrics, cacheStats: cache });
      if (disposed) return;
      mount(container, ...buildSections(ctx));
    } catch (err) {
      if (disposed) return;
      mount(
        container,
        card({
          body: errorState({
            title: '上下文指标加载失败',
            message: err?.message || String(err),
            onRetry: () => load(),
          }),
        })
      );
    }
  }

  await load();

  return () => {
    disposed = true;
  };
}

/* ------------------------------------------------------------------ *
 * 骨架
 * ------------------------------------------------------------------ */

function buildSkeleton() {
  const wrap = el('div', { class: 'page-stack' });
  const grid = el('div', { class: 'grid grid-cols-4' });
  for (let i = 0; i < 4; i += 1) grid.append(skeletonStat());
  wrap.append(grid);
  wrap.append(
    el('div', { class: 'grid grid-cols-2' }, [
      skeletonBlock(220),
      skeletonBlock(220),
    ])
  );
  wrap.append(skeletonBlock(200));
  return wrap;
}

/* ------------------------------------------------------------------ *
 * 区块
 * ------------------------------------------------------------------ */

function buildSections(ctx) {
  const metrics = getState('metrics');
  const cache = getState('cacheStats');
  const observability = getState('observability');

  const sections = [buildHeaderActions()];

  if (getState('connected') && !observability) {
    sections.push(
      el('div', { class: 'section' }, [
        alert({
          tone: 'info',
          title: '指标采集未开启',
          body: '在设置 → 后端配置 → 可观测性中启用。',
        }),
      ])
    );
  }

  sections.push(buildKpiRow(metrics));
  sections.push(buildLayerGrid(metrics));
  sections.push(
    el('div', {
      class: 'grid grid-cols-2 context-detail-grid',
    }, [
      buildCompressionCard(metrics),
      buildCacheCard(cache),
    ])
  );
  sections.push(buildRawMetrics(metrics));

  void ctx;
  return sections;
}

function buildHeaderActions() {
  return el('div', { class: 'page-head' }, [
    el('div', { class: 'page-head-text' }, [
      el('h1', { text: '上下文工程看板' }),
    ]),
    el('div', { class: 'page-head-actions' }, [
      button('查看任务明细', {
        icon: 'git-branch',
        variant: 'secondary',
        onClick: () => navigate('workbench'),
      }),
    ]),
  ]);
}

/* ------------------------------------------------------------------ *
 * KPI
 * ------------------------------------------------------------------ */

function buildKpiRow(metrics) {
  const { counters, histograms } = metrics || {};

  // 真实指标名：llm_tokens / llm_cost_usd / context_compression_ratio / context_utilization
  const inputTokens = metric(counters, 'llm_tokens') || {};
  const totalIn = Object.entries(inputTokens)
    .filter(([k]) => /direction="input"/.test(k))
    .reduce((s, [, v]) => s + (Number(v) || 0), 0);
  const totalOut = Object.entries(inputTokens)
    .filter(([k]) => /direction="output"/.test(k))
    .reduce((s, [, v]) => s + (Number(v) || 0), 0);

  const cost = sumLabels(metric(counters, 'llm_cost_usd'));
  const ratioHist = metric(histograms, 'context_compression_ratio');
  const utilHist = metric(histograms, 'context_utilization');

  const ratioMean = histOverallMean(ratioHist);
  const utilMean = histOverallMean(utilHist);
  const utilP90 = histMaxQuantile(utilHist, 'p90');

  const backtracks = sumLabels(metric(counters, 'backtracks'));
  const blocked = sumLabels(metric(counters, 'hallucination_blocked'));

  const row = el('div', {
    class: 'grid grid-cols-4',
  });

  // 压缩比 = 压缩后/压缩前。<1 才是真的省下来了。
  // 均值恒为 1.0 说明从未触发压缩 —— 这是一个应当被说出来的事实，
  // 而不是用一个漂亮的「100%」掩盖过去。
  const neverCompressed = ratioMean != null && ratioMean >= 0.999;

  row.append(
    statCard({
      label: '平均压缩比',
      value: ratioMean == null ? '—' : ratioMean.toFixed(3),
      icon: 'package',
      tone: ratioMean == null ? undefined : neverCompressed ? 'warning' : 'success',
      foot: ratioMean == null
        ? '未触发压缩'
        : neverCompressed
          ? '当前输入未超压缩阈值'
          : `压缩后 / 压缩前，省 ${fmtPct(1 - ratioMean)}`,
    }),
    statCard({
      label: '预算利用率',
      value: utilMean == null ? '—' : fmtPct(utilMean, 1),
      icon: 'gauge',
      tone: utilMean == null ? undefined : utilMean > 0.9 ? 'warning' : 'success',
      foot: utilP90 != null
        ? `最紧张的角色 p90 为 ${fmtPct(utilP90, 1)}`
        : '暂无数据',
    }),
    statCard({
      label: '累计 token',
      value: fmtCompact(totalIn + totalOut),
      unit: '',
      icon: 'hash',
      foot: `输入 ${fmtCompact(totalIn)} / 输出 ${fmtCompact(totalOut)}`,
    }),
    statCard({
      label: '累计成本',
      value: cost ? fmtCost(cost) : '—',
      icon: 'coins',
      foot: `回退 ${fmtInt(backtracks)} 次 · 拦截 ${fmtInt(blocked)} 次`,
      tone: blocked ? 'success' : undefined,
    })
  );

  return row;
}

/* ------------------------------------------------------------------ *
 * 五层能力
 * ------------------------------------------------------------------ */

function buildLayerGrid(metrics) {
  const { counters, histograms } = metrics || {};
  const grid = el('div', {
    class: 'grid context-layer-grid',
  });

  for (const layer of LAYERS) {
    grid.append(buildLayerCard(layer, counters, histograms));
  }

  return card({
    title: '五层上下文能力',
    body: grid,
  });
}

function buildLayerCard(layer, counters, histograms) {
  const body = el('div', { class: 'metric-list' });
  let hasData = false;

  // 直方图：均值 + 最差分位
  for (const name of layer.histograms) {
    const hist = metric(histograms, name);
    if (!hist) continue;
    const mean = histOverallMean(hist);
    if (mean == null) continue;
    hasData = true;

    const isRatio = /ratio/.test(name);
    const isUtil = /utilization/.test(name);
    const p90 = histMaxQuantile(hist, 'p90');

    let display;
    let ratio;
    let tone = '';

    if (isUtil) {
      // 利用率是 0–1 的比例，展示成百分数
      display = fmtPct(mean, 1);
      ratio = Math.min(1, mean);
      tone = mean > 0.9 ? 'warning' : '';
    } else if (isRatio) {
      display = mean.toFixed(3);
      ratio = Math.max(0, Math.min(1, 1 - mean)); // 条子长度代表「省下的比例」
      tone = mean < 0.7 ? 'success' : '';
    } else if (/latency|duration/.test(name)) {
      display = mean >= 1000 ? `${(mean / 1000).toFixed(2)}s` : `${mean.toFixed(0)}ms`;
    } else {
      display = mean.toFixed(3);
    }

    const p90Text = p90 != null
      ? `  p90 ${isUtil ? fmtPct(p90, 1) : p90.toFixed(/ratio/.test(name) ? 3 : 0)}`
      : '';

    body.append(
      metricRow({
        name: layerMetricLabel(name),
        value: display + p90Text,
        ratio,
        tone,
      })
    );
  }

  // 计数器：总量 + 标签维度拆分
  for (const name of layer.counters) {
    const counter = metric(counters, name);
    if (!counter) continue;
    hasData = true;
    const total = sumLabels(counter);
    const breakdown = Object.entries(counter);

    body.append(
      metricRow({ name: layerMetricLabel(name), value: fmtInt(total) })
    );

    if (breakdown.length > 1) {
      const chips = el('div', {
        style: { display: 'flex', 'flex-wrap': 'wrap', gap: 'var(--space-2)', 'margin-top': 'var(--space-2)' },
      });
      // 取数值最大的前 6 项，最相关的排前面
      const top = breakdown.sort((a, b) => Number(b[1]) - Number(a[1])).slice(0, 6);
      for (const [label, v] of top) {
        chips.append(
          el('span', { class: 'tag', text: `${compactLabel(label)} ${fmtInt(v)}` })
        );
      }
      body.append(chips);
    }
  }

  if (!hasData) {
    body.append(
      el('p', {
        class: 'hint-text',
        text: '暂无数据',
      })
    );
  }

  return el('div', {
    class: 'context-layer-card',
    style: {
      padding: 'var(--space-3)',
      border: '1px solid var(--border-subtle)',
      'border-radius': 'var(--radius-md)',
      background: 'var(--surface-base)',
    },
  }, [
    el('div', { style: { display: 'flex', 'align-items': 'center', gap: 'var(--space-2)', 'margin-bottom': 'var(--space-2)' } }, [
      el('span', {
        class: 'stat-card-icon',
        html: icon(layer.icon, 14),
        style: { width: '22px', height: '22px' },
      }),
      el('span', { text: layer.name, attrs: { title: layer.desc }, style: { 'font-size': 'var(--fs-sm)', 'font-weight': '600' } }),
    ]),
    body,
  ]);
}

/* ------------------------------------------------------------------ *
 * 压缩卡
 * ------------------------------------------------------------------ */

function buildCompressionCard(metrics) {
  const hist = metric(metrics?.histograms, 'context_compression_ratio');
  const body = el('div');

  // 汇总所有标签（agent=...）的统计量
  const labels = hist ? Object.values(hist).filter((v) => v && typeof v === 'object') : [];
  const totalCount = labels.reduce((s, v) => s + (Number(v.count) || 0), 0);

  if (!labels.length || totalCount === 0) {
    body.append(
      emptyState({
        small: true,
        icon: 'package',
        title: '未触发压缩',
        desc: '超过阈值后产生记录。',
      })
    );
  } else {
    const mean = histOverallMean(hist);
    const minVal = Math.min(...labels.map((v) => Number(v.min)).filter(Number.isFinite));
    const maxVal = Math.max(...labels.map((v) => Number(v.max)).filter(Number.isFinite));
    const p90 = histMaxQuantile(hist, 'p90');

    const stats = [
      ['触发次数', fmtInt(totalCount)],
      ['加权平均', mean?.toFixed(3) ?? '—'],
      ['最小值', Number.isFinite(minVal) ? minVal.toFixed(3) : '—'],
      ['最大值', Number.isFinite(maxVal) ? maxVal.toFixed(3) : '—'],
      ['最差 p90', p90 != null ? p90.toFixed(3) : '—'],
      ['涉及角色', String(Object.keys(hist).length)],
    ];
    const grid = el('div', { class: 'grid grid-cols-3', style: { gap: 'var(--space-3)' } });
    for (const [k, v] of stats) {
      grid.append(
        el('div', {}, [
          el('div', { class: 'kv-key', text: k }),
          el('div', { class: 'kv-value tnum', text: v }),
        ])
      );
    }
    body.append(grid);

    if (mean != null) {
      const kept = Math.max(0, Math.min(1, mean));
      body.append(
        el('div', { style: { 'margin-top': 'var(--space-4)' } }, [
          el('div', {
            style: {
              display: 'flex',
              'justify-content': 'space-between',
              'font-size': 'var(--fs-2xs)',
              color: 'var(--text-tertiary)',
              'margin-bottom': 'var(--space-2)',
            },
          }, [
            el('span', { text: `保留 ${fmtPct(kept)}` }),
            el('span', { text: `裁掉 ${fmtPct(1 - kept)}` }),
          ]),
          el('div', { class: 'bar-split' }, [
            el('span', { style: { width: `${kept * 100}%`, background: 'var(--brand)' } }),
            el('span', { style: { width: `${(1 - kept) * 100}%`, background: 'var(--status-success)' } }),
          ]),
        ])
      );

      if (mean >= 0.999) {
        body.append(
          el('div', { style: { 'margin-top': 'var(--space-3)' } }, [
            alert({
              tone: 'info',
              title: '尚未发生实际压缩',
            }),
          ])
        );
      }
    }
  }

  return card({
    title: '上下文压缩',
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
        desc: '设置 DEVAGENT_CACHE__ENABLED=true 可复用相同或语义相近的模型响应。',
      })
    );
    return card({ title: '模型响应缓存', body });
  }

  const isVector = cache.mode === 'vector';
  const list = el('div', { class: 'metric-list' });

  if (isVector) {
    list.append(
      metricRow({
        name: '总命中率',
        value: fmtPct(cache.hit_rate ?? 0),
        ratio: cache.hit_rate ?? 0,
      }),
      metricRow({
        name: '精确命中',
        value: fmtInt(cache.exact_hits ?? 0),
      }),
      metricRow({
        name: '语义命中',
        value: fmtInt(cache.semantic_hits ?? 0),
        tone: (cache.semantic_hits ?? 0) === 0 ? 'warning' : 'success',
      })
    );

    // 语义命中占比是判断「向量检索值不值」的唯一指标，单独凸显
    const share = cache.semantic_share ?? 0;
    list.append(
      el('div', {
        style: {
          padding: 'var(--space-3)',
          'border-radius': 'var(--radius-md)',
          background: share === 0 ? 'var(--status-warning-bg)' : 'var(--status-success-bg)',
          border: `1px solid ${share === 0 ? 'var(--status-warning-border)' : 'var(--status-success-border)'}`,
        },
      }, [
        el('div', {
          style: { display: 'flex', 'justify-content': 'space-between', 'align-items': 'baseline' },
        }, [
          el('span', {
            text: '语义命中占比',
            style: { 'font-size': 'var(--fs-xs)', color: 'var(--text-secondary)' },
          }),
          el('span', {
            text: fmtPct(share),
            style: { 'font-weight': '700', 'font-variant-numeric': 'tabular-nums' },
          }),
        ]),
      ])
    );
  } else {
    list.append(
      metricRow({ name: '模式', value: '精确匹配' }),
      metricRow({ name: '命中', value: fmtInt(cache.hits ?? 0) }),
      metricRow({ name: '未命中', value: fmtInt(cache.misses ?? 0) }),
      metricRow({
        name: '命中率',
        value: fmtPct(cache.hit_rate ?? 0),
        ratio: cache.hit_rate ?? 0,
      })
    );
  }

  list.append(metricRow({ name: '缓存条目数', value: fmtInt(cache.size ?? 0) }));
  body.append(list);

  if (cache.embed_failures) {
    body.append(
      el('div', { style: { 'margin-top': 'var(--space-3)' } }, [
        alert({
          tone: 'warning',
          title: `${cache.embed_failures} 次嵌入调用失败`,
          body: '缓存已静默降级为精确匹配：功能不受影响，但语义检索收益归零。请检查嵌入模型的 Key 与可用性。',
        }),
      ])
    );
  }

  return card({
    title: '模型响应缓存',
    body,
  });
}

/* ------------------------------------------------------------------ *
 * 原始指标
 * ------------------------------------------------------------------ */

function buildRawMetrics(metrics) {
  const body = el('div');

  if (!metrics) {
    body.append(emptyState({ small: true, icon: 'activity', title: '暂无指标快照' }));
    return card({ title: '指标快照', body });
  }

  const text = JSON.stringify(metrics, null, 2);
  const pre = el('pre', { class: 'code', style: { 'max-height': '340px' } });
  pre.textContent = text;

  const copyBtn = button('复制 JSON', {
    icon: 'copy',
    variant: 'ghost',
    small: true,
    onClick: async () => {
      const ok = await copyText(text);
      toast({
        tone: ok ? 'success' : 'error',
        title: ok ? '已复制指标快照' : '复制失败',
        duration: 1600,
      });
    },
  });

  const exportBtn = button('导出 Prometheus', {
    icon: 'download',
    variant: 'ghost',
    small: true,
    onClick: async () => {
      try {
        const textOut = await api.prometheusText();
        const blob = new Blob([textOut], { type: 'text/plain;charset=utf-8' });
        const url = URL.createObjectURL(blob);
        const a = el('a', { attrs: { href: url, download: 'devagent-metrics.prometheus' } });
        document.body.append(a);
        a.click();
        a.remove();
        URL.revokeObjectURL(url);
        toast({ tone: 'success', title: '已导出' });
      } catch (err) {
        toast({ tone: 'error', title: '导出失败', desc: err?.message || String(err) });
      }
    },
  });

  body.append(pre);

  return el('details', { class: 'card metric-disclosure' }, [
    el('summary', { class: 'card-header' }, [el('h2', { text: '指标快照' })]),
    el('div', { class: 'card-body' }, [el('div', { class: 'metric-disclosure-actions' }, [exportBtn, copyBtn]), body]),
  ]);
}

/* ------------------------------------------------------------------ *
 * 工具
 * ------------------------------------------------------------------ */

/** 把指标名转成可读中文。未知名称做一次轻量美化而不是原样抛出。 */
function layerMetricLabel(name) {
  const map = {
    llm_calls: '模型调用次数',
    llm_tokens: 'token 消耗',
    llm_cost_usd: '累计成本',
    llm_latency_ms: '模型响应延迟',
    node_executions: '节点执行次数',
    node_duration_ms: '节点耗时',
    verdict_total: '验证判定次数',
    hallucination_blocked: '拦截的幻觉输出',
    backtracks: '回退重试次数',
    retries: '重试次数',
    task_total: '任务总数',
    task_duration_ms: '任务耗时',
    context_compression_ratio: '压缩比',
    context_utilization: '预算利用率',
    reflexion_lessons: '反思经验条数',
  };
  return map[name] || name.replace(/_/g, ' ');
}

/** 把 Prometheus 标签串压缩显示，例如 `agent="coder"` → `coder`。 */
function compactLabel(label) {
  const m = /(?:agent|model|tier|role|status|direction|reason)="([^"]+)"/g;
  const hits = [...String(label).matchAll(m)].map((x) => x[1]);
  if (hits.length) return hits.join('/');
  const s = String(label);
  return s.length > 24 ? `${s.slice(0, 24)}…` : s;
}
