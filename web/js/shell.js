/**
 * App Shell —— 应用外壳。
 *
 * 职责（严格限定，不含任何业务渲染）：
 *  1. 构建侧边导航 / 顶栏 / 主内容容器；
 *  2. 路由分发（hash → 页面模块）；
 *  3. 主题（亮/暗/跟随系统）的解析与应用；
 *  4. 连接状态探测与展示；
 *  5. 全局加载进度条与移动端抽屉式导航。
 *
 * 页面模块只负责往 `#page-root` 里渲染内容，不需要感知外壳。
 */

import { api, getApiBase, requestEvent, setApiBase } from './api.js?v=20261008-live';
import { button, iconButton, iconEl, openModal, toast } from './components.js?v=20261008-live';
import { icon } from './icons.js?v=20261008-live';
import {
  NAV_GROUPS,
  ROUTES,
  getState,
  navigate,
  onRouteChange,
  parseHash,
  routeById,
  set,
  subscribe,
} from './store.js?v=20261008-live';
import { $, activateModal, clear, el, fromHTML, mount, storage } from './util.js?v=20261008-live';

/* ------------------------------------------------------------------ *
 * 主题
 * ------------------------------------------------------------------ */

const THEME_KEY = 'devagent.theme';

/** 系统暗色媒体查询。 */
const darkQuery = window.matchMedia('(prefers-color-scheme: dark)');

/**
 * 解析偏好为实际生效的主题。
 * 'system' 必须查媒体查询，不能只在启动时读一次 —— 用户可能在
 * 使用过程中切换系统主题（macOS 的自动日落切换就是典型场景）。
 */
function resolveTheme(pref) {
  if (pref === 'system') return darkQuery.matches ? 'dark' : 'light';
  return pref === 'dark' ? 'dark' : 'light';
}

/** 应用到 DOM。 */
function applyTheme(pref) {
  const effective = resolveTheme(pref);
  document.documentElement.dataset.theme = effective;
  // 让浏览器 UI（地址栏、表单控件）也跟着走
  document.documentElement.style.colorScheme = effective;
  set({ theme: pref, systemDark: effective === 'dark' });
  return effective;
}

darkQuery.addEventListener('change', () => {
  if (getState('theme') === 'system') {
    applyTheme('system');
    syncThemeButton();
  }
});

let themeBtn = null;

function syncThemeButton() {
  if (!themeBtn) return;
  const pref = getState('theme');
  const effective = resolveTheme(pref);
  const titleMap = {
    light: '当前：亮色 — 点击切换为暗色',
    dark: '当前：暗色 — 点击切换为跟随系统',
    system: `当前：跟随系统（${effective === 'dark' ? '暗色' : '亮色'}）— 点击切换为亮色`,
  };
  themeBtn.innerHTML = icon(effective === 'dark' ? 'moon' : 'sun', 16);
  themeBtn.title = titleMap[pref];
  themeBtn.setAttribute('aria-label', titleMap[pref]);
}

/** 循环切换：light → dark → system → light。 */
export function cycleTheme() {
  const cur = getState('theme');
  const next = { light: 'dark', dark: 'system', system: 'light' }[cur] || 'light';
  storage.set(THEME_KEY, next);
  applyTheme(next);
  syncThemeButton();
  const names = { light: '亮色', dark: '暗色', system: '跟随系统' };
  toast({ tone: 'info', title: `外观已切换为「${names[next]}」`, duration: 2000 });
}

/** 设置主题（设置页调用）。 */
export function setTheme(pref) {
  storage.set(THEME_KEY, pref);
  applyTheme(pref);
  syncThemeButton();
}

/* ------------------------------------------------------------------ *
 * 连接状态
 * ------------------------------------------------------------------ */

/**
 * 探测后端健康状态。
 *
 * 用「探测 + 分类」而非简单布尔：区分「连不上」与「连上了但没配模型」
 * 对用户意义完全不同 —— 后者任务会立刻失败，需要提前警告。
 */
export async function probeHealth() {
  try {
    const h = await api.health();
    const providers = Array.isArray(h?.providers) ? h.providers : [];
    set({
      connected: true,
      providers,
      observability: !!h?.observability_enabled,
      version: h?.version || '',
      healthDetail: providers.length
        ? `${providers.join(' / ')} · 指标${h?.observability_enabled ? '开' : '关'}`
        : '已连接 · 未配置模型',
    });
    return { ok: true, providers, observability: !!h?.observability_enabled };
  } catch (err) {
    set({
      connected: false,
      providers: [],
      healthDetail: err?.message || '连接失败',
    });
    return { ok: false, error: err };
  }
}

