/**
 * 基础设施层：DOM 助手、格式化、存储、事件总线。
 *
 * 这一层刻意**不依赖任何其他前端模块**，避免循环依赖。
 * 上层（api / components / pages）都建立在它之上。
 */

/* ------------------------------------------------------------------ *
 * DOM
 * ------------------------------------------------------------------ */

/** 按 id 取元素。 */
export function $(id) {
  return document.getElementById(id);
}

/**
 * 查询。`root` 默认 document，便于在组件内部做局部查询。
 * @param {string} sel CSS 选择器
 * @param {ParentNode} [root=document]
 */
export function qs(sel, root = document) {
  return root.querySelector(sel);
}

/** 查询全部，返回真数组（便于直接 .map/.filter）。 */
export function qsa(sel, root = document) {
  return Array.from(root.querySelectorAll(sel));
}

/**
 * 创建元素。
 *
 * 支持 `{ class, text, html, attrs, dataset, on, children }` 简写，
 * 目的是让渲染代码保持声明式而不必反复 `document.createElement`。
 *
 * 安全约定：`html` 明确表示「调用方保证内容是安全的」。凡是拼接用户数据
 * 的场景**必须**用 `text`，或先经 `esc()` 转义 —— 这是本项目的 XSS 基线。
 */
export function el(tag, opts = {}, children = []) {
  const node = document.createElement(tag);

  if (opts.class) node.className = opts.class;
  if (opts.text != null) node.textContent = String(opts.text);
  if (opts.html != null) node.innerHTML = opts.html;

  if (opts.attrs) {
    for (const [k, v] of Object.entries(opts.attrs)) {
      if (v === false || v == null) continue;
      node.setAttribute(k, v === true ? '' : String(v));
    }
  }

  if (opts.dataset) {
    for (const [k, v] of Object.entries(opts.dataset)) {
      if (v != null) node.dataset[k] = String(v);
    }
  }

  if (opts.style) {
    for (const [k, v] of Object.entries(opts.style)) {
      if (v != null) node.style.setProperty(k, String(v));
    }
  }

  if (opts.on) {
    for (const [evt, handler] of Object.entries(opts.on)) {
      node.addEventListener(evt, handler);
    }
  }

  for (const child of children.flat()) {
    if (child == null || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }

  return node;
}

/** 用 HTML 字符串创建节点（仅用于可信模板，如 icons 输出）。 */
export function fromHTML(html) {
  if (!html) return document.createDocumentFragment();
  const tpl = document.createElement('template');
  tpl.innerHTML = html.trim();
  return tpl.content.firstElementChild || document.createDocumentFragment();
}

/** 清空元素。 */
export function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
  return node;
}

/** 替换元素内容（清空 + 追加）。 */
export function mount(node, ...children) {
  clear(node);
  for (const c of children.flat()) {
    if (c == null || c === false) continue;
    node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return node;
}

/* ------------------------------------------------------------------ *
 * 转义 —— 所有进入 innerHTML 的动态文本都必须先过这一层
 * ------------------------------------------------------------------ */

const ESC_MAP = {
  '&': '&amp;',
  '<': '&lt;',
  '>': '&gt;',
  '"': '&quot;',
  "'": '&#39;',
};

/** HTML 转义。 */
export function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) => ESC_MAP[c]);
}

/* ------------------------------------------------------------------ *
 * 格式化
 * ------------------------------------------------------------------ */

/** 千分位整数。 */
export function fmtInt(n) {
  const v = Number(n);
  if (!Number.isFinite(v)) return '—';
  return Math.round(v).toLocaleString('zh-CN');
}

/**
 * 紧凑数字：1234 → 1.2k，1234567 → 1.2M。
 * 用于指标卡等空间紧张处；表格里仍用完整数字（精确性优先）。
 */
export function fmtCompact(n) {
  const v = Number(n);
  if (!Number.isFinite(v)) return '—';
  const abs = Math.abs(v);
  if (abs >= 1e9) return `${(v / 1e9).toFixed(1)}B`;
  if (abs >= 1e6) return `${(v / 1e6).toFixed(1)}M`;
  if (abs >= 1e3) return `${(v / 1e3).toFixed(1)}k`;
  return String(Math.round(v));
}

