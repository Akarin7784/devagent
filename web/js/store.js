/**
 * 应用状态容器。
 *
 * 取舍：**不引入状态管理库**，用一个「字段级订阅」的极简 store。
 *
 * 为什么不直接全局可变对象 + 手动重渲染：当多个面板共享同一份数据
 * （下面 `tasks` 同时被侧栏列表和 Dashboard 使用）时，手动同步迟早会漏。
 * 而引入 Redux/MobX 又需要构建步骤。折中方案是：
 *
 * - 状态集中在一处，只能通过 `set()` 修改；
 * - 订阅精确到**字段名**，避免「改 token 却重渲染整个 DAG」。
 *
 * 这足以覆盖本应用的复杂度，且零依赖。
 */

import { createEmitter, storage } from './util.js';

export const THEME_KEY = 'devagent.theme';
export const AUTO_REFRESH_KEY = 'devagent.autorefresh';

/** 初始状态。新增字段请同步补充到 JSDoc 里，方便定位。 */
const initial = {
  /** @type {'light'|'dark'|'system'} 主题偏好 */
  theme: storage.get(THEME_KEY, 'system'),
  /** @type {boolean} 系统当前是否为暗色（由 matchMedia 驱动） */
  systemDark: false,

  /** @type {boolean} 后端是否可达 */
  connected: false,
  /** @type {string} 健康检查摘要，如 "deepseek/qwen · 指标=开" */
  healthDetail: '连接中…',
  /** @type {string[]} 已启用的模型提供商 */
  providers: [],
  /** @type {boolean} 可观测性是否开启 */
  observability: false,
  /** @type {string} 版本号 */
  version: '',

  /** @type {Array} 任务列表（TaskListItem[]） */
  tasks: [],
  /** @type {string} 当前查看的任务 id */
  activeTaskId: '',
  /** @type {object|null} 当前任务详情（TaskView） */
  taskDetail: null,
  /** @type {Map<string,object>} 当前任务的图状态 */
  graph: new Map(),
  /** @type {string} 当前选中的 DAG 节点 */
  selectedNode: '',
  /** @type {Array} 当前任务的执行事件时间线 */
  timeline: [],
  /** @type {object|null} 当前任务的上下文指标 */
  contextMetrics: null,

  /** @type {object|null} 全局指标快照（MetricsResponse） */
  metrics: null,
  /** @type {object|null} 缓存统计 */
  cacheStats: null,
  /** @type {Array} 最近的 trace span */
  traces: [],

  /** @type {object|null} 最近一次评测结果（EvalSummaryView） */
  evalResult: null,
  /** @type {boolean} 评测是否运行中 */
  evalRunning: false,

  /** @type {Record<string, object>} 各页面的加载态：{ [key]: 'idle'|'loading'|'ready'|'error' } */
  load: {},
  /** @type {Record<string, string>} 各页面的错误信息 */
  error: {},
};

/** 状态本体。用浅拷贝冻结语义 —— 只能整体替换，不能就地改。 */
let state = { ...initial };

const emitter = createEmitter();

/**
 * 每个字段的活跃订阅者数量。
 *
 * 为什么要显式记账：本项目里"订阅了但忘了退订"是**已经发生过**的缺陷
 * （导航徽章每次 hashchange 重建都新增一个永久订阅者，写进已分离的 DOM）。
 * 这类泄漏完全静默：界面看起来正常，只是内存与 CPU 缓慢增长。
 * 有了这张计数表，回归测试才可能断言"重建 N 次后订阅数回到稳态"。
 */
const subCounts = new Map();

/** 所有已注册的字段名，用于校验 `set` 的键名拼写。 */
const KNOWN_KEYS = new Set(Object.keys(initial));

/**
 * 读取状态。
 * @param {string} [key] 不传则返回整个状态的浅拷贝
 */
export function getState(key) {
  return key === undefined ? { ...state } : state[key];
}

/**
 * 更新状态。只接受已知字段，未知字段在开发模式下告警 ——
 * 静默接受拼错的键（如 `taskDetail` 写成 `taskdetail`）会造成
 * 「赋值了但界面不变」的隐形 bug，非常难查。
 *
 * @param {object} patch 字段补丁
 */
