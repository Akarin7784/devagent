/**
 * 图标系统 —— 内联 SVG sprite。
 *
 * 设计取舍：**不用图标字体，不用外部图标库**。
 * 理由：
 * 1. 图标字体在暗色主题下要维护第二套字体文件，且渲染有亚像素模糊；
 * 2. 引入 lucide/heroicons 等库需要构建步骤，与「零构建」的项目约束冲突；
 * 3. 内联 SVG 可继承 `currentColor`，能随文本色自动适配主题 —— 这是暗色
 *    主题下图标不变色（最常见的主题 bug）的根本解法。
 *
 * 全部图标统一在 24×24 视图框内绘制，线宽 1.75，圆角端点。
 * 尺寸由 CSS 的 width/height 控制（默认 16px），因此同一图标可用于
 * 按钮内（16）、导航（18）、空态（22）。
 */

/** 图标路径定义。`d` 为路径，`extra` 为额外的图形元素（circle/line 等）。 */
const ICONS = {
  // —— 导航 ——
  dashboard: {
    d: 'M3 12.5 12 4l9 8.5M5.5 10.5V20h13v-9.5',
  },
  'git-branch': {
    d: 'M6 4v12M6 20a2 2 0 1 0 0-4 2 2 0 0 0 0 4ZM18 8a2 2 0 1 0 0-4 2 2 0 0 0 0 4ZM18 8v2a4 4 0 0 1-4 4H8',
  },
  layers: {
    d: 'M12 3 3 7.5l9 4.5 9-4.5L12 3ZM3 12.5 12 17l9-4.5M3 17 12 21.5 21 17',
  },
  flask: {
    d: 'M9 3h6M10 3v6.5L4.8 18A2 2 0 0 0 6.5 21h11a2 2 0 0 0 1.7-3L14 9.5V3M7.5 15h9',
  },
  activity: {
    d: 'M3 12h4l2.5-7 4 14L16 12h5',
  },
  settings: {
    d: 'M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6Z M19.4 15a1.7 1.7 0 0 0 .3 1.9l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.9-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.9.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.9 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.9l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.9.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.9-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.9V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1Z',
  },

  // —— 操作 ——
  plus: { d: 'M12 5v14M5 12h14' },
  play: { d: 'M7 4.5v15l12-7.5-12-7.5Z' },
  stop: { d: 'M7.5 7.5h9v9h-9z' },
  refresh: {
    d: 'M20 11a8 8 0 1 0-2.3 5.7M20 5v6h-6',
  },
  trash: {
    d: 'M4 7h16M9.5 7V5a1 1 0 0 1 1-1h3a1 1 0 0 1 1 1v2M6.5 7l.8 12a1 1 0 0 0 1 1h7.4a1 1 0 0 0 1-1l.8-12M10.5 11v5M13.5 11v5',
  },
  copy: {
    d: 'M9 9V6a2 2 0 0 1 2-2h6a2 2 0 0 1 2 2v6a2 2 0 0 1-2 2h-3M9 9H7a2 2 0 0 0-2 2v6a2 2 0 0 0 2 2h6a2 2 0 0 0 2-2v-2',
  },
  check: { d: 'm5 13 4.5 4.5L19 7' },
  close: { d: 'M6 6l12 12M18 6 6 18' },
  search: { d: 'M11 19a8 8 0 1 0 0-16 8 8 0 0 0 0 16ZM21 21l-4.3-4.3' },
  download: { d: 'M12 3v12M7.5 10.5 12 15l4.5-4.5M4 19h16' },
  link: {
    d: 'M10 13a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7L11.5 5.8M14 11a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1.5-1.5',
  },
  'external-link': {
    d: 'M14 4h6v6M20 4l-8.5 8.5M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5',
  },
  'chevron-right': { d: 'm9 6 6 6-6 6' },
  'chevron-left': { d: 'm15 6-6 6 6 6' },
  'chevron-down': { d: 'm6 9 6 6 6-6' },
  'chevron-up': { d: 'm6 15 6-6 6 6' },
  'arrow-up': { d: 'M12 20V4M6 10l6-6 6 6' },
  'arrow-down': { d: 'M12 4v16M6 14l6 6 6-6' },
  'arrow-right': { d: 'M4 12h16M14 6l6 6-6 6' },
  'more-horizontal': { d: 'M6 12h.01M12 12h.01M18 12h.01', extra: 'circles' },

  // —— 状态 ——
  'check-circle': { d: 'M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18Z m-4-9.5 3 3 5-5.5' },
  'x-circle': { d: 'M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18ZM15 9l-6 6M9 9l6 6' },
  'alert-triangle': {
    d: 'M10.3 4.3 2.6 17.7A2 2 0 0 0 4.3 20.7h15.4a2 2 0 0 0 1.7-3L13.7 4.3a2 2 0 0 0-3.4 0ZM12 9v4.5M12 17h.01',
  },
  'alert-circle': { d: 'M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18ZM12 8v5M12 16h.01' },
  info: { d: 'M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18ZM12 11v5M12 8h.01' },
  clock: { d: 'M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18ZM12 7.5V12l3 2' },
  loader: { d: 'M12 3v4M12 17v4M5.6 5.6l2.8 2.8M15.6 15.6l2.8 2.8M3 12h4M17 12h4M5.6 18.4l2.8-2.8M15.6 8.4l2.8-2.8' },
  ban: { d: 'M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18ZM5.6 5.6l12.8 12.8' },

  // —— 领域 ——
  database: {
    d: 'M12 8c4.4 0 8-1.1 8-2.5S16.4 3 12 3 4 4.1 4 5.5 7.6 8 12 8ZM4 5.5v13C4 19.9 7.6 21 12 21s8-1.1 8-2.5v-13M4 12c0 1.4 3.6 2.5 8 2.5s8-1.1 8-2.5',
  },
  cpu: {
    d: 'M7 7h10v10H7zM4 9h3M4 12h3M4 15h3M17 9h3M17 12h3M17 15h3M9 4v3M12 4v3M15 4v3M9 17v3M12 17v3M15 17v3',
  },
  zap: { d: 'M13.5 2 4 13.5h7L10.5 22 20 10.5h-7L13.5 2Z' },
  shield: { d: 'M12 3 4.5 6v6c0 4.5 3.2 8 7.5 9 4.3-1 7.5-4.5 7.5-9V6L12 3Z' },
  'shield-check': {
    d: 'M12 3 4.5 6v6c0 4.5 3.2 8 7.5 9 4.3-1 7.5-4.5 7.5-9V6L12 3ZM9.5 12.5l2 2 3.5-4',
  },
  code: { d: 'm8.5 8-5 4 5 4M15.5 8l5 4-5 4M13.5 4l-3 16' },
  terminal: { d: 'm5 8 4 4-4 4M12 17h7' },
  folder: { d: 'M3 7V5a1 1 0 0 1 1-1h5l2 3h9a1 1 0 0 1 1 1v11a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V7Z' },
  file: {
    d: 'M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8l-5-5ZM14 3v5h5M9 13h6M9 17h4',
  },
  target: { d: 'M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18ZM12 16.5a4.5 4.5 0 1 0 0-9 4.5 4.5 0 0 0 0 9ZM12 13.5a1.5 1.5 0 1 0 0-3 1.5 1.5 0 0 0 0 3Z' },
  route: { d: 'M6 20a2.5 2.5 0 1 0 0-5 2.5 2.5 0 0 0 0 5ZM18 9a2.5 2.5 0 1 0 0-5 2.5 2.5 0 0 0 0 5ZM18 9v3.5a3.5 3.5 0 0 1-3.5 3.5H8.5' },
  scale: { d: 'M12 3v18M7 7l-4 9h8L7 7ZM17 7l-4 9h8l-4-9ZM7 7h10M8 21h8' },
  gauge: { d: 'M12 21a9 9 0 1 1 9-9M12 12l4.5-3.5' },
  'trending-up': { d: 'M3 17.5 9 11l4 4 8-8M15.5 3.5H21v5.5' },
  'trending-down': { d: 'M3 6.5 9 13l4-4 8 8M15.5 20.5H21v-5.5' },
  'minus': { d: 'M5 12h14' },
  hash: { d: 'M5 9.5h14M5 14.5h14M10 3.5 8 20.5M16 3.5l-2 17' },
  filter: { d: 'M4 5h16l-6.2 7.4V19L10.2 17v-4.6L4 5Z' },
  sort: { d: 'M8 5v14M8 19l-3-3M8 19l3-3M16 19V5M16 5l-3 3M16 5l3 3' },
  book: { d: 'M4 5.5A2.5 2.5 0 0 1 6.5 3H19v15H6.5A2.5 2.5 0 0 0 4 20.5v-15ZM4 20.5A2.5 2.5 0 0 1 6.5 18H19v3H6.5A2.5 2.5 0 0 1 4 20.5Z' },
  users: {
    d: 'M16.5 20v-1.5a3.5 3.5 0 0 0-3.5-3.5H7a3.5 3.5 0 0 0-3.5 3.5V20M10 12a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7ZM20.5 20v-1.5a3.5 3.5 0 0 0-2.6-3.4M15.5 5.2a3.5 3.5 0 0 1 0 6.6',
  },
  bot: {
    d: 'M12 3v3M7 6h10a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2ZM9.5 11h.01M14.5 11h.01M9.5 15h5',
  },
  inbox: {
    d: 'M4 13h4l1.5 2.5h5L16 13h4M4 13l2.5-7.5A2 2 0 0 1 8.4 4h7.2a2 2 0 0 1 1.9 1.5L20 13v4a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2v-4Z',
  },
  palette: {
    d: 'M12 21a9 9 0 1 1 9-9c0 1.7-1.3 3-3 3h-1.5a2 2 0 0 0-1.5 3.3A1.7 1.7 0 0 1 12 21ZM7.5 10.5h.01M10.5 7.5h.01M14.5 7.5h.01',
  },
  'panel-left': { d: 'M4 4h16v16H4zM10 4v16' },
  sun: { d: 'M12 17a5 5 0 1 0 0-10 5 5 0 0 0 0 10ZM12 1.5v2M12 20.5v2M4.2 4.2l1.4 1.4M18.4 18.4l1.4 1.4M1.5 12h2M20.5 12h2M4.2 19.8l1.4-1.4M18.4 5.6l1.4-1.4' },
  moon: { d: 'M20.5 14.3A8.5 8.5 0 1 1 9.7 3.5a7 7 0 0 0 10.8 10.8Z' },
  monitor: { d: 'M4 4h16v11H4zM8.5 20h7M12 15v5' },
  menu: { d: 'M4 7h16M4 12h16M4 17h16' },
  'log-out': { d: 'M9 4H6a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h3M16 16l4-4-4-4M20 12H9' },
  history: { d: 'M3 12a9 9 0 1 0 2.6-6.4M3 4v5h5M12 8v4.5l3 2' },
  award: { d: 'M12 15a6 6 0 1 0 0-12 6 6 0 0 0 0 12ZM8.5 14 7 21.5l5-2.5 5 2.5L15.5 14' },
  percent: { d: 'M19 5 5 19M7 9.5a2.5 2.5 0 1 0 0-5 2.5 2.5 0 0 0 0 5ZM17 19.5a2.5 2.5 0 1 0 0-5 2.5 2.5 0 0 0 0 5Z' },
  coins: { d: 'M9 12.5c3.6 0 6.5-1.1 6.5-2.5S12.6 7.5 9 7.5 2.5 8.6 2.5 10s2.9 2.5 6.5 2.5ZM2.5 10v5c0 1.4 2.9 2.5 6.5 2.5s6.5-1.1 6.5-2.5v-5M9 15c3.6 0 6.5-1.1 6.5-2.5' },
  package: { d: 'm12 3 8 4.5v9L12 21l-8-4.5v-9L12 3ZM4 7.5 12 12l8-4.5M12 12v9' },
  eye: { d: 'M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12ZM12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6Z' },
  'eye-off': { d: 'M10.5 6a9 9 0 0 1 1.5-.1c6 0 9.5 6.1 9.5 6.1a17 17 0 0 1-2.7 3.4M6.6 7.8A17 17 0 0 0 2.5 12s3.5 6 9.5 6a9 9 0 0 0 4.2-1M3 3l18 18M10 10a3 3 0 0 0 4 4' },
  maximize: { d: 'M9 4H4v5M15 20h5v-5M20 9V4h-5M4 15v5h5' },
  'zoom-in': { d: 'M11 19a8 8 0 1 0 0-16 8 8 0 0 0 0 16ZM21 21l-4.3-4.3M11 8.5v5M8.5 11h5' },
  'zoom-out': { d: 'M11 19a8 8 0 1 0 0-16 8 8 0 0 0 0 16ZM21 21l-4.3-4.3M8.5 11h5' },
  'fit-screen': { d: 'M4 9V5.5A1.5 1.5 0 0 1 5.5 4H9M15 4h3.5A1.5 1.5 0 0 1 20 5.5V9M20 15v3.5a1.5 1.5 0 0 1-1.5 1.5H15M9 20H5.5A1.5 1.5 0 0 1 4 18.5V15' },
  'pause': { d: 'M9 5v14M15 5v14' },
  'file-text': { d: 'M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8l-5-5ZM14 3v5h5M9 12h6M9 16h4' },
  inbox_empty: { d: 'M4 13h4l1.5 2.5h5L16 13h4M4 13l2.5-7.5A2 2 0 0 1 8.4 4h7.2a2 2 0 0 1 1.9 1.5L20 13v4a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2v-4Z' },
};