/** 百分比。`digits` 默认 1 位小数；输入是 0–1 的比值。 */
export function fmtPct(ratio, digits = 1) {
  const v = Number(ratio);
  if (!Number.isFinite(v)) return '—';
  return `${(v * 100).toFixed(digits)}%`;
}

/** 美元成本。小额保留 4 位，大额 2 位 —— 否则 $0.0001 会显示成 $0.00。 */
export function fmtCost(usd) {
  const v = Number(usd ?? 0);
  if (!Number.isFinite(v)) return '—';
  if (v === 0) return '$0';
  if (v < 0.01) return `$${v.toFixed(4)}`;
  if (v < 1) return `$${v.toFixed(3)}`;
  return `$${v.toFixed(2)}`;
}

/** 时长：毫秒 → 人类可读。 */
export function fmtDuration(ms) {
  const v = Number(ms);
  if (!Number.isFinite(v) || v < 0) return '—';
  if (v < 1000) return `${Math.round(v)} ms`;
  if (v < 60_000) return `${(v / 1000).toFixed(2)} s`;
  const min = Math.floor(v / 60_000);
  const sec = ((v % 60_000) / 1000).toFixed(0);
  return `${min}m ${sec}s`;
}

/** 时钟时间。 */
export function fmtTime(ts) {
  if (!ts) return '';
  // 服务端时间戳可能是秒（SSE 事件）或毫秒（Date.now）
  const ms = ts > 1e12 ? ts : ts * 1000;
  const d = new Date(ms);
  if (Number.isNaN(d.getTime())) return '';
  return d.toLocaleTimeString('zh-CN', { hour12: false });
}

/** 相对时间：3 分钟前。 */
export function fmtRelative(ts) {
  if (!ts) return '';
  const ms = ts > 1e12 ? ts : ts * 1000;
  const diff = Date.now() - ms;
  if (!Number.isFinite(diff)) return '';
  if (diff < 5_000) return '刚刚';
  if (diff < 60_000) return `${Math.floor(diff / 1000)} 秒前`;
  if (diff < 3_600_000) return `${Math.floor(diff / 60_000)} 分钟前`;
  if (diff < 86_400_000) return `${Math.floor(diff / 3_600_000)} 小时前`;
  if (diff < 2_592_000_000) return `${Math.floor(diff / 86_400_000)} 天前`;
  return new Date(ms).toLocaleDateString('zh-CN');
}

/* ------------------------------------------------------------------ *
 * 键盘 / 焦点 —— 可访问性的底层工具
 * ------------------------------------------------------------------ */

/** 可获得焦点的元素选择器（用于焦点陷阱）。 */
const FOCUSABLE = [
  'a[href]',
  'button:not([disabled])',
  'input:not([disabled]):not([type="hidden"])',
  'select:not([disabled])',
  'textarea:not([disabled])',
  '[tabindex]:not([tabindex="-1"])',
].join(',');

/** 取容器内所有可聚焦元素（过滤掉不可见的）。 */
export function focusableWithin(root) {
  return qsa(FOCUSABLE, root).filter((n) => {
    if (n.hasAttribute('hidden')) return false;
    if (n.getAttribute('aria-hidden') === 'true') return false;
    // offsetParent 为 null 说明被 display:none 隐藏（fixed 元素除外）
    return n.offsetParent !== null || getComputedStyle(n).position === 'fixed';
  });
}

/**
 * 焦点陷阱：把 Tab 键限制在容器内。
 *
 * 为何必须：没有焦点陷阱的弹层对键盘用户等同于「黑洞」——
 * Tab 会跑到被遮挡的背景内容上，用户完全失去位置感。
 *
 * @returns {() => void} 解除函数
 */
export function trapFocus(container) {
  const onKeydown = (e) => {
    if (e.key !== 'Tab') return;
    const items = focusableWithin(container);
    if (!items.length) {
      e.preventDefault();
      return;
    }
    const first = items[0];
    const last = items[items.length - 1];
    const active = document.activeElement;

    if (e.shiftKey && (active === first || !container.contains(active))) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && active === last) {
      e.preventDefault();
      first.focus();
    }
  };
  container.addEventListener('keydown', onKeydown);
  return () => container.removeEventListener('keydown', onKeydown);
}

