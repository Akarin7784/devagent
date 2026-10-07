/**
 * 共享 UI 组件（声明式构造函数）。
 *
 * 约定：每个 `xxx()` 返回一个 DOM 节点，而不是 HTML 字符串。
 * 字符串模板容易漏转义，且无法复用事件绑定；返回节点则天然安全。
 * 唯一例外是 `icon()`（来自 icons.js），它生成的是纯静态 SVG，无注入面。
 */

import { icon } from './icons.js';
import { agentLabel, statusDotClass, taskStatusBadge } from './status.js';
import { el, esc, fromHTML, activateModal, mount } from './util.js';

// 重新导出，保持既有调用方的 import 路径不变（多个页面从 components.js 取）。
export { agentLabel, statusDotClass };

/** 把 icon() 的 SVG 字符串转成可选中的节点。 */
export function iconEl(name, size = 16, className = '') {
  if (!name) return document.createDocumentFragment();
  const svg = icon(name, size, className);
  return svg ? fromHTML(svg) : document.createDocumentFragment();
}

/* ------------------------------------------------------------------ *
 * 徽章 / 状态
 * ------------------------------------------------------------------ */

/**
 * 状态徽章。语义色 + 图标，不单靠颜色传达状态（色盲可达性）。
 * @param {string} tone success|danger|warning|info|neutral|brand|running
 * @param {string} label
 * @param {string} [ico] 图标名
 * @param {'sm'|'md'} [size]
 */
export function badge(tone, label, ico = '', size = 'md') {
  const cls = ['badge', `badge-${tone}`];
  if (size === 'sm') cls.push('badge-sm-custom');
  const node = el('span', { class: cls.join(' ') });
  if (ico) node.append(iconEl(ico, 11));
  node.append(el('span', { text: label }));
  return node;
}

/**
 * 任务状态 → 徽章。
 *
 * 映射表在 `status.js`（全站唯一真源）。这里只做「取三元组 → 造 DOM」。
 * 之所以不在此处内置表：`graph.js` 也需要同一批状态语义，
 * 两处各存一份必然漂移 —— 曾经真的漂移过（缺 ready/verifying/rejected，
 * 界面回显英文枚举名）。
 */
export function statusBadge(status) {
  const [tone, label, ico] = taskStatusBadge(status);
  return badge(tone, label, ico);
}

/** Agent 角色标签。 */
export function agentTag(type) {
  return el('span', { class: 'tag', text: agentLabel(type) });
}

/* ------------------------------------------------------------------ *
 * 空态 / 错误态 / 加载骨架
 * ------------------------------------------------------------------ */

/**
 * 空态。
 * 每处空态都应有「为什么空 + 下一步做什么」，否则用户只看到一片虚无。
 */
export function emptyState({ icon: ico = 'inbox', title, desc, action, small = false } = {}) {
  const node = el('div', { class: `empty-state${small ? ' empty-state-sm' : ''}` });
  node.append(el('div', { class: 'empty-state-icon', html: icon(ico, small ? 16 : 22) }));
  if (title) node.append(el('div', { class: 'empty-state-title', text: title }));
  if (desc) node.append(el('div', { class: 'empty-state-desc', text: desc }));
  if (action) node.append(action);
  return node;
}

/**
 * 错误态。展示技术细节但**限制在一个可折叠区域**，
 * 兼顾「用户看得懂」与「开发者能排查」。
 */
export function errorState({ title = '加载失败', message = '', onRetry, small = false } = {}) {
  const node = el('div', { class: `error-state${small ? ' empty-state-sm' : ''}` });
  node.append(el('div', { class: 'error-state-icon', html: icon('alert-triangle', small ? 16 : 22) }));
  node.append(el('div', { class: 'error-state-title', text: title }));
  if (message) node.append(el('div', { class: 'error-state-desc', text: message }));
  if (onRetry) {
    node.append(
      el('button', {
        class: 'btn btn-secondary btn-sm',
        attrs: { type: 'button' },
        on: { click: onRetry },
      }, [iconEl('refresh', 13), '重试'])
    );
  }
  return node;
}

