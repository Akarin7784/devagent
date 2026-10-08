/**
 * 设置页。
 *
 * 分区：后端连接、模型供应商、后端配置、外观、数据、关于。
 *
 * ## 为什么分区展示，而不是一页堆叠
 *
 * 四个分区的内容量差异极大：连接区有输入框、实时状态与可达性告警，
 * 关于区只有静态信息。堆在一页里的后果是首屏被**最不常改**的内容占满 ——
 * 只想切个主题，也要先滚过一整块连接表单。分区后每屏只呈现一个主题。
 *
 * ## 当前分区记在 URL 上（`#/settings?tab=appearance`）
 *
 * 刷新、分享、前进后退都能回到同一个分区，而不是"切了分区一刷新就回到第一个"。
 * 复用工作台 `?task=` 的同一套机制：同路由 replace **不触发**外壳拆页重建
 * （见 store.js 中 `navigate` 的注释），所以切分区不会打断页面状态。
 *
 * `?tab=` 是不可信输入（手改、旧书签、拼错的链接），必须经
 * `resolveSettingsTab` 归一 —— 否则一个不存在的分区 id 会渲染出
 * **空白面板且没有任何报错**，看起来就像"设置页坏了"。
 *
 * 设计原则：**每一项都必须有明确的作用说明与生效时机**。
 * 设置项不说清「改了什么、什么时候生效」，用户就只能靠试错 ——
 * 这是设置页面最常见的可用性缺陷。
 */

import { getApiBase, getApiKey, setApiBase, setApiKey } from '../api.js?v=20261008-live';
import { renderBackendSettings } from '../backend-settings.js?v=20261008-live';
import { cycleTheme, probeHealth, setTheme } from '../shell.js?v=20261008-live';
import {
  alert,
  button,
  card,
  confirmDialog,
  kvItem,
  toast,
} from '../components.js?v=20261008-live';
import { icon } from '../icons.js?v=20261008-live';
import { getState, navigate, resetState, subscribe } from '../store.js?v=20261008-live';
import { clear, el, mount, storage } from '../util.js?v=20261008-live';

/**
 * 分区定义。
 *
 * 顺序即标签顺序；`icon` 必须是 icons.js 里真实存在的名字 ——
 * `icon()` 遇到未定义图标只返回空串并打印告警，界面不会报错，
 * 所以这里由测试守着（见 pages.test.js 第 8 节）。
 */
export const SETTINGS_TABS = [
  { id: 'connection', label: '后端连接', icon: 'link' },
  { id: 'models', label: '模型供应商', icon: 'cpu' },
  { id: 'backend', label: '后端配置', icon: 'settings' },
  { id: 'appearance', label: '外观', icon: 'sun' },
  { id: 'data', label: '数据', icon: 'database' },
  { id: 'about', label: '关于', icon: 'info' },
];

/**
 * 把任意来源的分区 id 归一为**真实存在**的分区。
 *
 * 未知值回落到第一个分区，而不是返回空串：一个拼错的 `?tab=` 应该安静地
 * 回到默认分区，而不是给用户一个空白面板。
 *
 * @param {unknown} raw
 * @returns {string} 一定存在于 `SETTINGS_TABS` 中的 id
 */
export function resolveSettingsTab(raw) {
  const id = typeof raw === 'string' ? raw.trim() : '';
  return SETTINGS_TABS.some((t) => t.id === id) ? id : SETTINGS_TABS[0].id;
}

/**
 * 当前分区。
 *
 * 放在模块级而不是渲染函数内：改主题、重测连接都会触发 `paint()` 重建整页，
 * 若分区状态跟着重建，用户会被莫名弹回第一个分区。
 */
let activeTab = SETTINGS_TABS[0].id;