export function set(patch) {
  const changed = [];
  for (const [k, v] of Object.entries(patch)) {
    if (!KNOWN_KEYS.has(k)) {
      console.warn(`[store] 未知字段 "${k}"，已忽略。如需新增请加入 initial。`);
      continue;
    }
    if (Object.is(state[k], v)) continue;
    changed.push(k);
  }
  if (!changed.length) return;

  state = { ...state, ...patch };
  for (const k of changed) emitter.emit(k, state[k]);
  emitter.emit('*', state);
}

/**
 * 订阅字段变化。
 * @param {string|string[]} keys 字段名或字段名数组；传 '*' 订阅任意变化
 * @param {(value:any)=>void} fn
 * @returns {() => void} 取消订阅（**必须**被保存并在销毁时调用，否则会泄漏）
 */
export function subscribe(keys, fn) {
  const list = Array.isArray(keys) ? keys : [keys];
  const offs = list.map((k) => {
    subCounts.set(k, (subCounts.get(k) || 0) + 1);
    return emitter.on(k, fn);
  });
  let done = false;
  return () => {
    // 幂等：重复调用清理函数不应把计数减成负数
    if (done) return;
    done = true;
    offs.forEach((off) => off());
    for (const k of list) subCounts.set(k, Math.max(0, (subCounts.get(k) || 0) - 1));
  };
}

/**
 * 某字段当前的活跃订阅者数量。
 *
 * 仅供测试与诊断使用 —— 它存在的意义是让"忘记退订"这件事**可断言**，
 * 而不是靠人眼审查每一次 subscribe 调用。
 */
export function subscriberCount(key) {
  return subCounts.get(key) || 0;
}

/** 重置为初始值（切换后端地址时用）。 */
export function resetState(keep = ['theme', 'systemDark']) {
  const patch = {};
  for (const k of KNOWN_KEYS) {
    if (keep.includes(k)) continue;
    patch[k] = initial[k];
  }
  set(patch);
}

/* ------------------------------------------------------------------ *
 * 路由
 * ------------------------------------------------------------------ */

/** 路由表。`id` 同时是 hash 值与导航高亮依据。 */
export const ROUTES = [
  {
    id: 'overview',
    label: '总览',
    icon: 'dashboard',
    title: '总览',
    desc: '多 Agent 协作的运行概况与上下文工程收益',
    group: 'main',
  },
  {
    id: 'workbench',
    label: '任务工作台',
    icon: 'git-branch',
    title: '任务工作台',
    desc: '提交需求，观察 DAG 编排、执行轨迹与代码改动',
    group: 'main',
  },
  {
    id: 'context',
    label: '上下文工程',
    icon: 'layers',
    title: '上下文工程看板',
    desc: '装配决策、预算分配、压缩收益与语义缓存命中',
    group: 'main',
  },
  {
    id: 'eval',
    label: '评测中心',
    icon: 'flask',
    title: '评测中心',
    desc: '基于 golden set 的任务级、轨迹级与质量级评测',
    group: 'quality',
  },
  {
    id: 'observability',
    label: '可观测性',
    icon: 'activity',
    title: '可观测性',
    desc: '指标快照、链路追踪与 Prometheus 导出',
    group: 'quality',
  },
  {
    id: 'settings',
    label: '设置',
    icon: 'settings',
    title: '设置',
    desc: '后端连接、外观偏好与数据刷新策略',
    group: 'system',
  },
];

/** 导航分组标题。 */
export const NAV_GROUPS = [
  { id: 'main', label: '工作区' },
  { id: 'quality', label: '质量与观测' },
  { id: 'system', label: '系统' },
];

/** 路由 id → 定义。 */
export const routeById = (id) => ROUTES.find((r) => r.id === id) || ROUTES[0];

/**
 * 解析当前 hash 为路由。
 *
 * hash 形如 `#/workbench?task=abc`。用 hash 而非 History API 的理由：
 * 静态托管（python -m http.server / file://）下 pushState 的深链会 404，
 * 而 hash 路由在任何托管方式下都能直接刷新可用。对演示型产品这是刚需。
 *
 * @returns {{id: string, params: URLSearchParams}}
 */
