/**
 * 可观测性页。
 *
 * 三个视图：
 *   - 指标：counters / gauges / histograms 的表格化展示（可搜索、可排序）
 *   - 追踪：最近 span 列表，展示调用链与耗时
 *   - 缓存：与上下文页共用的缓存数据源，这里聚焦命中趋势
 *
 * 关键设计：**三层嵌套差异必须显式处理**。
 * counters/gauges 是 `{名称: {标签: 数值}}`（两层），
 * histograms 是 `{名称: {标签: {count,sum,p50,p90...}}}`（三层）。
 * 把它们统一渲染会直接导致 histogram 显示成 [object Object]。
 */

import { api } from '../api.js';
import {
  alert,
  button,
  card,
  emptyState,
  errorState,
  iconButton,
  skeletonBlock,
  skeletonTable,
  statCard,
  toast,
} from '../components.js';
import { icon } from '../icons.js';
import { getState, set } from '../store.js';
import {
  clear, copyText, el, fmtInt, fromHTML, histOverallMean, metric, mount,
  prefersReducedMotion, sumLabels,
} from '../util.js';

const LOAD_KEY = 'observability';
const AUTO_REFRESH_MS = 10_000;

export default async function renderObservability(root, ctx) {
  const container = el('div');
  root.append(container);

  let disposed = false;
  let timer = 0;
  let autoRefresh = true;
  let activeTab = 'metrics';
  let metricQuery = '';
  let sortState = { key: 'name', dir: 'asc' };
  let traces = [];

  /* ---------- 数据加载 ---------- */

  async function load({ silent = false } = {}) {
    if (!silent) {
      mount(container, buildSkeleton());
    }
    try {
      const [metrics, cache, traceRes] = await Promise.all([
        api.metrics(),
        api.cacheStats().catch(() => null),
        api.traces({ limit: 200 }).catch(() => ({ total: 0, spans: [] })),
      ]);
      if (disposed) return;
      traces = Array.isArray(traceRes?.spans) ? traceRes.spans : [];
      set({ metrics, cacheStats: cache, traces });
      paint();
    } catch (err) {
      if (disposed) return;
      mount(
        container,
        card({
          body: errorState({
            title: '可观测性数据加载失败',
            message: err?.message || String(err),
            onRetry: () => load(),
          }),
        })
      );
      return;
    }
  }

  function paint() {
    if (disposed) return;

    // 别把用户正在打字的输入框连同整页一起拆掉。
    // 自动刷新每 10s 一次，而 load({silent:true}) 会走 paint() ——
    // 原实现会整套重建，光标与已输入的半截关键词一起消失，
    // 表现为"输入框每隔几秒自己清空一次"。这里退化为只重绘数据区。
    if (isTypingInPage()) {
      for (const repaint of dataRegionRepaints) repaint();
      return;
    }

    dataRegionRepaints.clear();
    clear(container);
    mount(container, buildLayout());
  }

  /**
   * 数据区就地重绘函数集合。
   * 由各 tab 在构建时注册，`paint()` 在"用户正在输入"时改调它们 ——
   * 这样一次静默刷新既能更新数据，又不会碰输入框。
   */
  const dataRegionRepaints = new Set();

  /** 焦点是否落在本页的文本输入控件里。 */
  function isTypingInPage() {
    const active = document.activeElement;
    if (!active || !container.contains(active)) return false;
    const tag = active.tagName;
    return tag === 'INPUT' || tag === 'TEXTAREA' || active.isContentEditable === true;
  }

  /* ---------- 布局 ---------- */

  function buildLayout() {
    const wrap = el('div');
    const metrics = getState('metrics');
    const observability = getState('observability');

    wrap.append(
      el('div', { class: 'page-head' }, [
        el('div', { class: 'page-head-text' }, [
          el('h1', { text: '可观测性' }),
          el('p', {
            class: 'page-head-desc',
            text: '指标快照、调用链追踪与 Prometheus 导出。这里的数据同时服务于「排障」与「证明系统可靠性」。',
          }),
        ]),
        el('div', { class: 'page-head-actions' }, [
          buildAutoRefreshToggle(),
          iconButton('refresh', { label: '立即刷新', onClick: () => load({ silent: true }) }),
        ]),
      ])
    );

    if (!observability) {
      wrap.append(
        el('div', { class: 'section' }, [
          alert({
            tone: 'info',
            title: '可观测性未开启',
            body: '启动时加 --trace，或设置 DEVAGENT_OBSERVABILITY__METRICS_ENABLED=true。'
              + '未开启时指标为空，追踪列表也不会有数据。',
          }),
        ])
      );
    }

    if (metrics) {
      wrap.append(buildKpiRow(metrics));
    }

    // 标签页
    const tabs = el('div', { class: 'tabs', attrs: { role: 'tablist' } });
    const tabDefs = [
      ['metrics', '指标', countMetrics(metrics)],
      ['traces', '链路追踪', traces.length],
      ['cache', '缓存', null],
    ];
    for (const [id, label, count] of tabDefs) {
      const btn = el('button', {
        class: 'tab',
        attrs: {
          type: 'button',
          role: 'tab',
          id: `tab-${id}`,
          'aria-selected': String(activeTab === id),
          'aria-controls': `panel-${id}`,
          tabindex: activeTab === id ? '0' : '-1',
        },
        on: {
          click: () => {
            activeTab = id;
            paint();
          },
          keydown: (e) => {
            // 标准 tablist 键盘交互：左右方向键切换
            if (e.key !== 'ArrowRight' && e.key !== 'ArrowLeft') return;
            e.preventDefault();
            const idx = tabDefs.findIndex((t) => t[0] === activeTab);
            const nextIdx = e.key === 'ArrowRight'
              ? (idx + 1) % tabDefs.length
              : (idx - 1 + tabDefs.length) % tabDefs.length;
            activeTab = tabDefs[nextIdx][0];
            paint();
            document.getElementById(`tab-${activeTab}`)?.focus();
          },
        },
      });
      btn.append(el('span', { text: label }));
      if (count != null) btn.append(el('span', { class: 'tab-count', text: fmtInt(count) }));
      tabs.append(btn);
    }

    wrap.append(tabs);

    const panel = el('div', {
      attrs: { role: 'tabpanel', id: `panel-${activeTab}`, 'aria-labelledby': `tab-${activeTab}` },
      style: { 'margin-top': 'var(--space-4)' },
    });

    if (activeTab === 'metrics') panel.append(...buildMetricsTab(metrics));
    else if (activeTab === 'traces') panel.append(...buildTracesTab());
    else panel.append(...buildCacheTab(getState('cacheStats')));

    wrap.append(panel);
    return wrap;
  }

  function buildAutoRefreshToggle() {
    const btn = el('button', {
      class: 'btn btn-secondary btn-sm',
      attrs: { type: 'button', 'aria-pressed': String(autoRefresh) },
      on: {
        click: () => {
          autoRefresh = !autoRefresh;
          setupTimer();
          paint();
        },
      },
    });
    btn.append(fromHTML(icon(autoRefresh ? 'pause' : 'play', 13)));
    btn.append(el('span', { text: autoRefresh ? '自动刷新 10s' : '已暂停刷新' }));
    return btn;
  }

  /* ---------- KPI ---------- */

  function buildKpiRow(metrics) {
    const totalCounters = countMetrics(metrics, 'counters');
    const totalGauges = countMetrics(metrics, 'gauges');
    const totalHistograms = countMetrics(metrics, 'histograms');

    // 从 gauge 里找活跃任务数之类的实时量
    const running = getState('tasks')?.filter?.((t) => t.status === 'running').length
      ?? (getState('tasks') || []).filter((t) => t.status === 'running').length;

    const row = el('div', {
      class: 'grid grid-cols-4',
      style: { 'margin-bottom': 'var(--space-5)' },
    });

    row.append(
      statCard({ label: '计数器指标', value: fmtInt(totalCounters), icon: 'hash', foot: '只增不减的累计量' }),
      statCard({ label: '瞬时指标', value: fmtInt(totalGauges), icon: 'gauge', foot: '当前值，可升可降' }),
      statCard({ label: '直方图指标', value: fmtInt(totalHistograms), icon: 'activity', foot: '含 p50/p90 等分位统计' }),
      statCard({ label: 'Trace span', value: fmtInt(traces.length), icon: 'route', foot: '内存导出器保留的最近记录' })
    );

    void running;
    return row;
  }

  /* ---------- 指标表 ---------- */

  function buildMetricsTab(metrics) {
    if (!metrics) {
      return [
        card({
          body: emptyState({
            icon: 'activity',
            title: '暂无指标',
            desc: '服务尚未产生任何指标记录，或可观测性未开启。',
          }),
        }),
      ];
    }

    const counters = Object.entries(metrics.counters || {});
    const gauges = Object.entries(metrics.gauges || {});
    const histograms = Object.entries(metrics.histograms || {});

    const allRows = [
      ...counters.map(([name, labels]) => ({ type: 'counter', name, labels: flatten(labels) })),
      ...gauges.map(([name, labels]) => ({ type: 'gauge', name, labels: flatten(labels) })),
      ...histograms.map(([name, labels]) => ({ type: 'histogram', name, labels: flattenHist(labels) })),
    ];

    // 搜索框
    const searchInput = el('input', {
      class: 'input',
      attrs: { type: 'search', placeholder: '搜索指标名或标签…', 'aria-label': '搜索指标', value: metricQuery },
    });

    // 行数提示：必须由**同一次**过滤结果驱动，否则会和表格显示的内容对不上
    const countHint = el('span', { class: 'hint-text' });

    /** 排序：就地排**副本**（绝不改 allRows，页面会反复重算过滤结果）。 */
    function sortedRows() {
      const rows = filterMetrics(allRows, metricQuery);
      rows.sort((a, b) => {
        const dir = sortState.dir === 'asc' ? 1 : -1;
        if (sortState.key === 'type') return a.type.localeCompare(b.type) * dir;
        return a.name.localeCompare(b.name) * dir;
      });
      return rows;
    }

    const tableHost = el('div');

    /**
     * 重绘表格区。
     *
     * ## 为什么每次都要重算（这是一次真实的翻车）
     *
     * 原实现把 `filtered` 算在 buildMetricsTab 的作用域里，输入框的 handler
     * 只调 refreshTable() —— 而那个函数读的是**闭包捕获的那一个数组**。
     * 结果是：打字时 query 变了、表格纹丝不动、行数提示永远是初始值，
     * 用户会以为"搜索坏了"。这类 bug 不会报错，只会让人觉得功能没做。
     *
     * 因此过滤结果与提示文案都从**当前** query 现算，不缓存到闭包里。
     */
    function refreshTable() {
      const rows = sortedRows();
      countHint.textContent = `${rows.length} / ${allRows.length} 条`;
      clear(tableHost);
      if (!rows.length) {
        tableHost.append(
          emptyState({ small: true, icon: 'search', title: '没有匹配的指标', desc: '试试缩短关键词' })
        );
        return;
      }
      tableHost.append(buildMetricTable(rows));
    }

    searchInput.addEventListener('input', () => {
      metricQuery = searchInput.value;
      // 只更新表格区与提示，不动输入框本身 —— 否则光标位置会丢
      refreshTable();
    });

    const toolbar = el('div', { class: 'toolbar' }, [
      el('div', { class: 'input-group', style: { flex: '1 1 240px', 'max-width': '360px' } }, [
        el('span', { class: 'input-icon', html: icon('search', 14) }),
        searchInput,
      ]),
      el('div', { class: 'toolbar-spacer' }),
      countHint,
    ]);

    const tableCard = card({
      title: '指标明细',
      subtitle: 'counters / gauges 为两层结构，histograms 为三层（最内层是统计量）',
      actions: button('复制全部 JSON', {
        icon: 'copy',
        variant: 'ghost',
        small: true,
        onClick: async () => {
          const ok = await copyText(JSON.stringify(metrics, null, 2));
          toast({ tone: ok ? 'success' : 'error', title: ok ? '已复制' : '复制失败', duration: 1600 });
        },
      }),
      body: tableHost,
      flush: true,
    });

    refreshTable();
    // 注册"就地重绘"入口：静默刷新时若用户正在输入，改调它而不是重建整页
    dataRegionRepaints.add(refreshTable);

    return [toolbar, tableCard];
  }

  function buildMetricTable(rows) {
    const wrap = el('div', { class: 'table-wrap table-wrap-stack', style: { 'max-height': '560px', 'overflow-y': 'auto' } });
    const table = el('table', { class: 'table' });

    const makeTh = (key, label) => {
      const active = sortState.key === key;
      const th = el('th', { attrs: { 'aria-sort': active ? (sortState.dir === 'asc' ? 'ascending' : 'descending') : 'none' } });
      const btn = el('button', {
        class: 'table-sort',
        attrs: { type: 'button' },
        on: {
          click: () => {
            if (sortState.key === key) sortState.dir = sortState.dir === 'asc' ? 'desc' : 'asc';
            else sortState = { key, dir: 'asc' };
            paint();
          },
        },
      });
      btn.append(el('span', { text: label }));
      btn.append(fromHTML(icon(active && sortState.dir === 'desc' ? 'arrow-down' : 'arrow-up', 11)));
      th.append(btn);
      return th;
    };

    table.append(
      el('thead', {}, [
        el('tr', {}, [
          makeTh('name', '指标名'),
          makeTh('type', '类型'),
          el('th', { text: '标签 / 值' }),
        ]),
      ])
    );

    const tbody = el('tbody');
    for (const r of rows) {
      const tr = el('tr', {});
      tr.append(
        el('td', { class: 'primary', dataset: { label: '指标名' } }, [
          el('span', { class: 'mono', text: r.name, style: { 'font-size': 'var(--fs-xs)', 'word-break': 'break-all' } }),
        ]),
        el('td', { dataset: { label: '类型' } }, [
          el('span', { class: `badge badge-${typeTone(r.type)}`, text: r.type }),
        ]),
        el('td', { dataset: { label: '标签 / 值' } }, [buildLabelList(r.labels)])
      );
      tbody.append(tr);
    }
    table.append(tbody);
    wrap.append(table);
    return wrap;
  }

  function buildLabelList(labels) {
    if (!labels.length) return el('span', { class: 'hint-text', text: '—' });
    const list = el('div', { style: { display: 'flex', 'flex-direction': 'column', gap: '2px' } });
    for (const l of labels.slice(0, 12)) {
      list.append(
        el('div', {
          style: {
            display: 'flex',
            'justify-content': 'space-between',
            gap: 'var(--space-3)',
            'font-size': 'var(--fs-2xs)',
            'font-family': 'var(--font-mono)',
          },
        }, [
          el('span', { text: l.label || '(无标签)', style: { color: 'var(--text-tertiary)', 'word-break': 'break-all' } }),
          el('span', { text: l.value, style: { color: 'var(--text-secondary)', 'font-variant-numeric': 'tabular-nums', 'flex': 'none' } }),
        ])
      );
    }
    if (labels.length > 12) {
      list.append(el('div', { class: 'hint-text', text: `… 另有 ${labels.length - 12} 条` }));
    }
    return list;
  }

  /* ---------- 追踪 ---------- */

  function buildTracesTab() {
    if (!traces.length) {
      return [
        card({
          body: emptyState({
            icon: 'route',
            title: '暂无 trace 记录',
            desc: '追踪数据来自内存导出器。启用 --trace 后，执行任务即可产生 span。'
              + '生产环境建议改为 OTLP 导出到 Jaeger/Tempo，内存导出器只适用于开发调试。',
          }),
        }),
      ];
    }

    const sorted = [...traces].sort((a, b) => (b.duration_ms || 0) - (a.duration_ms || 0));
    const maxDuration = sorted[0]?.duration_ms || 1;

    const wrap = el('div', { class: 'table-wrap table-wrap-stack' });
    const table = el('table', { class: 'table' });
    table.append(
      el('thead', {}, [
        el('tr', {}, [
          el('th', { text: 'Span' }),
          el('th', { class: 'num', text: '耗时' }),
          el('th', { text: '时长分布' }),
          el('th', { text: '状态' }),
          el('th', { text: 'Trace ID' }),
        ]),
      ])
    );

    const tbody = el('tbody');
    for (const s of sorted.slice(0, 100)) {
      const dur = s.duration_ms || 0;
      const pct = Math.max(1, (dur / maxDuration) * 100);
      const tone = s.status === 'error' ? 'danger' : dur === maxDuration ? 'warning' : '';

      const tr = el('tr', {});
      tr.append(
        el('td', { class: 'primary', dataset: { label: 'Span' } }, [
          el('span', { class: 'mono', text: s.name, style: { 'font-size': 'var(--fs-xs)' } }),
        ]),
        el('td', {
          class: 'num',
          dataset: { label: '耗时' },
          text: dur >= 1000 ? `${(dur / 1000).toFixed(2)}s` : `${dur.toFixed(1)}ms`,
        }),
        el('td', { dataset: { label: '分布' } }, [
          el('div', { class: 'bar', style: { 'min-width': '80px' } }, [
            el('div', {
              class: `bar-fill${tone ? ` bar-fill-${tone}` : ''}`,
              style: { width: `${pct}%` },
            }),
          ]),
        ]),
        el('td', { dataset: { label: '状态' } }, [
          el('span', {
            class: `badge badge-${s.status === 'error' ? 'danger' : s.status === 'ok' ? 'success' : 'neutral'}`,
            text: s.status || 'unset',
          }),
        ]),
        el('td', { dataset: { label: 'Trace ID' } }, [
          el('span', {
            class: 'mono',
            text: String(s.trace_id || '').slice(0, 12),
            style: { 'font-size': 'var(--fs-2xs)', color: 'var(--text-tertiary)' },
          }),
        ])
      );
      tbody.append(tr);
    }
    table.append(tbody);
    wrap.append(table);

    return [
      card({
        title: '调用链 Span',
        subtitle: `按耗时降序，显示前 100 条（共 ${traces.length} 条）`,
        body: wrap,
        flush: true,
      }),
    ];
  }

  /* ---------- 缓存 ---------- */

  function buildCacheTab(cache) {
    if (!cache || cache.mode === 'disabled') {
      return [
        card({
          body: emptyState({
            icon: 'database',
            title: '缓存未启用',
            desc: '设置 DEVAGENT_CACHE__ENABLED=true 后重启服务即可启用。',
          }),
        }),
      ];
    }

    const list = el('div', { class: 'metric-list' });
    const pct = (v) => `${((Number(v) || 0) * 100).toFixed(1)}%`;

    if (cache.mode === 'vector') {
      list.append(
        el('div', { class: 'grid grid-cols-2', style: { gap: 'var(--space-4)' } }, [
          statCard({ label: '精确命中', value: fmtInt(cache.exact_hits ?? 0), icon: 'check-circle' }),
          statCard({ label: '语义命中', value: fmtInt(cache.semantic_hits ?? 0), icon: 'zap', tone: (cache.semantic_hits ?? 0) ? 'success' : 'warning' }),
          statCard({ label: '总命中率', value: pct(cache.hit_rate), icon: 'percent' }),
          statCard({ label: '语义命中占比', value: pct(cache.semantic_share), icon: 'target', tone: (cache.semantic_share ?? 0) ? 'success' : 'warning' }),
        ])
      );
    } else {
      list.append(
        el('div', { class: 'grid grid-cols-3', style: { gap: 'var(--space-4)' } }, [
          statCard({ label: '命中', value: fmtInt(cache.hits ?? 0), icon: 'check-circle' }),
          statCard({ label: '未命中', value: fmtInt(cache.misses ?? 0), icon: 'x-circle' }),
          statCard({ label: '命中率', value: pct(cache.hit_rate), icon: 'percent' }),
        ])
      );
    }

    const body = el('div', {}, [
      list,
      el('div', { class: 'divider' }),
      el('div', { class: 'grid grid-cols-2', style: { gap: 'var(--space-4)' } }, [
        el('div', {}, [
          el('div', { class: 'kv-key', text: '缓存模式' }),
          el('div', { class: 'kv-value', text: cache.mode === 'vector' ? '向量检索（exact + semantic）' : '精确匹配' }),
        ]),
        el('div', {}, [
          el('div', { class: 'kv-key', text: '缓存条目数' }),
          el('div', { class: 'kv-value tnum', text: fmtInt(cache.size ?? 0) }),
        ]),
      ]),
    ]);

    if (cache.embed_failures) {
      body.append(
        el('div', { style: { 'margin-top': 'var(--space-4)' } }, [
          alert({
            tone: 'warning',
            title: `${cache.embed_failures} 次嵌入调用失败`,
            body: '缓存已静默降级为精确匹配。功能不受影响，但语义检索收益归零 —— 请检查嵌入模型配置。',
          }),
        ])
      );
    }

    if (cache.mode === 'vector' && (cache.semantic_hits ?? 0) === 0 && (cache.hits ?? 0) > 0) {
      body.append(
        el('div', { style: { 'margin-top': 'var(--space-3)' } }, [
          alert({
            tone: 'warning',
            title: '语义命中为 0，向量检索未带来增量',
            body: '当前请求之间差异较大，语义检索只增加了嵌入调用成本。可考虑关闭 DEVAGENT_CACHE__SEMANTIC 以降本。',
          }),
        ])
      );
    }

    return [card({ title: '模型响应缓存', body })];
  }

  /* ---------- 定时器 ---------- */

  function setupTimer() {
    window.clearInterval(timer);
    if (!autoRefresh) return;
    timer = window.setInterval(() => {
      // 页面隐藏或标签页在别的页面时不刷新，避免无意义请求
      if (document.visibilityState === 'visible' && !document.hidden) {
        load({ silent: true });
      }
    }, AUTO_REFRESH_MS);
  }

  /* ---------- 骨架 ---------- */

  function buildSkeleton() {
    const wrap = el('div');
    const grid = el('div', { class: 'grid grid-cols-4', style: { 'margin-bottom': 'var(--space-5)' } });
    for (let i = 0; i < 4; i += 1) {
      grid.append(el('div', { class: 'stat-card' }, [
        el('div', { class: 'skeleton skeleton-text', style: { width: '50%' } }),
        el('div', { class: 'skeleton skeleton-stat' }),
      ]));
    }
    wrap.append(grid, skeletonBlock(60), card({ body: skeletonTable(8, 3), flush: true }));
    return wrap;
  }

  /* ---------- 启动 ---------- */

  await load();
  setupTimer();

  // 页面隐藏时暂停定时器（省电 + 避免堆积）
  const onVisibility = () => {
    if (document.visibilityState === 'visible' && autoRefresh) load({ silent: true });
  };
  document.addEventListener('visibilitychange', onVisibility);

  void ctx;
  void prefersReducedMotion;

  return () => {
    disposed = true;
    window.clearInterval(timer);
    document.removeEventListener('visibilitychange', onVisibility);
  };
}