/**
 * 打开弹层时的通用处理：记住来源焦点、加滚动锁、监听 Esc。
 * 返回的清理函数负责**把焦点还给来源元素** —— 这是键盘用户
 * 关闭弹层后「回到哪里」的关键，遗漏会让人迷失。
 */
export function activateModal(container, { onEscape } = {}) {
  const previous = document.activeElement;
  const scrollbarWidth = window.innerWidth - document.documentElement.clientWidth;

  document.documentElement.style.setProperty('--scrollbar-comp', `${scrollbarWidth}px`);
  document.body.dataset.modalOpen = 'true';

  const onKey = (e) => {
    if (e.key === 'Escape' && onEscape) {
      e.stopPropagation();
      onEscape();
    }
  };
  document.addEventListener('keydown', onKey);
  const releaseTrap = trapFocus(container);

  // 让屏幕阅读器聚焦到弹层，并朗读其 aria-label/标题
  const target = focusableWithin(container)[0] || container;
  target.focus?.({ preventScroll: true });

  return () => {
    document.removeEventListener('keydown', onKey);
    releaseTrap();
    delete document.body.dataset.modalOpen;
    document.documentElement.style.removeProperty('--scrollbar-comp');
    // 元素可能已被移除，恢复焦点前检查仍在文档中
    if (previous instanceof HTMLElement && document.contains(previous)) {
      previous.focus({ preventScroll: true });
    }
  };
}

/* ------------------------------------------------------------------ *
 * 存储 —— localStorage 的安全封装
 * 隐私模式 / 禁用 Cookie 时 localStorage 会抛异常，必须兜住，
 * 否则一个偏好读取失败会让整个应用启动失败。
 * ------------------------------------------------------------------ */

export const storage = {
  get(key, fallback = null) {
    try {
      const v = window.localStorage.getItem(key);
      return v == null ? fallback : v;
    } catch {
      return fallback;
    }
  },
  set(key, value) {
    try {
      window.localStorage.setItem(key, String(value));
      return true;
    } catch {
      return false;
    }
  },
  remove(key) {
    try {
      window.localStorage.removeItem(key);
    } catch {
      /* 忽略 */
    }
  },
};

/* ------------------------------------------------------------------ *
 * 杂项
 * ------------------------------------------------------------------ */

/** 防抖。 */
export function debounce(fn, wait = 200) {
  let t = 0;
  return (...args) => {
    window.clearTimeout(t);
    t = window.setTimeout(() => fn(...args), wait);
  };
}

/** 节流（尾部保证触发）。 */
export function throttle(fn, wait = 200) {
  let last = 0;
  let timer = 0;
  return (...args) => {
    const now = Date.now();
    const rest = wait - (now - last);
    if (rest <= 0) {
      window.clearTimeout(timer);
      timer = 0;
      last = now;
      fn(...args);
    } else if (!timer) {
      timer = window.setTimeout(() => {
        last = Date.now();
        timer = 0;
        fn(...args);
      }, rest);
    }
  };
}

/** 深取：`get(obj, 'a.b.c', fallback)`。 */
export function get(obj, path, fallback = undefined) {
  const parts = String(path).split('.');
  let cur = obj;
  for (const p of parts) {
    if (cur == null || typeof cur !== 'object') return fallback;
    cur = cur[p];
  }
  return cur === undefined ? fallback : cur;
}

/* ------------------------------------------------------------------ *
 * 指标名解析
 * ------------------------------------------------------------------ */

/**
 * 按**后缀**匹配指标。
 *
 * 为什么不用精确相等：指标的完整名可能带命名空间前缀
 * （如 `devagent_context_compression_ratio`），也可能不带
 * （如 `context_compression_ratio`）—— 取决于导出器的配置。
 * 用后缀匹配可以同时兼容两种形态，避免「改了一个配置项就白屏」。
 *
 * @param {Record<string,any>|undefined} map 指标字典
 * @param {string} suffix 后缀，如 'context_compression_ratio'
 * @returns {any} 匹配到的值，未匹配返回 undefined
 */