/** 骨架屏：统计卡。 */
export function skeletonStat() {
  return el('div', { class: 'stat-card' }, [
    el('div', { class: 'skeleton skeleton-text', style: { width: '45%' } }),
    el('div', { class: 'skeleton skeleton-stat' }),
    el('div', { class: 'skeleton skeleton-text', style: { width: '65%', 'margin-top': '10px' } }),
  ]);
}

/** 骨架屏：表格若干行。 */
export function skeletonTable(rows = 5, cols = 4) {
  const wrap = el('div', { style: { padding: 'var(--space-4)' } });
  for (let r = 0; r < rows; r += 1) {
    const row = el('div', {
      style: { display: 'flex', gap: 'var(--space-4)', 'margin-bottom': 'var(--space-4)' },
    });
    for (let c = 0; c < cols; c += 1) {
      row.append(
        el('div', {
          class: 'skeleton skeleton-text',
          style: { flex: c === 0 ? '2' : '1', 'margin-bottom': '0', height: '12px' },
        })
      );
    }
    wrap.append(row);
  }
  return wrap;
}

/** 骨架屏：图表区块。 */
export function skeletonBlock(height = 160) {
  return el('div', { class: 'skeleton skeleton-block', style: { height: `${height}px` } });
}

/**
 * 按加载态分发渲染。
 * 把「loading / error / empty / ready」四种分支收敛到一处，
 * 保证每个页面都不遗漏任何状态 —— 这是「加载与异常状态处理」的落地方式。
 */
export function renderByState(status, { loading, error, empty, ready, isEmpty, emptyArgs, onRetry, errTitle }) {
  if (status === 'loading') return loading ? loading() : skeletonBlock();
  if (status === 'error') {
    // error 既接受渲染函数，也接受字符串/Error（便于调用方直接透传异常信息）
    if (typeof error === 'function') return error();
    const msg = error instanceof Error ? error.message : (error || '');
    return errorState({ title: errTitle || '加载失败', message: msg, onRetry });
  }
  if (isEmpty) return empty ? empty(emptyArgs) : emptyState(emptyArgs || {});
  return typeof ready === 'function' ? ready() : document.createDocumentFragment();
}

/* ------------------------------------------------------------------ *
 * Toast
 * ------------------------------------------------------------------ */

let toastRegion = null;

function ensureToastRegion() {
  if (toastRegion && document.contains(toastRegion)) return toastRegion;
  toastRegion = el('div', {
    class: 'toast-region',
    attrs: { role: 'region', 'aria-label': '通知' },
  });
  document.body.append(toastRegion);
  return toastRegion;
}

/**
 * 弹出一条通知。
 *
 * 可达性要点：容器是 `aria-live="polite"`（在区域上声明），错误用
 * `role="alert"` 立即打断朗读。自动消失时间对错误放宽到 8s ——
 * 错误信息被秒速吞掉是常见的体验灾难。
 *
 * @param {object} opts
 * @param {'success'|'error'|'warning'|'info'} [opts.tone='info']
 * @param {string} opts.title
 * @param {string} [opts.desc]
 * @param {number} [opts.duration] 毫秒，0 表示不自动关闭
 */