/* ------------------------------------------------------------------ *
 * 纯函数
 * ------------------------------------------------------------------ */

/**
 * 按关键词过滤指标行（**纯函数**，零 DOM，可直接单测）。
 *
 * 单独抽出来的理由：这段逻辑原本内联在 `buildMetricsTab()` 里，被闭包
 * 捕获成"只算一次"的数组 —— 输入框改的是 query，重绘用的却是旧结果。
 * 把决策变成纯函数之后，"打字能不能过滤"这件事才有办法断言。
 *
 * 匹配范围刻意包含**标签**：真实排障时用户是按 `agent="coder"`
 * 或模型名去找指标的，只匹配指标名会让人以为数据不存在。
 * 大小写不敏感（用户不会照着键名的大小写打字）。
 *
 * @param {Array<{type:string,name:string,labels:Array<{label:string,value:string}>}>} rows
 * @param {string} query 关键词，空/纯空白表示不过滤
 * @returns {Array} 过滤后的**新数组**（排序等后续操作不会污染入参）
 */
export function filterMetrics(rows, query) {
  const list = Array.isArray(rows) ? rows : [];
  const q = String(query ?? '').trim().toLowerCase();
  if (!q) return list.slice();
  return list.filter((r) => {
    if (String(r?.name ?? '').toLowerCase().includes(q)) return true;
    return (r?.labels || []).some((l) => String(l?.label ?? '').toLowerCase().includes(q));
  });
}