export function metric(map, suffix) {
  if (!map || typeof map !== 'object') return undefined;
  if (suffix in map) return map[suffix];
  const key = Object.keys(map).find((k) => k === suffix || k.endsWith(`_${suffix}`));
  return key === undefined ? undefined : map[key];
}

/** metric() 的键名版本：需要拿到实际键名时使用。 */
export function metricKey(map, suffix) {
  if (!map || typeof map !== 'object') return null;
  if (suffix in map) return suffix;
  return Object.keys(map).find((k) => k.endsWith(`_${suffix}`)) ?? null;
}

/** 把 `{标签串: 数值}` 求和。 */
export function sumLabels(map) {
  return Object.values(map || {}).reduce((s, v) => s + (Number(v) || 0), 0);
}

/**
 * 从直方图统计字典取均值。
 * 服务端的直方图可能只给 count/sum（未算 mean），也可能直接给 mean，
 * 两种都要支持。
 */
export function histMean(h) {
  if (!h || typeof h !== 'object') return null;
  if (typeof h.mean === 'number') return h.mean;
  if (typeof h.sum === 'number' && h.count) return h.sum / h.count;
  return null;
}

/**
 * 按标签分别求均值，再取加权平均。
 *
 * 直方图的典型结构是 `{ 'agent="coder"': {count,sum,p50...}, ... }`。
 * 直接取第一个标签的值会漏掉其他角色 —— 必须逐标签聚合。
 */
export function histOverallMean(h) {
  if (!h || typeof h !== 'object') return null;
  const single = histMean(h);
  if (single != null) return single;
  let sum = 0;
  let count = 0;
  for (const v of Object.values(h)) {
    const m = histMean(v);
    const c = typeof v?.count === 'number' ? v.count : 1;
    if (m == null) continue;
    sum += m * c;
    count += c;
  }
  return count ? sum / count : null;
}

/** 从直方图所有标签中取某个分位数的最大值（如最坏情况的 p90）。 */
export function histMaxQuantile(h, q = 'p90') {
  if (!h || typeof h !== 'object') return null;
  const vals = Object.values(h)
    .map((v) => (v && typeof v === 'object' ? Number(v[q]) : NaN))
    .filter((v) => Number.isFinite(v));
  return vals.length ? Math.max(...vals) : null;
}

/** 复制到剪贴板。返回是否成功 —— 旧浏览器/非安全上下文会失败。 */
export async function copyText(text) {
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      return true;
    }
    // 回退：file:// 或 http 非 localhost 场景没有 clipboard API
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.append(ta);
    ta.select();
    const ok = document.execCommand('copy');
    ta.remove();
    return ok;
  } catch {
    return false;
  }
}

/** 当前是否移动端视口（与 tokens.css 的断点保持一致）。 */
export function isMobile() {
  return window.matchMedia('(max-width: 639px)').matches;
}

/** 当前是否桌面端视口。 */
export function isDesktop() {
  return window.matchMedia('(min-width: 1024px)').matches;
}

/**
 * 系统是否偏好减少动效。
 * 动画决策必须查询这个而不是硬编码 —— CSS 已处理 transition/animation，
 * 但 JS 驱动的滚动、延时播放需要自己判断。
 */
export function prefersReducedMotion() {
  return window.matchMedia('(prefers-reduced-motion: reduce)').matches;
}

/** 下一个动画帧（Promise 化，便于时序控制）。 */
export function nextFrame() {
  return new Promise((r) => requestAnimationFrame(() => r()));
}

/**
 * 极简事件总线。用于跨模块通信（如主题切换、任务选择），
 * 避免模块间直接互相 import 造成耦合。
 */
export function createEmitter() {
  const map = new Map();
  return {
    on(type, fn) {
      if (!map.has(type)) map.set(type, new Set());
      map.get(type).add(fn);
      return () => map.get(type)?.delete(fn);
    },
    emit(type, payload) {
      map.get(type)?.forEach((fn) => {
        try {
          fn(payload);
        } catch (err) {
          // 单个订阅者出错不应阻断其他订阅者
          console.error(`[bus] ${type} 处理器异常`, err);
        }
      });
    },
  };
}