export function toast({ tone = 'info', title, desc = '', duration } = {}) {
  const region = ensureToastRegion();
  const icoName = {
    success: 'check-circle',
    error: 'x-circle',
    warning: 'alert-triangle',
    info: 'info',
  }[tone] || 'info';

  const ttl = duration ?? (tone === 'error' ? 8000 : 4000);

  const node = el('div', {
    class: `toast toast-${tone}`,
    attrs: {
      role: tone === 'error' ? 'alert' : 'status',
      'aria-live': tone === 'error' ? 'assertive' : 'polite',
    },
  });

  node.append(el('div', { class: 'toast-icon', html: icon(icoName, 16) }));

  const body = el('div', { class: 'toast-body' }, [
    el('div', { class: 'toast-title', text: title }),
    desc ? el('div', { class: 'toast-desc', text: desc }) : null,
  ]);
  node.append(body);

  const close = el('button', {
    class: 'btn btn-ghost btn-icon btn-sm',
    attrs: { type: 'button', 'aria-label': '关闭通知' },
    on: { click: () => dismiss() },
  }, [iconEl('close', 13)]);
  node.append(close);

  let timer = 0;
  const dismiss = () => {
    window.clearTimeout(timer);
    node.dataset.leaving = 'true';
    node.addEventListener('animationend', () => node.remove(), { once: true });
    // 兜底：动画被禁用（prefers-reduced-motion）时 animationend 不触发
    window.setTimeout(() => node.remove(), 400);
  };

  region.append(node);
  // 悬停暂停自动关闭，让用户来得及读完
  node.addEventListener('mouseenter', () => window.clearTimeout(timer));
  node.addEventListener('mouseleave', () => {
    if (ttl > 0) timer = window.setTimeout(dismiss, 1200);
  });
  if (ttl > 0) timer = window.setTimeout(dismiss, ttl);

  return dismiss;
}

/* ------------------------------------------------------------------ *
 * 弹层：Modal / Drawer
 * ------------------------------------------------------------------ */

/**
 * 打开对话框。
 * @param {object} opts
 * @param {string} opts.title
 * @param {Node|string} opts.body
 * @param {Array<{label:string, tone?:string, onClick?:Function, closeOnClick?:boolean}>} [opts.actions]
 * @param {string} [opts.size] 'sm'|'md'|'lg'
 * @returns {{close: () => void, node: HTMLElement}}
 */
export function openModal({ title, body, actions = [], size = 'md' } = {}) {
  const overlay = el('div', { class: 'overlay', attrs: { role: 'presentation' } });
  const widths = { sm: 420, md: 560, lg: 800 };
  const modal = el('div', {
    class: 'modal',
    style: { width: `min(${widths[size] || 560}px, 100%)` },
    attrs: { role: 'dialog', 'aria-modal': 'true', 'aria-label': title, tabindex: '-1' },
  });

  const header = el('div', { class: 'modal-header' }, [
    el('h2', { text: title }),
  ]);
  const closeBtn = el('button', {
    class: 'btn btn-ghost btn-icon',
    attrs: { type: 'button', 'aria-label': '关闭对话框' },
    on: { click: () => close() },
  }, [iconEl('close', 15)]);
  header.append(closeBtn);

  const bodyNode = el('div', { class: 'modal-body' });
  if (typeof body === 'string') bodyNode.textContent = body;
  else if (body) bodyNode.append(body);

  modal.append(header, bodyNode);

  const close = () => {
    release();
    overlay.remove();
  };

  if (actions.length) {
    const footer = el('div', { class: 'modal-footer' });
    for (const a of actions) {
      footer.append(
        el('button', {
          class: `btn btn-${a.tone || 'secondary'}`,
          attrs: { type: 'button' },
          on: {
            click: () => {
              const keep = a.onClick?.();
              if (a.closeOnClick !== false && keep !== false) close();
            },
          },
        }, [a.label])
      );
    }
    modal.append(footer);
  }

  overlay.append(modal);
  overlay.addEventListener('mousedown', (e) => {
    // 只有点在遮罩本身（而非拖选到遮罩）才关闭
    if (e.target === overlay) close();
  });

  document.body.append(overlay);
  const release = activateModal(modal, { onEscape: close });

  return { close, node: modal };
}

/**
 * 打开右侧抽屉。用于节点详情等「与主内容并列查看」的场景，
 * 避免对话框遮挡它正在描述的主体。
 *
 * @returns {{close: () => void, body: HTMLElement, node: HTMLElement}}
 */