export default async function renderSettings(root, ctx) {
  const container = el('div', { class: 'page-stack settings-page' });
  root.append(container);
  let disposed = false;
  const backendSession = {};

  // URL 优先（深链 / 刷新 / 前进后退）；没有参数时沿用本次会话内停留的分区。
  const fromUrl = ctx?.params?.get?.('tab');
  if (fromUrl != null) {
    const resolved = resolveSettingsTab(fromUrl);
    // 非法值就地纠正 URL：否则地址栏写着 ?tab=xyz 而界面显示"后端连接"，
    // 复制出去又是一个坏链接。
    if (resolved !== fromUrl) navigate('settings', { tab: resolved }, true);
    activeTab = resolved;
  }

  function paint() {
    if (disposed) return;
    clear(container);
    mount(container, ...buildLayout());
  }

  /**
   * 标签上的连接状态点及其屏幕阅读器文本（由 `buildTabs` 在渲染时登记）。
   *
   * 为什么必须订阅而不是"渲染时读一次"：设置页可能在健康探测返回**之前**
   * 就渲染完成 —— 那一刻 `connected` 还是 false，点会一直红着，而顶栏稍后
   * 显示「服务正常」。同一个屏幕上两个相反的结论，比不显示状态更糟。
   */
  let statusDot = null;
  let statusText = null;

  /** 焦点是否落在本页的文本输入控件里。 */
  function isTypingInPage() {
    const active = document.activeElement;
    if (!active || !container.contains(active)) return false;
    const tag = active.tagName;
    return tag === 'INPUT' || tag === 'TEXTAREA' || active.isContentEditable === true;
  }

  /**
   * 连接状态变化的应变。
   *
   * - 正在输入：只就地更新状态点。整页重绘会把连接区输入框里刚敲的地址
   *   连同光标一起冲掉（observability 页踩过同一个坑）；
   * - 否则整页重绘：卡片里的「已连接/未连接」与「后端不可达」告警一并刷新。
   */
  function onConnectionChange() {
    if (!isTypingInPage() && !['backend', 'models'].includes(activeTab)) {
      paint();
      return;
    }
    if (!statusDot) return;
    const ok = getState('connected');
    statusDot.setAttribute('class', `dot ${ok ? 'dot-success' : 'dot-danger'}`);
    if (statusText) statusText.textContent = ok ? '已连接' : '未连接';
  }

  // 订阅必须回收，否则每次进入设置页都会多一个永久订阅者（往已分离的 DOM 里写）
  const unsubscribe = subscribe('connected', onConnectionChange);

  /* ---------- 分区切换 ---------- */

  function selectTab(id) {
    const next = resolveSettingsTab(id);
    if (next === activeTab) return;
    activeTab = next;
    // 只改 URL（replace）：不写历史记录、不派发 hashchange，
    // 因此外壳不会拆掉当前页面再重建（切线不该打断页面）。
    navigate('settings', { tab: activeTab }, true);
    paint();
  }

  function onTabKeydown(event, index) {
    // WAI-ARIA tablist 的标准键盘交互：左右切换，Home/End 到首尾。
    const step = event.key === 'ArrowRight' ? 1 : event.key === 'ArrowLeft' ? -1 : 0;
    let nextIndex = null;
    if (step) nextIndex = (index + step + SETTINGS_TABS.length) % SETTINGS_TABS.length;
    else if (event.key === 'Home') nextIndex = 0;
    else if (event.key === 'End') nextIndex = SETTINGS_TABS.length - 1;
    if (nextIndex == null) return;
    event.preventDefault();
    selectTab(SETTINGS_TABS[nextIndex].id);
    // paint() 重造了 DOM，焦点必须重新落到当前标签上，否则键盘用户会掉出 tablist
    document.getElementById(`tab-${activeTab}`)?.focus();
  }

  /* ---------- 布局 ---------- */

  function buildLayout() {
    return [buildHead(), buildTabs(), buildPanel()];
  }

  /** 标签栏。ARIA tabs 模式：tablist / tab / aria-selected / aria-controls。 */
  function buildTabs() {
    const list = el('div', {
      class: 'tabs',
      attrs: { role: 'tablist', 'aria-label': '设置分区' },
    });

    SETTINGS_TABS.forEach((tab, index) => {
      const selected = tab.id === activeTab;
      const btn = el('button', {
        class: 'tab',
        attrs: {
          type: 'button',
          role: 'tab',
          id: `tab-${tab.id}`,
          'aria-selected': String(selected),
          // 只给选中的标签挂 aria-controls：面板是"只渲染当前分区"，
          // 给未选中的标签也挂上会留下悬空 IDREF（aria-valid-attr-value 会报错）
          ...(selected ? { 'aria-controls': `panel-${tab.id}` } : {}),
          // roving tabindex：tablist 整体只占一个 Tab 停留点
          tabindex: selected ? '0' : '-1',
        },
        on: {
          click: () => selectTab(tab.id),
          keydown: (event) => onTabKeydown(event, index),
        },
      });
      if (tab.icon) btn.append(el('span', { html: icon(tab.icon, 15) }));
      btn.append(el('span', { text: tab.label }));
      // 连接状态直接标在标签上：否则"后端通不通"必须先切到该分区才知道。
      // 颜色不是唯一载体 —— 屏幕阅读器读的是 sr-only 文本。
      if (tab.id === 'connection') {
        const ok = getState('connected');
        statusDot = el('span', {
          class: `dot ${ok ? 'dot-success' : 'dot-danger'}`,
          attrs: { 'aria-hidden': 'true' },
        });
        statusText = el('span', { class: 'sr-only', text: ok ? '已连接' : '未连接' });
        btn.append(statusDot, statusText);
      }
      list.append(btn);
    });

    return list;
  }

  /** 面板容器：`id` 与当前 tab 的 `aria-controls` 对应。 */
  function buildPanel() {
    return el('div', {
      attrs: {
        role: 'tabpanel',
        id: `panel-${activeTab}`,
        'aria-labelledby': `tab-${activeTab}`,
      },
    }, [buildActiveCard()]);
  }

  /** 只构建当前分区的卡片 —— 这是"不再挤在一页"的实质。 */
  function buildActiveCard() {
    switch (activeTab) {
      case 'models':
      case 'backend':
        return renderBackendSettings(activeTab, backendSession);
      case 'appearance':
        return buildAppearanceCard();
      case 'data':
        return buildDataCard();
      case 'about':
        return buildAboutCard();
      case 'connection':
      default:
        return buildConnectionCard();
    }
  }

  function buildHead() {
    return el('div', { class: 'page-head' }, [
      el('div', { class: 'page-head-text' }, [
        el('h1', { text: '设置' }),
      ]),
    ]);
  }

  /* ---------- 连接 ---------- */

  function buildConnectionCard() {
    const body = el('div', { class: 'field', style: { gap: 'var(--space-4)', 'max-width': '560px' } });

    const input = el('input', {
      class: 'input mono',
      attrs: {
        type: 'url',
        id: 'api-base-input',
        placeholder: '如 http://127.0.0.1:8812（留空表示同源）',
        value: getApiBase(),
        spellcheck: 'false',
        autocomplete: 'off',
      },
      on: { input: () => {
        if (input.value.trim().replace(/\/+$/, '') !== getApiBase()) accessKey.value = '';
      } },
    });
    const accessKey = el('input', {
      class: 'input mono', attrs: {
        id: 'api-access-key', type: 'password', value: getApiKey(),
        autocomplete: 'new-password', placeholder: '后端启用访问控制时填写',
      },
    });

    const testBtn = button('测试连接', {
      icon: 'zap',
      variant: 'secondary',
      onClick: async () => {
        const value = input.value.trim().replace(/\/+$/, '');
        testBtn.dataset.loading = 'true';
        const prev = getApiBase();
        const previousKey = getApiKey();
        setApiBase(value);
        setApiKey(accessKey.value);
        const result = await probeHealth();
        if (result.ok) {
          storage.set('devagent.apiBase', value);
          toast({
            tone: result.providers?.length ? 'success' : 'warning',
            title: result.providers?.length ? '连接成功' : '已连接，但未配置模型',
            desc: getState('healthDetail'),
          });
          // 换了后端要清空旧数据，否则会显示上一个后端的内容
          if (prev !== value) resetState();
        } else {
          setApiBase(prev);
          setApiKey(previousKey);
          toast({
            tone: 'error',
            title: '连接失败',
            desc: result.error?.message || '无法访问该地址',
          });
        }
        delete testBtn.dataset.loading;
        paint();
      },
    });

    const saveBtn = button('保存并重载', {
      icon: 'check',
      variant: 'primary',
      onClick: () => {
        const value = input.value.trim().replace(/\/+$/, '');
        storage.set('devagent.apiBase', value);
        toast({ tone: 'info', title: '已保存，正在重载…', duration: 1200 });
        // 用 URL 参数而非仅 localStorage：便于分享带后端的完整链接
        const url = new URL(window.location.href);
        if (value) url.searchParams.set('api', value);
        else url.searchParams.delete('api');
        window.location.replace(url.toString());
      },
    });

    body.append(
      el('div', { class: 'field' }, [
        el('label', { class: 'label', attrs: { for: 'api-base-input' }, text: '后端 API 地址' }),
        el('div', { style: { display: 'flex', gap: 'var(--space-2)', 'flex-wrap': 'wrap' } }, [
          el('div', { class: 'input-group', style: { flex: '1 1 260px' } }, [
            el('span', { class: 'input-icon', html: icon('link', 14) }),
            input,
          ]),
          testBtn,
          saveBtn,
        ]),
        el('div', {
          class: 'field-hint',
          text: '留空表示与前端同源（推荐：把后端反向代理到同一端口，可避免 CORS 与混合内容问题）。',
        }),
      ])
    );
    body.append(el('div', { class: 'field' }, [
      el('label', { class: 'label', attrs: { for: 'api-access-key' }, text: '后端会话访问密钥（X-API-Key）' }),
      accessKey,
      button('应用会话密钥', { variant: 'secondary', small: true, onClick: async () => {
        if (input.value.trim().replace(/\/+$/, '') !== getApiBase()) {
          toast({ tone: 'warning', title: '请先测试新的后端连接' }); return;
        }
        setApiKey(accessKey.value); await probeHealth(); paint();
      } }),
      el('p', { class: 'field-hint', text: '仅用于当前会话，刷新后需重新输入。' }),
    ]));

    // 实时状态
    const connected = getState('connected');
    const providers = getState('providers') || [];
    body.append(
      el('div', {
        style: {
          padding: 'var(--space-3)',
          'border-radius': 'var(--radius-md)',
          background: 'var(--surface-sunken)',
          border: '1px solid var(--border-subtle)',
        },
      }, [
        el('div', { style: { display: 'flex', 'align-items': 'center', gap: 'var(--space-2)', 'margin-bottom': 'var(--space-3)' } }, [
          el('span', { class: `dot ${connected ? 'dot-success' : 'dot-danger'}` }),
          el('span', {
            text: connected ? '已连接' : '未连接',
            style: { 'font-weight': '600', 'font-size': 'var(--fs-sm)' },
          }),
        ]),
        el('div', { class: 'kv' }, [
          kvItem('模型提供商', providers.length ? providers.join('、') : '未配置'),
          kvItem('可观测性', getState('observability') ? '已开启' : '未开启'),
          kvItem('服务版本', getState('version') || '—'),
          kvItem('当前地址', getApiBase() || '同源'),
        ]),
      ])
    );

    if (!connected) {
      body.append(
        alert({
          tone: 'warning',
          title: '后端不可达',
          body: '在项目根目录执行 PYTHONPATH=src python scripts/serve_demo.py 可启动带脚本化模型的演示后端（无需 API Key）。',
        })
      );
    }

    return card({ title: '后端连接', body });
  }

  /* ---------- 外观 ---------- */

  function buildAppearanceCard() {
    const body = el('div', { class: 'field', style: { gap: 'var(--space-4)' } });
    const current = getState('theme');
    const effective = getState('systemDark') ? 'dark' : 'light';

    const options = [
      { id: 'light', label: '亮色', icon: 'sun', desc: '适合明亮环境与打印' },
      { id: 'dark', label: '暗色', icon: 'moon', desc: '长时间编码更护眼，减少屏幕眩光' },
      { id: 'system', label: '跟随系统', icon: 'monitor', desc: `随操作系统自动切换（当前系统为${effective === 'dark' ? '暗色' : '亮色'}）` },
    ];

    const grid = el('div', { class: 'grid grid-cols-3', style: { gap: 'var(--space-3)' } });
    for (const o of options) {
      const active = current === o.id;
      const btn = el('button', {
        class: 'btn',
        attrs: {
          type: 'button',
          'aria-pressed': String(active),
          style: active
            ? 'border-color:var(--brand);background:var(--brand-subtle);color:var(--brand-text);height:auto;padding:var(--space-3);flex-direction:column;align-items:flex-start;gap:var(--space-2);'
            : 'height:auto;padding:var(--space-3);flex-direction:column;align-items:flex-start;gap:var(--space-2);border-color:var(--border-default);background:var(--surface-base);',
        },
        on: {
          click: () => {
            setTheme(o.id);
            toast({ tone: 'info', title: `外观已设为「${o.label}」`, duration: 1800 });
            paint();
          },
        },
      });
      btn.append(
        el('span', {
          style: { display: 'flex', 'align-items': 'center', gap: 'var(--space-2)' },
          html: icon(o.icon, 16),
        })
      );
      btn.append(
        el('span', {}, [
          el('div', { text: o.label, style: { 'font-weight': '600', 'font-size': 'var(--fs-sm)' } }),
          el('div', { class: 'hint-text', text: o.desc, style: { 'text-align': 'left' } }),
        ])
      );
      grid.append(btn);
    }

    body.append(
      el('div', { class: 'field' }, [
        el('div', { class: 'label', text: '主题' }),
        grid,
      ]),
      el('p', {
        class: 'hint-text',
        text: '主题通过 CSS 语义令牌实现，切换时组件样式无需重新加载。同时已尊重系统的「减少动态效果」偏好。',
      })
    );

    const cycleBtn = button('顶栏按钮循环切换', {
      icon: 'refresh',
      variant: 'ghost',
      small: true,
      onClick: () => {
        cycleTheme();
        paint();
      },
    });
    body.append(el('div', {}, [cycleBtn]));

    return card({ title: '外观', subtitle: '主题与视觉偏好', body });
  }

  /* ---------- 数据 ---------- */

  function buildDataCard() {
    const body = el('div', { class: 'field', style: { gap: 'var(--space-4)' } });

    const info = el('div', { class: 'grid grid-cols-3', style: { gap: 'var(--space-3)' } });
    info.append(
      statLine('任务数', String((getState('tasks') || []).length)),
      statLine('Trace span', String((getState('traces') || []).length)),
      statLine('本地偏好项', String(countLocalPrefs()))
    );

    body.append(
      alert({
        tone: 'info',
        title: '数据说明',
        body: '任务的持久化取决于后端 storage 后端：memory 模式重启即清空，'
          + 'sql 模式写入 PostgreSQL。前端不做任何本地任务缓存 —— '
          + '界面显示的永远是服务端的真实状态。',
      }),
      info,
      el('div', { style: { display: 'flex', gap: 'var(--space-2)', 'flex-wrap': 'wrap' } }, [
        button('刷新所有数据', {
          icon: 'refresh',
          variant: 'secondary',
          onClick: async () => {
            const r = await probeHealth();
            toast({
              tone: r.ok ? 'success' : 'error',
              title: r.ok ? '已重新探测后端' : '后端不可达',
              desc: getState('healthDetail'),
            });
            paint();
          },
        }),
        button('清除本地偏好', {
          icon: 'trash',
          variant: 'ghost',
          onClick: async () => {
            const ok = await confirmDialog({
              title: '清除本地偏好',
              message: '将删除浏览器中保存的主题、API 地址、任务草稿等本地数据。任务数据在后端，不受影响。'
                + '清除后页面会重新加载。',
              confirmLabel: '清除',
              danger: true,
            });
            if (!ok) return;
            storage.remove('devagent.theme');
            storage.remove('devagent.apiBase');
            storage.remove('devagent.autorefresh');
            storage.remove('devagent.taskDraft');
            toast({ tone: 'success', title: '已清除，正在重载…', duration: 1000 });
            window.setTimeout(() => {
              const url = new URL(window.location.href);
              url.search = '';
              url.hash = '';
              window.location.replace(url.toString());
            }, 800);
          },
        }),
      ])
    );

    return card({ title: '数据', subtitle: '来源与清理', body });
  }

  function statLine(label, value) {
    return el('div', {}, [
      el('div', { class: 'kv-key', text: label }),
      el('div', { class: 'kv-value tnum', text: value }),
    ]);
  }

  function countLocalPrefs() {
    let n = 0;
    for (const k of ['devagent.theme', 'devagent.apiBase', 'devagent.autorefresh', 'devagent.taskDraft']) {
      if (storage.get(k, null) != null) n += 1;
    }
    return n;
  }

  /* ---------- 关于 ---------- */

  function buildAboutCard() {
    const body = el('div', { class: 'field', style: { gap: 'var(--space-4)' } });

    body.append(
      el('p', {
        class: 'hint-text',
        text: 'DevAgent 是一个以「上下文工程」为核心的多 Agent 协作软件研发助手。'
          + '需求澄清 → 架构设计 → 编码 → 测试 → 代码审查，全流程由独立验证层把关。',
        style: { 'font-size': 'var(--fs-sm)', color: 'var(--text-secondary)', 'line-height': 'var(--lh-relaxed)' },
      })
    );

    const links = el('div', { style: { display: 'flex', gap: 'var(--space-2)', 'flex-wrap': 'wrap' } });
    links.append(
      button('API 文档', {
        icon: 'external-link',
        variant: 'secondary',
        small: true,
        onClick: () => {
          const base = getApiBase() || window.location.origin;
          window.open(`${base}/docs`, '_blank', 'noopener');
        },
      }),
      button('指标导出', {
        icon: 'download',
        variant: 'ghost',
        small: true,
        onClick: () => navigate('observability'),
      })
    );

    body.append(
      el('div', { class: 'kv' }, [
        kvItem('前端形态', '零构建 · 原生 ES 模块'),
        kvItem('设计系统', '语义令牌 + 组件库'),
        kvItem('主题', '亮色 / 暗色 / 跟随系统'),
        kvItem('可访问性', 'WCAG AA · 键盘可达'),
      ]),
      links
    );

    return card({ title: '关于', subtitle: 'DevAgent Console', body });
  }

  paint();

  return () => {
    disposed = true;
    backendSession.disposed = true;
    backendSession.notify = null;
    unsubscribe();
  };
}
