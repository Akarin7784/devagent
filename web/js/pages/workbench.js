/**
 * 任务工作台。
 *
 * 这是产品的核心页面：提交需求 → 观察编排 → 查看产出。
 *
 * 布局（≥1280 三区并排，1024–1279 两区 + 详情下沉为整行，<1024 单列）：
 *   左：任务队列 + 提交表单
 *   中：DAG 画布 + 执行时间线
 *   右：节点详情 / 执行摘要
 *
 * 数据流向（关键设计）：
 *   SSE 事件 → 乐观更新图状态（立即反馈）
 *   GET /tasks/{id} → 权威快照覆盖（最终自愈）
 *
 * 两条路径并列存在是刻意的：SSE 提供低延迟，快照提供一致性。
 * 只靠 SSE 会因丢事件而永久停留在错误状态；只靠轮询则失去实时感。
 */

import { api, subscribeTaskEvents } from '../api.js';
import {
  agentTag,
  alert,
  badge,
  button,
  card,
  confirmDialog,
  emptyState,
  errorState,
  iconButton,
  kvItem,
  skeletonBlock,
  statusBadge,
  statusDotClass,
  toast,
} from '../components.js';
import { icon } from '../icons.js';
import {
  applyEvent,
  buildGraphState,
  diffStats,
  LEGEND_ITEMS,
  layoutDag,
  parseDiff,
  renderDag,
  statusLabel,
} from '../../graph.js';
import { getState, navigate, set } from '../store.js';
import {
  $, clear, copyText, debounce, el, esc, fmtCost, fmtDuration, fmtInt,
  fmtRelative, fmtTime, fromHTML, mount, prefersReducedMotion,
} from '../util.js';

const MAX_GOAL_LEN = 10_000;
const MAX_TIMELINE = 400;

/**
 * 页面入口。
 * @returns {() => void} 清理函数（关闭 SSE、移除订阅）
 */