export function openDrawer({ title, subtitle = '', body } = {}) {
  const overlay = el('div', { class: 'drawer-overlay' });
  const drawer = el('aside', {
    class: 'drawer',
    attrs: { role: 'dialog', 'aria-modal': 'true', 'aria-label': title, tabindex: '-1' },
  });

  const header = el('div', { class: 'drawer-header' });
  const titleWrap = el('div', { style: { flex: '1', 'min-width': '0' } }, [
    el('h2', { text: title }),
    subtitle ? el('div', { class: 'hint-text', text: subtitle }) : null,
  ]);
  header.append(
    titleWrap,
    el('button', {
      class: 'btn btn-ghost btn-icon',
      attrs: { type: 'button', 'aria-label': '关闭详情面板' },
      on: { click: () => close() },
    }, [iconEl('close', 15)])
  );

  const bodyNode = el('div', { class: 'drawer-body' });
  if (typeof body === 'string') bodyNode.textContent = body;
  else if (body) bodyNode.append(body);

  drawer.append(header, bodyNode);

  const close = () => {
    release();
    overlay.remove();
    drawer.remove();
  };

  overlay.addEventListener('mousedown', close);
  document.body.append(overlay, drawer);
  const release = activateModal(drawer, { onEscape: close });

  return { close, body: bodyNode, node: drawer };
}

/**
 * 确认对话框（Promise 化）。用于删除等破坏性操作。
 * @returns {Promise<boolean>}
 */
export function confirmDialog({
  title = '确认操作',
  message = '',
  confirmLabel = '确认',
  cancelLabel = '取消',
  danger = false,
} = {}) {
  return new Promise((resolve) => {
    let settled = false;
    const finish = (value) => {
      if (settled) return;
      settled = true;
      resolve(value);
    };

    const body = el('div', { class: 'field' }, [
      el('p', { class: 'hint-text', text: message, style: { 'font-size': 'var(--fs-sm)' } }),
    ]);

    const { close } = openModal({
      title,
      body,
      size: 'sm',
      actions: [
        {
          label: cancelLabel,
          tone: 'ghost',
          onClick: () => finish(false),
        },
        {
          label: confirmLabel,
          tone: danger ? 'danger' : 'primary',
          onClick: () => finish(true),
        },
      ],
    });

    // 用户按 Esc 或点遮罩关闭时，需要解析为 false
    const observer = new MutationObserver(() => {
      if (!document.contains(document.querySelector('.overlay'))) {
        observer.disconnect();
        finish(false);
      }
    });
    observer.observe(document.body, { childList: true });
    void close;
  });
}

/* ------------------------------------------------------------------ *
 * 基础片段
 * ------------------------------------------------------------------ */

/** 卡片。 */
export function card({ title, subtitle, actions, body, flush = false, footer, id } = {}) {
  const node = el('section', { class: 'card', attrs: id ? { id } : {} });
  if (title || actions) {
    const head = el('div', { class: 'card-header' });
    const text = el('div', { style: { 'min-width': '0' } }, [
      el('h2', { text: title }),
      subtitle ? el('div', { class: 'card-subtitle', text: subtitle }) : null,
    ]);
    head.append(text);
    if (actions) {
      const wrap = el('div', { class: 'card-header-actions' });
      for (const a of [].concat(actions)) wrap.append(a);
      head.append(wrap);
    }
    node.append(head);
  }
  if (body) {
    const b = el('div', { class: `card-body${flush ? ' card-body-flush' : ''}` });
    if (typeof body === 'string') b.textContent = body;
    else b.append(body);
    node.append(b);
  }
  if (footer) {
    const f = el('div', { class: 'card-footer' });
    if (typeof footer === 'string') f.textContent = footer;
    else f.append(footer);
    node.append(f);
  }
  return node;
}