/** 统计某层有多少个指标名。 */
function countMetrics(metrics, layer) {
  if (!metrics) return 0;
  if (layer) return Object.keys(metrics[layer] || {}).length;
  return Object.keys(metrics.counters || {}).length
    + Object.keys(metrics.gauges || {}).length
    + Object.keys(metrics.histograms || {}).length;
}

/** counters/gauges：{标签: 数值} → [{label, value}]。 */
function flatten(map) {
  return Object.entries(map || {}).map(([label, v]) => ({
    label,
    value: typeof v === 'object' ? JSON.stringify(v) : formatNum(v),
  }));
}

/** histograms：{标签: {count,sum,p50,...}} → [{label, value}] 摘要。 */
function flattenHist(map) {
  return Object.entries(map || {}).map(([label, stats]) => {
    if (!stats || typeof stats !== 'object') {
      return { label, value: String(stats) };
    }
    // 只展示最有信息量的几个统计量，全列会撑爆表格
    const parts = [];
    if (stats.count != null) parts.push(`n=${formatNum(stats.count)}`);
    if (stats.mean != null) parts.push(`mean=${formatNum(stats.mean)}`);
    if (stats.p50 != null) parts.push(`p50=${formatNum(stats.p50)}`);
    if (stats.p90 != null) parts.push(`p90=${formatNum(stats.p90)}`);
    if (stats.max != null) parts.push(`max=${formatNum(stats.max)}`);
    if (!parts.length && stats.sum != null) parts.push(`sum=${formatNum(stats.sum)}`);
    return { label, value: parts.join('  ') || '—' };
  });
}

function formatNum(v) {
  const n = Number(v);
  if (!Number.isFinite(n)) return String(v);
  if (Number.isInteger(n)) return n.toLocaleString('zh-CN');
  return n.toFixed(3).replace(/\.?0+$/, '');
}

function typeTone(type) {
  return { counter: 'brand', gauge: 'info', histogram: 'warning' }[type] || 'neutral';
}