/** `extra: 'circles'` 的图标需要额外画点（more-horizontal）。 */
const EXTRAS = {
  'more-horizontal': [
    { tag: 'circle', attrs: { cx: 5, cy: 12, r: 1.4, fill: 'currentColor', stroke: 'none' } },
    { tag: 'circle', attrs: { cx: 12, cy: 12, r: 1.4, fill: 'currentColor', stroke: 'none' } },
    { tag: 'circle', attrs: { cx: 19, cy: 12, r: 1.4, fill: 'currentColor', stroke: 'none' } },
  ],
};

const ATTRS = 'fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"';

/**
 * 生成图标的 SVG 字符串。
 *
 * `aria-hidden="true"` 是刻意的：图标永远是装饰性的 —— 语义由相邻的文本
 * 或按钮的 `aria-label` 承担。让屏幕阅读器读出一堆「图形」只会制造噪音。
 *
 * @param {string} name 图标名
 * @param {number|string} [size=16] 边长（px）
 * @param {string} [className=''] 附加 class
 * @returns {string} SVG 标记
 */
export function icon(name, size = 16, className = '') {
  const def = ICONS[name];
  if (!def) {
    // 未定义图标时返回空串而非抛错：一个图标缺失不该让整个页面渲染失败。
    if (typeof console !== 'undefined') console.warn(`[icons] 未定义图标: ${name}`);
    return '';
  }
  const extras = (EXTRAS[name] || [])
    .map((e) => `<${e.tag} ${Object.entries(e.attrs).map(([k, v]) => `${k}="${v}"`).join(' ')}/>`)
    .join('');
  const d = Array.isArray(def.d) ? def.d : [def.d];
  const paths = d.map((p) => `<path d="${p}"/>`).join('');
  const cls = ['icon', className].filter(Boolean).join(' ');
  return `<svg class="${cls}" width="${size}" height="${size}" viewBox="0 0 24 24" ${ATTRS} aria-hidden="true" focusable="false">${paths}${extras}</svg>`;
}

/** 供测试与校验使用：已注册的图标名列表。 */
export const ICON_NAMES = Object.freeze(Object.keys(ICONS));

export default icon;