/** 统计卡。 */
export function statCard({ label, value, unit = '', icon: ico, foot, tone }) {
  const node = el('div', { class: 'stat-card' });
  const head = el('div', { class: 'stat-card-head' });
  if (ico) {
    const iconBox = el('div', { class: 'stat-card-icon', html: icon(ico, 15) });
    if (tone) {
      iconBox.style.background = `var(--status-${tone}-bg)`;
      iconBox.style.color = `var(--status-${tone})`;
    }
    head.append(iconBox);
  }
  head.append(el('div', { class: 'stat-card-label', text: label }));
  node.append(head);

  const valueRow = el('div', { class: 'stat-card-value' }, [
    el('span', { text: value }),
    unit ? el('span', { class: 'stat-card-unit', text: unit }) : null,
  ]);
  node.append(valueRow);

  if (foot) {
    const f = el('div', { class: 'stat-card-foot' });
    if (typeof foot === 'string') f.textContent = foot;
    else f.append(foot);
    node.append(f);
  }
  return node;
}

/** 键值对。 */
export function kvItem(key, value) {
  return el('div', { class: 'kv-item' }, [
    el('div', { class: 'kv-key', text: key }),
    el('div', { class: 'kv-value', text: value }),
  ]);
}

/** 指标行（名称 + 数值 + 可选进度条）。 */
export function metricRow({ name, value, ratio = null, tone = '' }) {
  const node = el('div');
  node.append(
    el('div', { class: 'metric-row-head' }, [
      el('span', { class: 'metric-name', text: name }),
      el('span', { class: 'metric-value', text: value }),
    ])
  );
  if (ratio != null) {
    const pct = Math.min(100, Math.max(0, Number(ratio) * 100));
    node.append(
      el('div', { class: 'bar' }, [
        el('div', {
          class: `bar-fill${tone ? ` bar-fill-${tone}` : ''}`,
          style: { width: `${pct}%` },
        }),
      ])
    );
  }
  return node;
}

/** 提示条。 */
export function alert({ tone = 'info', title, body, icon: ico }) {
  const icoName = ico || {
    info: 'info',
    warning: 'alert-triangle',
    danger: 'alert-circle',
    success: 'check-circle',
  }[tone];
  const node = el('div', { class: `alert alert-${tone}`, attrs: { role: 'note' } });
  node.append(el('div', { class: 'alert-icon', html: icon(icoName, 15) }));
  const b = el('div', { class: 'alert-body' });
  if (title) b.append(el('div', { class: 'alert-title', text: title }));
  if (body) {
    if (typeof body === 'string') b.append(el('div', { html: body }));
    else b.append(body);
  }
  node.append(b);
  return node;
}

/** 面板头部（用于任务工作台等分栏区）。 */
export function panelHead({ title, actions, legend }) {
  const head = el('div', { class: 'section-head' });
  head.append(el('h2', { text: title }));
  if (legend) {
    const lg = el('div', { class: 'legend' });
    lg.append(legend);
    head.append(lg);
  }
  if (actions) {
    const wrap = el('div', { class: 'section-head-actions' });
    for (const a of [].concat(actions)) wrap.append(a);
    head.append(wrap);
  }
  return head;
}

/** 图标按钮。 */
export function iconButton(name, { label, size = 16, variant = 'ghost', onClick, small = false, pressed } = {}) {
  const node = el('button', {
    class: `btn btn-${variant} btn-icon${small ? ' btn-sm' : ''}`,
    attrs: {
      type: 'button',
      'aria-label': label,
      title: label,
      ...(pressed === undefined ? {} : { 'aria-pressed': String(pressed) }),
    },
    on: onClick ? { click: onClick } : {},
  });
  node.append(iconEl(name, size));
  return node;
}

/** 文本按钮（带图标）。 */
export function button(label, { icon: ico, variant = 'secondary', onClick, small = false, disabled = false, type = 'button', loading = false } = {}) {
  const node = el('button', {
    class: `btn btn-${variant}${small ? ' btn-sm' : ''}`,
    attrs: { type, ...(disabled ? { disabled: true } : {}), ...(loading ? { 'data-loading': 'true', 'aria-busy': 'true' } : {}) },
    on: onClick ? { click: onClick } : {},
  });
  if (ico) node.append(iconEl(ico, small ? 13 : 14));
  node.append(el('span', { text: label }));
  return node;
}

export { mount, esc };