function connIndicator() {
  const node = el('button', {
    class: 'conn-status',
    attrs: { type: 'button' },
    on: { click: () => showConnectionInfo() },
  });
  const dot = el('span', { class: 'dot dot-sm' });
  const text = el('span', { class: 'conn-status-text' });
  node.append(dot, text);

  const paint = () => {
    const connected = getState('connected');
    dot.className = `dot dot-sm ${
      connected
        ? (getState('providers').length ? 'dot-success' : 'dot-warning')
        : 'dot-danger'
    }`;
    text.textContent = getState('healthDetail');
    node.title = `${connected ? '后端已连接' : '后端未连接'}：${getState('healthDetail')}`;
  };
  paint();
  subscribe(['connected', 'healthDetail', 'providers'], paint);
  return node;
}

function kvLine(k, v) {
  return el('div', { class: 'kv-item' }, [
    el('div', { class: 'kv-key', text: k }),
    el('div', { class: 'kv-value mono', text: v, style: { 'font-size': 'var(--fs-xs)', 'word-break': 'break-all' } }),
  ]);
}

function showConnectionInfo() {
  const connected = getState('connected');
  const providers = getState('providers');
  const base = getApiBase() || '同源（当前页面地址）';

  const body = el('div', { class: 'field', style: { gap: 'var(--space-4)' } });

  if (!connected) {
    body.append(
      el('div', { class: 'alert alert-warning' }, [
        el('div', { class: 'alert-icon', html: icon('alert-triangle', 15) }),
        el('div', { class: 'alert-body' }, [
          el('div', { class: 'alert-title', text: '后端不可达' }),
          el('div', {
            html: '请先启动后端：<code>PYTHONPATH=src python scripts/serve_demo.py</code>'
              + '（演示用脚本化模型）或 <code>python -m devagent.cli serve</code>（需配置 API Key）。',
          }),
        ]),
      ])
    );
  } else if (!providers.length) {
    body.append(
      el('div', { class: 'alert alert-warning' }, [
        el('div', { class: 'alert-icon', html: icon('alert-triangle', 15) }),
        el('div', { class: 'alert-body' }, [
          el('div', { class: 'alert-title', text: '未配置模型提供商' }),
          el('div', { text: '后端已连通，但没有可用的模型 Key，提交的任务会在需求澄清阶段失败。' }),
        ]),
      ])
    );
  }

  body.append(
    el('div', { class: 'kv' }, [
      kvLine('状态', connected ? '已连接' : '未连接'),
      kvLine('接口地址', base),
      kvLine('模型提供商', providers.length ? providers.join('、') : '未配置'),
      kvLine('可观测性', getState('observability') ? '已开启' : '未开启'),
      kvLine('服务版本', getState('version') || '—'),
    ])
  );

  const { close } = openModal({
    title: '后端连接',
    body,
    size: 'sm',
  });

  // 追加一个「重新探测」按钮（openModal 未提供 actions，这里手动挂）
  const footer = el('div', { class: 'modal-footer' }, [
    button('重新探测', {
      icon: 'refresh',
      variant: 'secondary',
      onClick: async () => {
        await probeHealth();
        close();
        toast({
          tone: getState('connected') ? 'success' : 'error',
          title: getState('connected') ? '连接正常' : '仍然无法连接',
          desc: getState('healthDetail'),
        });
      },
    }),
    button('关闭', { variant: 'primary', onClick: close }),
  ]);
  // 把 footer 移到 modal 内部末尾
  const modal = document.querySelector('.overlay .modal');
  if (modal) modal.append(footer);
}

/* ------------------------------------------------------------------ *
 * 侧边导航
 * ------------------------------------------------------------------ */

/**
 * 导航里注册的订阅清理函数。
 *
 * ## 为什么要有这个模块级变量（这是一次真实的泄漏）
 *
 * `rebuildNav()` 每次 hashchange 都会重建整个导航，而导航项里的
 * "运行中任务数"徽章会 `subscribe('tasks', paint)`。
 * 原实现丢弃了 subscribe 的返回值 —— 于是每导航一次就多一个**永久**
 * 订阅者，全都往已经脱离文档的 `<span>` 里写 DOM：N 次导航后是 N+1 个。
 * 界面看起来完全正常，只是内存与 CPU 缓慢增长。
 *
 * 因此把清理函数存到模块级，重建前先统一回收（`disposeNavSubscriptions`）。
 */
let navDisposers = [];

/** 回收上一次导航注册的全部订阅（幂等）。 */
function disposeNavSubscriptions() {
  for (const off of navDisposers) {
    try {
      off();
    } catch (err) {
      console.error('[shell] 导航订阅清理失败', err);
    }
  }
  navDisposers = [];
}