export default async function renderWorkbench(root, ctx) {
  const disposers = [];
  let unsubscribeStream = null;
  let autoScroll = true;
  let timelineFilter = 'all';
  let countLabel = null;
  const events = [];

  /** 图状态（与 store 分离，因为 Map 需要就地变更才能高效 applyEvent）。 */
  let graph = new Map();
  let layout = null;
  let selectedNode = '';
  let taskDetail = null;

  /* ---------- DOM 骨架（一次构建，后续只更新局部） ---------- */

  const taskListEl = el('div', { class: 'task-list', attrs: { role: 'list' } });
  const goalInput = el('textarea', {
    class: 'textarea',
    attrs: {
      id: 'goal-input',
      rows: '5',
      placeholder: '例如：为 /users 接口增加分页参数，非法参数返回 400',
      'aria-label': '需求描述',
      maxlength: String(MAX_GOAL_LEN),
    },
  });
  const charCount = el('span', { class: 'field-counter', text: `0 / ${fmtInt(MAX_GOAL_LEN)}` });
  const submitBtn = button('开始执行', { icon: 'play', variant: 'primary', type: 'submit' });
  const submitForm = el('form', { class: 'field' });

  const dagSvg = el('svg', {
    class: 'dag',
    attrs: { xmlns: 'http://www.w3.org/2000/svg', role: 'img', 'aria-label': '任务依赖图' },
  });
  const dagEmpty = el('div');
  const dagCanvas = el('div', { class: 'dag-canvas' });
  const dagLegend = el('div', { class: 'legend' });
  const dagToolbar = el('div', { class: 'dag-toolbar' });

  const timelineEl = el('ol', { class: 'timeline', attrs: { 'aria-live': 'polite', 'aria-label': '执行事件' } });
  const timelineTools = el('div', { class: 'timeline-tools' });
  const summaryEl = el('div');

  const detailBody = el('div');

  /* ---------- 顶部：页面级操作 ---------- */

  const refreshBtn = iconButton('refresh', {
    label: '刷新当前任务',
    onClick: () => refreshCurrent({ manual: true }),
  });

  const head = el('div', { class: 'page-head' }, [
    el('div', { class: 'page-head-text' }, [
      el('h1', { text: '任务工作台' }),
      el('p', {
        class: 'page-head-desc',
        text: '提交自然语言需求，观察多 Agent 如何拆解、编排与互相验证。点击 DAG 节点可查看该步骤的代码改动。',
      }),
    ]),
    el('div', { class: 'page-head-actions' }, [refreshBtn]),
  ]);

  /* ---------- 组装 ---------- */

  const asideCol = el('div', { class: 'workbench-aside' }, [
    buildSubmitCard(),
    card({
      title: '任务队列',
      body: taskListEl,
      flush: true,
      actions: iconButton('refresh', {
        label: '刷新任务列表',
        small: true,
        onClick: () => loadTasks(),
      }),
    }),
  ]);

  const mainCol = el('div', { class: 'workbench-main' }, [
    card({
      title: '任务 DAG',
      subtitle: '节点依赖与实时执行状态',
      actions: dagLegend,
      body: el('div', {}, [dagCanvas, dagToolbar]),
    }),
    card({
      title: '执行时间线',
      actions: timelineTools,
      body: timelineEl,
      flush: false,
    }),
    summaryEl,
  ]);

  // 右栏：节点详情。用 workbench-detail（而非 workbench-aside）——
  // 两者视觉一致，但只有它能在 ≤1279px 时跨满整行下沉到主区下方。
  // 若复用 workbench-aside，CSS 无法区分左右两栏，下沉规则会同时命中左栏。
  const detailCol = el('div', { class: 'workbench-detail' }, [
    card({ title: '节点详情', body: detailBody }),
  ]);

  root.append(head, el('div', { class: 'workbench' }, [asideCol, mainCol, detailCol]));

  /* ---------- 渲染函数 ---------- */

  function buildSubmitCard() {
    const form = submitForm;
    form.append(
      el('div', { class: 'field' }, [
        el('div', {
          style: { display: 'flex', 'align-items': 'center', 'justify-content': 'space-between' },
        }, [
          el('label', { class: 'label', attrs: { for: 'goal-input' }, text: '需求描述' }),
          charCount,
        ]),
        goalInput,
      ]),
      el('div', { class: 'hint-text' }, [
        fromHTML(icon('info', 12)),
        ' 越具体越好：包含接口路径、边界条件与期望行为，需求 Agent 会据此生成可验收的标准。',
      ]),
      el('div', { style: { display: 'flex', 'gap': 'var(--space-2)', 'margin-top': 'var(--space-1)' } }, [
        submitBtn,
      ])
    );
    form.addEventListener('submit', onSubmit);

    goalInput.addEventListener('input', () => {
      const len = goalInput.value.length;
      charCount.textContent = `${fmtInt(len)} / ${fmtInt(MAX_GOAL_LEN)}`;
      charCount.dataset.state = len > MAX_GOAL_LEN * 0.95 ? 'over' : 'ok';
    });

    return card({ title: '提交需求', body: form });
  }

  /* ---------- 提交任务 ---------- */

  async function onSubmit(e) {
    e.preventDefault();
    const goal = goalInput.value.trim();
    if (!goal) {
      goalInput.setAttribute('aria-invalid', 'true');
      goalInput.focus();
      toast({ tone: 'warning', title: '请输入需求内容' });
      return;
    }
    goalInput.removeAttribute('aria-invalid');

    submitBtn.dataset.loading = 'true';
    submitBtn.setAttribute('aria-busy', 'true');
    try {
      const task = await api.createTask(goal);
      goalInput.value = '';
      charCount.textContent = `0 / ${fmtInt(MAX_GOAL_LEN)}`;
      toast({
        tone: 'success',
        title: '任务已提交',
        desc: `任务 ${shortId(task.task_id)} 开始执行`,
      });
      await loadTasks();
      await selectTask(task.task_id);
    } catch (err) {
      toast({
        tone: 'error',
        title: '提交失败',
        desc: err?.message || String(err),
      });
    } finally {
      delete submitBtn.dataset.loading;
      submitBtn.removeAttribute('aria-busy');
    }
  }

  /* ---------- 任务列表 ---------- */

  async function loadTasks() {
    try {
      const res = await api.listTasks({ limit: 100 });
      set({ tasks: Array.isArray(res?.items) ? res.items : [] });
      paintTaskList();
    } catch (err) {
      clear(taskListEl);
      taskListEl.append(
        errorState({
          small: true,
          title: '任务列表加载失败',
          message: err?.message || '',
          onRetry: () => loadTasks(),
        })
      );
    }
  }

  function paintTaskList() {
    const tasks = getState('tasks') || [];
    const active = getState('activeTaskId');
    clear(taskListEl);

    if (!tasks.length) {
      taskListEl.append(
        emptyState({
          small: true,
          icon: 'inbox',
          title: '暂无任务',
          desc: '在上方提交需求后，任务会出现在这里',
        })
      );
      return;
    }

    for (const t of tasks) {
      const item = el('button', {
        class: 'task-item',
        attrs: {
          type: 'button',
          ...(t.task_id === active ? { 'aria-current': 'true' } : {}),
          'aria-label': `任务：${t.goal}`,
        },
        dataset: { id: t.task_id },
        on: { click: () => selectTask(t.task_id) },
      });
      item.append(
        el('div', { class: 'task-item-goal', text: t.goal }),
        el('div', { class: 'task-item-meta' }, [
          el('span', { class: `dot dot-sm ${statusDotClass(t.status)}` }),
          el('span', { text: statusLabel(t.status) }),
          el('span', { text: '·' }),
          el('span', { text: fmtDuration(t.duration_ms) }),
          el('span', { text: '·' }),
          el('span', { text: `${fmtInt(t.total_tokens)} tok` }),
        ])
      );
      taskListEl.append(item);
    }
  }

  /* ---------- 选中任务 ---------- */

  async function selectTask(taskId) {
    if (!taskId) return;
    closeStream();
    set({ activeTaskId: taskId });
    paintTaskList();

    // 重置视图状态 —— 否则上一任务的节点会残留
    graph = new Map();
    layout = null;
    selectedNode = '';
    taskDetail = null;
    events.length = 0;

    paintDag();
    paintTimeline();
    mount(summaryEl);
    mount(detailBody, emptyState({
      small: true,
      icon: 'target',
      title: '未选择节点',
      desc: '点击 DAG 中的节点查看该步骤详情与代码改动',
    }));

    await refreshCurrent();
    openStream(taskId);

    // 同步 URL，便于分享/刷新后回到同一任务
    const { params } = currentRoute();
    if (params !== taskId) navigate('workbench', { task: taskId }, true);
  }

  function currentRoute() {
    const p = new URLSearchParams(window.location.hash.split('?')[1] || '');
    return { params: p.get('task') || '' };
  }

  /* ---------- 拉取权威快照 ---------- */

  async function refreshCurrent({ manual = false } = {}) {
    const taskId = getState('activeTaskId');
    if (!taskId) return;

    if (manual) refreshBtn.dataset.loading = 'true';
    try {
      const [detail, ctxData] = await Promise.all([
        api.getTask(taskId),
        api.getContext(taskId).catch(() => null),
      ]);

      if (detail && detail.task_id === getState('activeTaskId')) {
        taskDetail = detail;
        set({ taskDetail: detail });

        // 权威快照覆盖乐观状态：SSE 丢事件也能自愈
        const nodes = Array.isArray(detail.nodes) ? detail.nodes : [];
        if (nodes.length) {
          graph = buildGraphState(nodes);
        }
        paintDag();
        paintSummary(detail);
      }

      if (ctxData) {
        set({ contextMetrics: ctxData.metrics || {} });
      }

      if (manual) {
        toast({ tone: 'success', title: '已刷新', duration: 1600 });
      }
    } catch (err) {
      if (manual) {
        toast({ tone: 'error', title: '刷新失败', desc: err?.message || String(err) });
      } else {
        clear(summaryEl);
        summaryEl.append(
          card({
            body: errorState({
              title: '任务详情加载失败',
              message: err?.message || '',
              onRetry: () => refreshCurrent(),
            }),
          })
        );
      }
    } finally {
      delete refreshBtn.dataset.loading;
    }
  }

  /* ---------- SSE ---------- */

  function openStream(taskId) {
    unsubscribeStream = subscribeTaskEvents(taskId, {
      onEvent: (ev, meta) => {
        // 重连后的重复投递：直接丢弃。既不用重绘图（内容没变），
        // 也不能污染事件数组 —— 否则时间线会出现整段重复。
        if (meta?.duplicate) return;

        // 乐观更新：节点状态立即反映到图上
        const changed = applyEvent(graph, ev);
        if (changed) paintDag();

        if (events.length < MAX_TIMELINE) {
          events.push(ev);
          appendTimelineItem(ev, { fromReplay: meta?.replayed });
          // 回放期间不逐条刷新计数（几十条事件会触发几十次 DOM 写入）
          if (!meta?.replayed) paintTimelineMeta();
        }

        if (ev.kind === 'task_finished' || ev.kind === 'task_cancelled') {
          closeStream();
          // 任务结束：拉一次完整快照做最终对齐。
          // 失败不打扰用户 —— 任务本身已经结束了，快照失败只是少一次刷新。
          refreshCurrent({ background: true });
          loadTasks();
        }
      },
      onError: (err) => {
        // 重连中 —— 只提示一次，不刷屏
        if (!openStream.warned) {
          openStream.warned = true;
          toast({
            tone: 'warning',
            title: '事件流连接中断',
            desc: `${err.message}（浏览器会自动重连，历史事件不会重复）`,
          });
        }
      },
      onClose: () => {
        // 服务端在任务结束后主动关闭，属正常路径，不报错
      },
    });
  }

  function closeStream() {
    unsubscribeStream?.();
    unsubscribeStream = null;
    openStream.warned = false;
  }

  /* ---------- DAG ---------- */

  function paintDag() {
    const nodes = [...graph.values()];
    clear(dagCanvas);
    clear(dagToolbar);

    if (!nodes.length) {
      dagCanvas.append(
        emptyState({
          icon: 'route',
          title: getState('activeTaskId') ? '该任务没有 DAG 结构' : '尚未选择任务',
          desc: getState('activeTaskId')
            ? '可能来自离线冒烟运行或旧版本记录，此类任务不包含节点级依赖信息。'
            : '提交需求后，这里会显示节点依赖与实时的执行状态。',
        })
      );
      return;
    }

    let newLayout;
    try {
      newLayout = layoutDag(nodes);
    } catch (err) {
      // 环等畸形输入：降级为提示，而不是让整个页面脚本挂掉
      dagCanvas.append(
        errorState({
          title: '依赖图无法渲染',
          message: err.message,
          onRetry: () => refreshCurrent(),
        })
      );
      return;
    }
    layout = newLayout;

    // 画布尺寸：随图变化，并留出边距供平移
    const padding = 28;
    const width = Math.max(layout.width + padding * 2, 640);
    const height = Math.max(layout.height + padding * 2, 260);
    dagSvg.setAttribute('viewBox', `0 0 ${width} ${height}`);
    dagSvg.setAttribute('width', String(width));
    dagSvg.setAttribute('height', String(height));
    dagCanvas.append(dagSvg);

    renderDag(dagSvg, layout, graph, {
      selected: selectedNode,
      onSelect: (id) => {
        selectedNode = selectedNode === id ? '' : id;
        paintDag();
        paintDetail();
      },
    });

    // 工具栏：缩放/适配（用 viewBox 实现，无第三方库）
    dagToolbar.append(
      iconButton('fit-screen', { label: '适配画布宽度', small: true, onClick: () => fitDag(width) }),
      iconButton('zoom-in', { label: '放大', small: true, onClick: () => zoomDag(1.25) }),
      iconButton('zoom-out', { label: '缩小', small: true, onClick: () => zoomDag(0.8) }),
    );
  }

  let dagZoom = 1;

  function zoomDag(factor) {
    dagZoom = Math.min(2.5, Math.max(0.5, dagZoom * factor));
    dagSvg.style.width = `${dagZoom * 100}%`;
    dagSvg.style.height = 'auto';
  }

  function fitDag(width) {
    dagZoom = 1;
    dagSvg.style.width = '';
    dagSvg.style.height = '';
    // 窄图居中，宽图允许横滑
    dagCanvas.scrollLeft = width > dagCanvas.clientWidth
      ? (width - dagCanvas.clientWidth) / 2
      : 0;
  }

  /* ---------- 节点详情 ---------- */

  function paintDetail() {
    clear(detailBody);

    if (!selectedNode || !graph.has(selectedNode)) {
      detailBody.append(
        emptyState({
          small: true,
          icon: 'target',
          title: '未选择节点',
          desc: '点击 DAG 中的节点查看该步骤详情与代码改动',
        })
      );
      return;
    }

    const node = graph.get(selectedNode);
    const changes = node.agentType === 'coder' ? extractChanges(taskDetail, selectedNode) : [];

    // 头部
    const headRow = el('div', { style: { 'margin-bottom': 'var(--space-3)' } }, [
      el('div', { style: { display: 'flex', 'align-items': 'center', gap: 'var(--space-2)', 'flex-wrap': 'wrap' } }, [
        el('span', { class: 'mono', text: selectedNode, style: { 'font-weight': '600', 'font-size': 'var(--fs-sm)' } }),
        agentTag(node.agentType),
        statusBadge(node.status),
      ]),
    ]);
    detailBody.append(headRow);

    // 键值
    const kv = el('div', { class: 'kv', style: { 'margin-bottom': 'var(--space-4)' } });
    kv.append(
      kvItem('尝试次数', String(node.attempt || 1)),
      kvItem('token', node.tokens ? fmtInt(node.tokens) : '—'),
      kvItem('依赖', node.deps?.length ? node.deps.join('、') : '无（起始节点）')
    );
    detailBody.append(kv);

    if (node.goal) {
      detailBody.append(
        el('div', { style: { 'margin-bottom': 'var(--space-3)' } }, [
          el('div', { class: 'kv-key', text: '节点目标' }),
          el('div', { class: 'kv-value', text: node.goal, style: { 'font-size': 'var(--fs-xs)' } }),
        ])
      );
    }

    if (node.lastError) {
      detailBody.append(
        alert({ tone: 'danger', title: '最近一次错误', body: node.lastError })
      );
    }

    // 代码改动
    if (node.agentType === 'coder') {
      detailBody.append(
        el('div', { class: 'section-head', style: { 'margin-top': 'var(--space-4)' } }, [
          el('h3', { text: '代码改动', style: { 'font-size': 'var(--fs-sm)' } }),
        ])
      );
      if (changes.length) {
        for (const c of changes) detailBody.append(renderChange(c));
      } else {
        detailBody.append(
          el('p', {
            class: 'hint-text',
            text: '该节点暂无结构化 diff。任务可能尚未完成，或模型输出未按约定的 ```diff 围栏格式给出。',
          })
        );
      }
    }

    // 原始输出（可折叠）
    const step = findStep(taskDetail, selectedNode);
    if (step?.output) {
      detailBody.append(buildRawOutput(step.output));
    }
  }

  /* ---------- 原始输出折叠块 ---------- */

  function buildRawOutput(text) {
    const id = `raw-${Math.random().toString(36).slice(2, 8)}`;
    const wrap = el('details', { style: { 'margin-top': 'var(--space-4)' } });
    const summary = el('summary', {
      text: `原始输出（${fmtInt(text.length)} 字符）`,
      style: {
        cursor: 'pointer',
        'font-size': 'var(--fs-xs)',
        color: 'var(--text-secondary)',
        padding: 'var(--space-2) 0',
      },
    });
    const pre = el('pre', { class: 'code', attrs: { id } });
    pre.textContent = text;
    const copyBtn = button('复制', {
      icon: 'copy',
      variant: 'ghost',
      small: true,
      onClick: async () => {
        const ok = await copyText(text);
        toast({
          tone: ok ? 'success' : 'error',
          title: ok ? '已复制到剪贴板' : '复制失败',
          duration: 1600,
        });
      },
    });
    wrap.append(summary, pre, el('div', { style: { 'margin-top': 'var(--space-2)' } }, [copyBtn]));
    return wrap;
  }

  /* ---------- 时间线 ---------- *
   *
   * 工具条与列表**分开重建**。
   *
   * 早先两者耦合在一个 paintTimeline() 里，导致切换"自动滚动"开关这种
   * 纯 UI 状态的操作会 clear 掉整个列表再重建 400 个节点 —— 滚动位置丢失、
   * 视觉闪一下、用户以为列表被重置了。现在：
   *   - paintTimelineTools()：只重建工具条（按钮的 aria-pressed 等）
   *   - paintTimelineItems()：只重建列表
   *   - appendTimelineItem()：增量追加，不动已有节点
   */

  /** 只重建工具条。计数等易变文案走 paintTimelineMeta() 就地更新。 */
  function paintTimelineTools() {
    clear(timelineTools);

    const seg = el('div', { class: 'segmented', attrs: { role: 'group', 'aria-label': '事件过滤' } });
    const filters = [
      ['all', '全部'],
      ['node', '节点'],
      ['verdict', '验证'],
    ];
    const filterBtns = [];
    for (const [key, label] of filters) {
      const b = el('button', {
        attrs: { type: 'button', 'aria-pressed': String(timelineFilter === key) },
        text: label,
        on: {
          click: () => {
            if (timelineFilter === key) return;
            timelineFilter = key;
            // 只更新按钮态 + 重建列表，不碰工具条其余部分
            for (const [k, btn] of filterBtns) {
              btn.setAttribute('aria-pressed', String(k === key));
            }
            paintTimelineItems({ preserveScroll: false });
          },
        },
      });
      filterBtns.push([key, b]);
      seg.append(b);
    }

    const autoBtn = el('button', {
      class: 'btn btn-ghost btn-sm',
      attrs: { type: 'button', 'aria-pressed': String(autoScroll) },
      on: {
        click: () => {
          autoScroll = !autoScroll;
          // 就地改按钮，不重建任何列表 —— 这是本次修复的核心
          autoBtn.setAttribute('aria-pressed', String(autoScroll));
          clear(autoBtn);
          autoBtn.append(fromHTML(icon(autoScroll ? 'pause' : 'play', 13)));
          autoBtn.append(el('span', { text: autoScroll ? '已跟随' : '已暂停' }));
          if (autoScroll) scrollTimelineToEnd();
        },
      },
    });
    autoBtn.append(fromHTML(icon(autoScroll ? 'pause' : 'play', 13)));
    autoBtn.append(el('span', { text: autoScroll ? '已跟随' : '已暂停' }));

    countLabel = el('span', { class: 'hint-text' });

    timelineTools.append(
      seg,
      el('div', { class: 'toolbar-spacer' }),
      countLabel,
      autoBtn
    );
    paintTimelineMeta();
  }

  /** 就地更新计数（不重建任何节点）。 */
  function paintTimelineMeta() {
    if (!countLabel) return;
    const total = events.length;
    const shown = countVisible();
    countLabel.textContent = shown === total
      ? `${fmtInt(total)} 条`
      : `${fmtInt(shown)} / ${fmtInt(total)} 条`;
    countLabel.title = shown === total
      ? '已记录的全部事件'
      : `当前过滤条件下显示 ${shown} 条，共收到 ${total} 条`;
  }

  function countVisible() {
    if (timelineFilter === 'all') return events.length;
    return events.filter(matchFilter).length;
  }

  function matchFilter(ev) {
    if (timelineFilter === 'node') return ev.kind.startsWith('node');
    if (timelineFilter === 'verdict') return ev.kind === 'node_verdict';
    return true;
  }

  /** 重建整个时间线（切任务、任务结束对齐时用）。 */
  function paintTimeline() {
    paintTimelineTools();
    paintTimelineItems({ preserveScroll: false });
  }

  /**
   * 重建列表。
   * @param {object} [opts]
   * @param {boolean} [opts.preserveScroll] 是否保留当前滚动位置
   */
  function paintTimelineItems({ preserveScroll = true } = {}) {
    const prevTop = preserveScroll ? timelineEl.scrollTop : null;
    clear(timelineEl);

    if (!events.length) {
      timelineEl.append(
        emptyState({
          icon: 'history',
          title: getState('activeTaskId') ? '等待事件…' : '尚未选择任务',
          desc: getState('activeTaskId')
            ? '任务执行过程中的节点开始、完成与验证判定会实时出现在这里。'
            : '选择一个任务即可查看它的执行轨迹。',
        })
      );
      paintTimelineMeta();
      return;
    }

    const filtered = events.filter(matchFilter);
    if (!filtered.length) {
      timelineEl.append(
        emptyState({ small: true, icon: 'filter', title: '没有匹配的事件', desc: '试试切换到「全部」' })
      );
      paintTimelineMeta();
      return;
    }

    // DocumentFragment 批量插入：一次重排而不是 N 次
    const frag = document.createDocumentFragment();
    for (const ev of filtered) frag.append(timelineItem(ev));
    timelineEl.append(frag);

    if (prevTop != null) timelineEl.scrollTop = prevTop;
    else if (autoScroll) scrollTimelineToEnd();
    paintTimelineMeta();
  }

  /**
   * 滚动到底部。
   *
   * 尊重 `prefers-reduced-motion`：对前庭敏感的用户，元素的自动位移
   * 会引发不适。用即时跳转（behavior:'auto'）而不是平滑滚动。
   */
  function scrollTimelineToEnd() {
    if (prefersReducedMotion()) {
      timelineEl.scrollTop = timelineEl.scrollHeight;
      return;
    }
    timelineEl.scrollTo({ top: timelineEl.scrollHeight, behavior: 'smooth' });
  }

  /**
   * 增量追加一条事件（不重建已有节点）。
   * @param {object} ev
   * @param {{fromReplay?: boolean}} [opts] 事件是否来自历史回放
   */
  function appendTimelineItem(ev, { fromReplay = false } = {}) {
    // 首次追加时移除空态
    if (timelineEl.querySelector('.empty-state')) clear(timelineEl);

    if (!matchFilter(ev)) return;

    timelineEl.append(timelineItem(ev, { fromReplay }));

    // 回放期间不逐条滚动：几百条历史事件会触发几百次滚动计算，
    // 用户只看到画面抖动。回放结束后由 paintTimeline() 统一滚到底。
    if (autoScroll && !fromReplay) scrollTimelineToEnd();
  }

  /* ---------- 执行摘要 ---------- */

  function paintSummary(detail) {
    clear(summaryEl);

    const stats = [
      ['状态', statusLabel(detail.status)],
      ['耗时', fmtDuration(detail.duration_ms)],
      ['token', fmtInt(detail.total_tokens)],
      ['成本', fmtCost(detail.total_cost_usd)],
      ['步骤数', fmtInt((detail.steps || []).length)],
      ['节点数', fmtInt((detail.nodes || []).length)],
    ];

    const body = el('div');
    const grid = el('div', {
      class: 'grid grid-cols-3',
      style: { gap: 'var(--space-4)' },
    });
    for (const [k, v] of stats) {
      grid.append(
        el('div', {}, [
          el('div', { class: 'kv-key', text: k }),
          el('div', { class: 'kv-value tnum', text: v }),
        ])
      );
    }
    body.append(grid);

    if (detail.error) {
      body.append(
        el('div', { style: { 'margin-top': 'var(--space-3)' } }, [
          alert({ tone: 'danger', title: '执行失败', body: detail.error }),
        ])
      );
    }

    const actions = [];
    if (detail.status === 'running' || detail.status === 'pending') {
      actions.push(
        button('取消任务', {
          icon: 'stop',
          variant: 'secondary',
          small: true,
          onClick: () => cancelCurrent(detail.task_id),
        })
      );
    }
    actions.push(
      button('删除任务', {
        icon: 'trash',
        variant: 'ghost',
        small: true,
        onClick: () => deleteCurrent(detail.task_id),
      })
    );

    summaryEl.append(
      card({
        title: '执行摘要',
        subtitle: `任务 ${shortId(detail.task_id)}`,
        actions,
        body,
      })
    );
  }

  async function cancelCurrent(taskId) {
    try {
      await api.cancelTask(taskId);
      toast({ tone: 'info', title: '已发送取消请求' });
      await refreshCurrent();
      await loadTasks();
    } catch (err) {
      toast({ tone: 'error', title: '取消失败', desc: err?.message || String(err) });
    }
  }

  async function deleteCurrent(taskId) {
    const ok = await confirmDialog({
      title: '删除任务',
      message: `确定要删除任务 ${shortId(taskId)} 吗？该操作不可撤销，相关的执行记录与上下文指标也会一并移除。`,
      confirmLabel: '删除',
      danger: true,
    });
    if (!ok) return;

    try {
      await api.deleteTask(taskId);
      toast({ tone: 'success', title: '任务已删除' });
      set({ activeTaskId: '' });
      closeStream();
      graph = new Map();
      taskDetail = null;
      events.length = 0;
      paintDag();
      paintTimeline();
      mount(summaryEl);
      await loadTasks();
    } catch (err) {
      toast({ tone: 'error', title: '删除失败', desc: err?.message || String(err) });
    }
  }

  /* ---------- 初始化 ---------- */

  dagLegend.append(...buildLegend());

  goalInput.addEventListener('input', debounce(() => {
    goalInput.removeAttribute('aria-invalid');
  }, 300));

  await loadTasks();

  // 优先使用 URL 中的 task 参数；否则选第一个任务
  const initialTask = ctx?.params?.get?.('task') || '';
  const tasks = getState('tasks') || [];
  const target = tasks.some((t) => t.task_id === initialTask)
    ? initialTask
    : tasks[0]?.task_id;
  if (target) await selectTask(target);
  else {
    paintDag();
    paintTimeline();
    mount(detailBody, emptyState({
      small: true,
      icon: 'target',
      title: '未选择节点',
      desc: '提交或选择一个任务后，点击 DAG 节点查看详情',
    }));
  }

  return () => {
    closeStream();
    disposers.forEach((d) => d());
  };
}