export function parseHash() {
  const raw = window.location.hash.replace(/^#\/?/, '');
  const [pathPart, queryPart] = raw.split('?');
  const id = pathPart || ROUTES[0].id;
  const params = new URLSearchParams(queryPart || '');
  // 页内锚点（如 skip-link 的 #page-root）不是路由，必须原样放行 ——
  // 否则 routeById 会把它回退成默认路由，导致「跳转到主内容」变成跳转首页。
  if (!ROUTES.some((r) => r.id === id)) {
    return { id: null, params, anchor: id || null };
  }
  return { id, params, anchor: null };
}

/**
 * 跳转路由。
 *
 * ## replace 分支为什么不派发 hashchange（重要的回归教训）
 *
 * 这里曾经是 `history.replaceState(...)` + `window.dispatchEvent(new HashChangeEvent('hashchange'))`。
 * 看似"通知一下总没错"，实际后果是：工作台里点一个任务
 * （`workbench.js` 的 `selectTask` 会调 `navigate('workbench', { task }, true)`
 * 把任务写进 URL）会立刻触发 `shell.js` 的 `renderRoute()` →
 * `disposeCurrent()` **拆掉刚刚建好的页面**，然后重建一遍：
 *   - 两条 SSE 订阅（前一条的清理恰好在 await 之前，管不到这一条）；
 *   - 两次 `/tasks/{id}` + `/context` 请求；
 *   - 选中的节点、时间线滚动位置、输入框内容全部丢失。
 *
 * 判断依据是**路由身份**：`#/workbench?task=A` → `#/workbench?task=B`
 * 只是同一页面的参数变化，页面自己已经知道该怎么更新，外壳不必介入。
 * 而 `#/workbench` → `#/overview`（或非法路由纠错）必须重建页面 ——
 * 那种情况才需要手动补一个事件。
 *
 * 注意：非 replace 分支保持原样（`location.hash = ...`），
 * 由浏览器触发**原生** hashchange —— 前进/后退因此仍然正常工作。
 *
 * @param {string} id 路由 id
 * @param {Record<string,string>} [params] 查询参数
 * @param {boolean} [replace] 是否替换历史记录（用于自动纠错）
 */
export function navigate(id, params = {}, replace = false) {
  const q = new URLSearchParams(
    Object.entries(params).filter(([, v]) => v != null && v !== '')
  ).toString();
  const hash = `#/${id}${q ? `?${q}` : ''}`;
  if (window.location.hash === hash) return;
  if (replace) {
    const routeChanged = routeIdOf(window.location.hash) !== id;
    window.history.replaceState(null, '', hash);
    // 只有路由身份变了才需要外壳重渲染；同路由换参数不应打断页面
    if (routeChanged) window.dispatchEvent(new HashChangeEvent('hashchange'));
  } else {
    window.location.hash = hash;
  }
}

/** 从 hash 里取出路由 id（与 parseHash 同源的宽松版，供 navigate 判断身份用）。 */
function routeIdOf(hash) {
  const raw = String(hash || '').replace(/^#\/?/, '');
  return raw.split('?')[0] || ROUTES[0].id;
}

/** 监听路由变化。 */
export function onRouteChange(fn) {
  const handler = () => fn(parseHash());
  window.addEventListener('hashchange', handler);
  return () => window.removeEventListener('hashchange', handler);
}

/* ------------------------------------------------------------------ *
 * 加载态助手
 * ------------------------------------------------------------------ */

/** 设置某资源的加载状态。 */
export function setLoad(key, status, error = '') {
  set({
    load: { ...state.load, [key]: status },
    error: error ? { ...state.error, [key]: error } : { ...state.error, [key]: '' },
  });
}

/** 读取某资源的加载状态，默认 idle。 */
export function getLoad(key) {
  return state.load[key] || 'idle';
}

/** 读取某资源的错误信息。 */
export function getError(key) {
  return state.error[key] || '';
}

export { initial as initialState };