function buildNav() {
  const nav = el('nav', { class: 'nav', attrs: { 'aria-label': '主导航' } });
  // 经 routeById 归一：页内锚点（如 #page-root）解析出 id=null，
  // 这里回退到默认路由，保证导航高亮不会全部落空。
  const route = routeById(parseHash().id);

  for (const g of NAV_GROUPS) {
    const items = ROUTES.filter((r) => r.group === g.id);
    if (!items.length) continue;
    nav.append(el('div', { class: 'nav-section-label', text: g.label }));
    for (const r of items) {
      const active = r.id === route.id;
      const item = el('a', {
        class: 'nav-item',
        attrs: {
          href: `#/${r.id}`,
          ...(active ? { 'aria-current': 'page' } : {}),
        },
        on: { click: (event) => {
          if (event.button > 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
          // 先导航再收起抽屉，避免设置 inert 中断链接的默认激活行为。
          event.preventDefault();
          navigate(r.id);
          toggleMobileNav(false);
        } },
      });
      item.append(fromHTML(icon(r.icon, 17)));
      item.append(el('span', { text: r.label }));

      // 工作台显示运行中任务数 —— 让用户在任何页面都能感知后台进度
      if (r.id === 'workbench') {
        const badgeEl = el('span', { class: 'nav-item-badge' });
        const paint = () => {
          const tasks = getState('tasks');
          const running = tasks.filter(
            (t) => t.status === 'running' || t.status === 'pending'
          ).length;
          badgeEl.textContent = running ? String(running) : '';
          badgeEl.hidden = !running;
        };
        paint();
        // **必须保存**取消订阅函数（见 navDisposers 的注释）
        navDisposers.push(subscribe('tasks', paint));
        item.append(badgeEl);
      }
      nav.append(item);
    }
  }
  return nav;
}

/** 建一个新的导航节点；旧节点的订阅在这里被回收。 */
function createNav() {
  disposeNavSubscriptions();
  const fresh = buildNav();
  fresh.id = 'app-nav';
  return fresh;
}

/**
 * 建导航（**导出仅为可测**）。
 *
 * 导航徽章的订阅泄漏是"界面全对、内存缓慢增长"的类型，只有把
 * "重建两次之后订阅数是否翻倍"变成断言才防得住。
 * 与 `graph.js` 导出 `edgePath` 的理由相同：可测的边界值得显式一点。
 */
export function createNavForTest() {
  return createNav();
}

function rebuildNav() {
  const old = $('app-nav');
  if (!old) return;
  old.replaceWith(createNav());
}

/* ------------------------------------------------------------------ *
 * 顶栏
 * ------------------------------------------------------------------ */

let statusBadge = null;

function paintTopbarBadge() {
  if (!statusBadge) return;
  const connected = getState('connected');
  const providers = getState('providers');
  const tone = connected ? (providers.length ? 'success' : 'warning') : 'danger';
  statusBadge.className = `badge badge-${tone}`;
  clear(statusBadge);
  statusBadge.append(fromHTML(icon(connected ? 'check-circle' : 'alert-circle', 11)));
  statusBadge.append(
    el('span', {
      text: connected ? (providers.length ? '服务正常' : '未配模型') : '未连接',
    })
  );
  statusBadge.title = getState('healthDetail');
}

function buildTopbar() {
  const route = routeById(parseHash().id);

  const toggle = el('button', {
    class: 'btn btn-ghost btn-icon nav-toggle',
    attrs: {
      type: 'button',
      'aria-label': '打开导航菜单',
      'aria-expanded': 'false',
      'aria-controls': 'app-sidebar',
    },
    on: { click: () => toggleMobileNav() },
  });
  toggle.append(fromHTML(icon('menu', 18)));

  const title = el('div', { class: 'topbar-title' }, [
    el('h1', { attrs: { id: 'page-title' }, text: route.title }),
    el('div', { class: 'subtitle', attrs: { id: 'page-desc' }, text: route.desc }),
  ]);

  statusBadge = el('span', { class: 'badge badge-neutral', attrs: { id: 'topbar-status' } });

  themeBtn = el('button', {
    class: 'btn btn-ghost btn-icon theme-toggle',
    attrs: { type: 'button' },
    on: { click: cycleTheme },
  });
  syncThemeButton();

  const bar = el('header', { class: 'app-topbar', attrs: { role: 'banner' } }, [
    toggle,
    title,
    el('div', { class: 'topbar-actions' }, [statusBadge, themeBtn]),
  ]);

  paintTopbarBadge();
  subscribe(['connected', 'providers', 'healthDetail'], paintTopbarBadge);
  return bar;
}

/* ------------------------------------------------------------------ *
 * 移动端导航
 * ------------------------------------------------------------------ */

let navOverlay = null;
let releaseMobileNav = null;

function toggleMobileNav(force) {
  const sidebar = $('app-sidebar');
  if (!sidebar) return;
  const open = force ?? !sidebar.dataset.open;

  if (open) {
    if (sidebar.dataset.open) return;
    sidebar.inert = false;
    sidebar.dataset.open = 'true';
    sidebar.style.transform = 'translateX(0)';
    navOverlay = el('div', {
      class: 'drawer-overlay',
      on: { click: () => toggleMobileNav(false) },
    });
    document.body.append(navOverlay);
    releaseMobileNav = activateModal(sidebar, { onEscape: () => toggleMobileNav(false) });
    document.querySelector('.nav-toggle')?.setAttribute('aria-expanded', 'true');
  } else {
    releaseMobileNav?.();
    releaseMobileNav = null;
    sidebar.inert = !window.matchMedia('(min-width: 1024px)').matches;
    delete sidebar.dataset.open;
    sidebar.style.transform = '';
    navOverlay?.remove();
    navOverlay = null;
    document.querySelector('.nav-toggle')?.setAttribute('aria-expanded', 'false');
  }
}

// 跨越桌面断点时收起抽屉，并同步隐藏侧栏的键盘可达性。
window.matchMedia('(min-width: 1024px)').addEventListener('change', () => {
  toggleMobileNav(false);
});

/* ------------------------------------------------------------------ *
 * 加载进度条
 * ------------------------------------------------------------------ */

let progressEl = null;
let progressCount = 0;

function initProgress() {
  progressEl = el('div', {
    class: 'route-progress',
    attrs: { 'aria-hidden': 'true' },
  });
  document.body.append(progressEl);

  requestEvent.on('pending', (count) => {
    progressCount = count;
    progressEl.dataset.active = count > 0 ? 'true' : 'false';
  });
}

/** 手动控制进度条（路由切换等本地耗时操作用）。 */
export function setBusy(on) {
  if (!progressEl) return;
  progressEl.dataset.active = on || progressCount > 0 ? 'true' : 'false';
}

/* ------------------------------------------------------------------ *
 * 外壳构建与路由
 * ------------------------------------------------------------------ */

/** 页面模块注册表。由 main.js 注入，避免 shell 直接依赖业务页面。 */
const pageRegistry = new Map();
let notFoundRenderer = null;

/**
 * 注册页面。
 * @param {string} id 路由 id
 * @param {(container: HTMLElement, ctx: object) => void|Promise<void|Function>} render
 */
export function registerPage(id, render) {
  pageRegistry.set(id, render);
}

export function registerNotFound(fn) {
  notFoundRenderer = fn;
}

/** 当前页面的清理函数（订阅、SSE、定时器统一在这里回收）。 */
let disposeCurrent = null;

/**
 * 渲染当前路由对应的页面。
 *
 * 生命周期：清理上一页 → 更新外壳文案 → 调用页面渲染函数。
 * 页面渲染函数可以返回一个清理函数，shell 会持久保存并在下次导航时调用。
 * 这是避免「切页后定时器/SSE 还在跑」的关键机制。
 */
async function renderRoute() {
  const route = routeById(parseHash().id);
  const root = $('page-root');
  if (!root) return;
  root.dataset.page = route.id;

  if (typeof disposeCurrent === 'function') {
    try {
      disposeCurrent();
    } catch (err) {
      console.error('[shell] 页面清理失败', err);
    }
    disposeCurrent = null;
  }

  document.title = `${route.title} · DevAgent`;
  const titleEl = $('page-title');
  const descEl = $('page-desc');
  if (titleEl) titleEl.textContent = route.title;
  if (descEl) descEl.textContent = route.desc;

  clear(root);
  window.scrollTo({ top: 0, behavior: 'auto' });
  rebuildNav();

  const ctx = { route, params: parseHash().params, navigate };
  const renderer = pageRegistry.get(route.id);

  setBusy(true);
  try {
    if (renderer) {
      const dispose = await renderer(root, ctx);
      if (typeof dispose === 'function') disposeCurrent = dispose;
    } else if (notFoundRenderer) {
      notFoundRenderer(root, ctx);
    } else {
      mount(
        root,
        el('div', { class: 'empty-state' }, [
          el('div', { class: 'empty-state-icon', html: icon('alert-circle', 22) }),
          el('div', { class: 'empty-state-title', text: '页面不存在' }),
          el('div', { class: 'empty-state-desc', text: `未找到路由「${route.id}」。` }),
        ])
      );
    }
  } catch (err) {
    // 页面渲染抛错不能让整个应用白屏 —— 给出可操作的错误界面
    console.error('[shell] 页面渲染失败', err);
    clear(root);
    root.append(errorPage(err, route));
  } finally {
    setBusy(false);
  }
}

function errorPage(err, route) {
  return el('div', { class: 'card' }, [
    el('div', { class: 'error-state' }, [
      el('div', { class: 'error-state-icon', html: icon('alert-triangle', 22) }),
      el('div', { class: 'error-state-title', text: `「${route.title}」渲染失败` }),
      el('div', { class: 'error-state-desc', text: err?.message || String(err) }),
      el('div', { style: { display: 'flex', gap: 'var(--space-2)' } }, [
        button('重新加载', { icon: 'refresh', variant: 'secondary', onClick: () => renderRoute() }),
        button('回到总览', { variant: 'ghost', onClick: () => navigate('overview') }),
      ]),
    ]),
  ]);
}

/* ------------------------------------------------------------------ *
 * 启动
 * ------------------------------------------------------------------ */

/**
 * 构建外壳并启动。
 * @param {object} [opts]
 */
function badgeWorkspace() {
  return el('span', { class: 'workspace-local', text: '本地' });
}

export async function mountShell() {
  // 主题尽早就位，减少首屏闪色（FOUC）
  applyTheme(storage.get(THEME_KEY, 'system'));

  // API 基址：?api= 优先，其次是上次保存的值
  const params = new URLSearchParams(window.location.search);
  const baseFromUrl = params.get('api');
  if (baseFromUrl != null) {
    setApiBase(baseFromUrl);
    storage.set('devagent.apiBase', baseFromUrl);
  } else {
    setApiBase(storage.get('devagent.apiBase', ''));
  }

  const app = $('app');

  const brand = el('div', { class: 'brand' }, [
    el('div', { class: 'brand-mark', html: icon('terminal', 19) }),
    el('div', { class: 'brand-text' }, [
      el('div', { class: 'brand-name', text: 'DevAgent' }),
      el('div', { class: 'brand-tagline', text: '你的研发协作空间' }),
    ]),
  ]);

  const nav = createNav();

  const docsLink = el('a', {
    class: 'nav-item',
    attrs: { href: '#', role: 'button' },
    on: {
      click: (e) => {
        e.preventDefault();
        const base = getApiBase() || window.location.origin;
        window.open(`${base}/docs`, '_blank', 'noopener');
      },
    },
  });
  docsLink.append(fromHTML(icon('book', 17)));
  docsLink.append(el('span', { text: 'API 文档' }));

  const closeNav = iconButton('close', { label: '关闭导航菜单', onClick: () => toggleMobileNav(false) });
  closeNav.classList.add('mobile-nav-close');
  const sidebar = el(
    'aside',
    { class: 'app-sidebar', attrs: { id: 'app-sidebar' } },
    [closeNav, brand, el('div', { class: 'workspace-label' }, [fromHTML(icon('folder', 14)), el('span', { text: '当前工作区' }), badgeWorkspace()]), nav, el('div', { class: 'sidebar-foot' }, [connIndicator(), docsLink])]
  );

  const main = el('div', { class: 'app-main' }, [
    buildTopbar(),
    el('main', {
      class: 'app-content',
      attrs: { id: 'page-root', role: 'main', tabindex: '-1' },
    }),
  ]);

  // 跳过导航链接 —— 键盘用户的第一个 Tab 位
  document.body.prepend(
    el('a', { class: 'skip-link', attrs: { href: '#page-root' }, text: '跳转到主内容' })
  );

  clear(app);
  app.append(sidebar, main);
  sidebar.inert = !window.matchMedia('(min-width: 1024px)').matches;

  initProgress();

  onRouteChange(() => renderRoute());
  if (!window.location.hash) {
    // 无 hash 时用 replaceState 补默认路由，不污染历史栈
    window.history.replaceState(null, '', `#/${ROUTES[0].id}`);
  }
  await renderRoute();

  // 健康探测：立刻一次 + 30s 轮询兜底
  probeHealth();
  window.setInterval(() => {
    // 页面隐藏时不轮询 —— 省电，也避免后台标签页堆积报错
    if (document.visibilityState === 'visible') probeHealth();
  }, 30_000);

  // 标签页重新可见时立即刷新一次连接状态（可能挂起了一夜）
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') probeHealth();
  });
}

export { resolveTheme };