/* ------------------------------------------------------------------ *
 * 纯函数助手
 * ------------------------------------------------------------------ */

function shortId(id) {
  return String(id || '').slice(0, 8);
}

/** 构建 DAG 图例。与 graph.js 的 LEGEND_ITEMS 同源，保证颜色一致。 */
function buildLegend() {
  return LEGEND_ITEMS.map((i) =>
    el('span', {}, [
      el('span', { class: `dot dot-sm dot-${i.group}` }),
      el('span', { text: i.label }),
    ])
  );
}

const EVENT_LABELS = {
  task_started: '任务开始',
  task_finished: '任务结束',
  task_cancelled: '任务取消',
  node_started: '节点开始',
  node_finished: '节点完成',
  node_verdict: '验证判定',
};

function eventTone(ev) {
  if (ev.kind === 'node_verdict') return ev.verdict === 'pass' ? 'ok' : 'fail';
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

function eventTitle(ev) {
  const label = EVENT_LABELS[ev.kind] || ev.kind;
  if (ev.node_id) {
    return `${label} · ${ev.node_id}${ev.agent_type ? ` (${ev.agent_type})` : ''}`;
  }
  return label;
}

/**
 * 单条事件。
 * @param {object} ev
 * @param {{fromReplay?: boolean}} [opts]
 *
 * `fromReplay` 表示这条是服务端补发的历史（重连或切任务时的回放），
 * 不是刚刚发生的。给它一个淡化的视觉标记 —— 用户需要能区分
 * "我正在看直播"和"这是补的课"，否则会误以为任务又跑了一遍。
 */
function timelineItem(ev, { fromReplay = false } = {}) {
  const tone = eventTone(ev);
  const item = el('li', {
    class: `timeline-item tone-${tone}${fromReplay ? ' timeline-item-replay' : ''}`,
    ...(fromReplay ? { attrs: { title: '历史事件回放' } } : {}),
  });

  item.append(el('span', { class: 'timeline-time', text: fmtTime(ev.timestamp) }));

  const content = el('div');
  const title = el('div', { class: 'timeline-title' });
  title.append(el('span', { text: eventTitle(ev) }));
  if (ev.kind === 'node_verdict') {
    title.append(
      ev.verdict === 'pass'
        ? badge('success', '通过', 'check')
        : badge('danger', '驳回', 'close')
    );
  }
  content.append(title);

  // 附加字段（已在标题展示的不重复）
  const skip = new Set(['kind', 'task_id', 'timestamp', 'node_id', 'agent_type', 'verdict']);
  const fields = Object.entries(ev).filter(
    ([k, v]) => !skip.has(k) && v !== '' && v != null && !(Array.isArray(v) && !v.length)
  );
  if (fields.length) {
    const row = el('div', { class: 'timeline-fields' });
    for (const [k, v] of fields) {
      let val;
      if (Array.isArray(v)) val = v.join('；');
      else if (typeof v === 'object') val = JSON.stringify(v);
      else val = v;
      row.append(
        el('span', { class: 'ev-field' }, [
          el('b', { text: k }),
          ` ${String(val).slice(0, 200)}`,
        ])
      );
    }
    content.append(row);
  }

  item.append(content);
  return item;
}

/**
 * 从步骤输出里抽出 ```diff 围栏块。
 *
 * step_id 必须**后缀匹配**：真实数据的 step_id 形如
 * `task_cea6348d2798:N1`（带 task 前缀），而节点 id 是 `N1`。
 * 早期用精确相等导致 diff 面板恒为空。
 *
 * 回退重跑时同一节点的多个 attempt 会被收进来，按 (文件, 正文) 去重
 * 并保留最后一次 —— 那才是最终落地的代码。
 */
function extractChanges(detail, nodeId) {
  const steps = Array.isArray(detail?.steps) ? detail.steps : [];
  const suffix = `:${nodeId}`;
  const matches = (s) => {
    const id = String(s.step_id || '');
    return id === nodeId || id.endsWith(suffix);
  };
  const mine = steps.filter(matches);
  const pool = mine.length ? mine : steps.filter((s) => s.agent === 'coder');

  const seen = new Map();
  const ordered = [...pool].sort((a, b) => (a.attempt || 1) - (b.attempt || 1));

  for (const step of ordered) {
    const text = String(step.output || '');
    const re = /```diff\r?\n([\s\S]*?)```/g;
    let m;
    while ((m = re.exec(text)) !== null) {
      const body = m[1].replace(/\s+$/, '');
      const file = guessFile(text, body, step);
      seen.set(`${file}\u0000${body}`, {
        file,
        reason: guessReason(text, body),
        diff: body,
        attempt: step.attempt || 1,
      });
    }
    // 没有 diff 围栏时退化为概述，避免面板完全空白
    if (!/```diff/.test(text) && text.trim()) {
      seen.set(`step\u0000${step.step_id}`, {
        file: `step ${step.step_id || ''}`.trim(),
        reason: '',
        diff: '',
        note: text.slice(0, 400),
        attempt: step.attempt || 1,
      });
    }
  }
  return [...seen.values()];
}

/** 找到节点对应的步骤。 */
function findStep(detail, nodeId) {
  const steps = Array.isArray(detail?.steps) ? detail.steps : [];
  const suffix = `:${nodeId}`;
  return steps.find((s) => {
    const id = String(s.step_id || '');
    return id === nodeId || id.endsWith(suffix);
  }) || null;
}

/**
 * 推定改动涉及的文件名。三级回退 —— 真实数据的 Coder 输出里 diff 围栏
 * 不含 `+++ b/path` 头，文件名只出现在上一行的 Markdown 标题里。
 */
function guessFile(outputText, diffBody, step) {
  const plus = /^\+\+\+ [ab]\/(.+)$/m.exec(diffBody);
  if (plus) return plus[1].trim();
  const minus = /^--- [ab]\/(.+)$/m.exec(diffBody);
  if (minus) return minus[1].trim();

  const idx = outputText.indexOf(diffBody);
  if (idx >= 0) {
    const headings = [...outputText.slice(0, idx).matchAll(/^##\s*\d+\.\s*(.+)$/gm)];
    if (headings.length) return headings[headings.length - 1][1].trim();
  }
  return `step ${step.step_id || ''}`.trim();
}

/** 回捞该 diff 所属改动块的「理由：」行。取不到返回空串 —— 不编造内容。 */
function guessReason(outputText, diffBody) {
  const idx = outputText.indexOf(diffBody);
  if (idx < 0) return '';
  const reason = [...outputText.slice(0, idx).matchAll(/^理由：(.+)$/gm)].pop();
  return reason ? reason[1].trim() : '';
}

/** 渲染单个文件的改动。 */
function renderChange(change) {
  const { add, del } = diffStats(change.diff);
  const wrap = el('div', { class: 'diff-file' });

  const head = el('div', { class: 'diff-file-head' });
  head.append(el('span', { text: change.file, attrs: { title: change.file } }));
  if (change.diff) {
    const stat = el('span', { style: { display: 'flex', gap: 'var(--space-2)', flex: 'none' } }, [
      el('span', { class: 'diff-stat-add', text: `+${add}` }),
      el('span', { class: 'diff-stat-del', text: `−${del}` }),
    ]);
    head.append(stat);
  }
  wrap.append(head);

  if (change.reason) {
    wrap.append(el('div', { class: 'diff-file-reason', text: change.reason }));
  }

  if (change.diff) {
    const bodyEl = el('div', { class: 'diff-body' });
    for (const line of parseDiff(change.diff)) bodyEl.append(diffLine(line));
    wrap.append(bodyEl);
  } else {
    wrap.append(
      el('div', { class: 'diff-file-reason', text: change.note || '（无 diff 内容）' })
    );
  }

  return wrap;
}

function diffLine(line) {
  const prefix = { add: '+', del: '-', ctx: ' ' }[line.type] || '';
  const row = el('div', { class: `diff-line ${line.type}` });
  row.append(el('span', { class: 'ln', text: line.oldNo == null ? '' : String(line.oldNo) }));
  row.append(el('span', { class: 'ln', text: line.newNo == null ? '' : String(line.newNo) }));
  row.append(el('span', { class: 'lt', text: prefix + line.text }));
  return row;
}

export { esc, fmtRelative };
