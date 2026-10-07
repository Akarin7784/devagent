/**
 * 应用入口。
 *
 * 职责：把页面模块注册到外壳，然后启动外壳。
 * 这一层是**唯一**知道「有哪些页面」的地方，模块之间因此不产生横向依赖。
 */

import { mountShell, registerNotFound, registerPage } from './shell.js';
import { ROUTES } from './store.js';
import { button, emptyState } from './components.js';
import { el } from './util.js';

import renderOverview from './pages/overview.js';
import renderWorkbench from './pages/workbench.js';
import renderContext from './pages/context.js';
import renderEval from './pages/eval.js';
import renderObservability from './pages/observability.js';
import renderSettings from './pages/settings.js';

/* ---------- 注册页面 ---------- */

registerPage('overview', renderOverview);
registerPage('workbench', renderWorkbench);
registerPage('context', renderContext);
registerPage('eval', renderEval);
registerPage('observability', renderObservability);
registerPage('settings', renderSettings);

/* ---------- 404 ---------- */

registerNotFound((root) => {
  root.append(
    el('div', { class: 'card' }, [
      emptyState({
        icon: 'alert-circle',
        title: '页面不存在',
        desc: '这个地址没有对应的页面。它可能已被移除，或者链接拼写有误。',
        action: button('回到总览', {
          icon: 'dashboard',
          variant: 'primary',
          onClick: () => {
            window.location.hash = `#/${ROUTES[0].id}`;
          },
        }),
      }),
    ])
  );
});

/* ---------- 全局兜底 ---------- */

/**
 * 未捕获的 Promise 拒绝：最常见的来源是某处漏了 await 的 fetch。
 * 捕获并提示，避免用户面对一个「什么都点不动」的界面却毫无线索。
 */
window.addEventListener('unhandledrejection', (e) => {
  const msg = e.reason?.message || String(e.reason);
  // 网络类错误已经由 api.js 规范化并就地提示，这里只记录避免重复弹窗
  if (e.reason?.name === 'ApiError') return;
  console.error('[devagent] 未处理的 Promise 拒绝', e.reason);
  void msg;
});

/** 启动。 */
mountShell().catch((err) => {
  console.error('[devagent] 启动失败', err);
  // 外壳都起不来时给出最朴素的兜底，而不是白屏
  const root = document.getElementById('app') || document.body;
  root.textContent = `应用启动失败：${err?.message || err}`;
});

export {};
