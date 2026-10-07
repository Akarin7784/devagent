/**
 * 设置页。
 *
 * 三组配置：后端连接（含连通性测试）、外观偏好、数据刷新策略。
 *
 * 设计原则：**每一项都必须有明确的作用说明与生效时机**。
 * 设置项不说清「改了什么、什么时候生效」，用户就只能靠试错 ——
 * 这是设置页面最常见的可用性缺陷。
 */

import { getApiBase, setApiBase } from '../api.js';
import { cycleTheme, probeHealth, setTheme } from '../shell.js';
import {
  alert,
  button,
  card,
  confirmDialog,
  kvItem,
  toast,
} from '../components.js';
import { icon } from '../icons.js';
import { getState, navigate, resetState } from '../store.js';
import { clear, el, mount, storage } from '../util.js';

export default async function renderSettings(root, ctx) {
  const container = el('div');
  root.append(container);
  let disposed = false;

  function paint() {
    if (disposed) return;
    clear(container);
    mount(container, ...buildSections());
  }

  /* ---------- 分区 ---------- */

  function buildSections() {
    return [
      buildHead(),
      buildConnectionCard(),
      buildAppearanceCard(),
      buildDataCard(),
      buildAboutCard(),
    ];
  }

  function buildHead() {
    return el('div', { class: 'page-head' }, [
      el('div', { class: 'page-head-text' }, [
        el('h1', { text: '设置' }),
        el('p', {
          class: 'page-head-desc',
          text: '后端连接、外观偏好与数据刷新策略。所有偏好保存在浏览器本地，不会上传到服务端。',
        }),
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
    });

    const testBtn = button('测试连接', {
      icon: 'zap',
      variant: 'secondary',
      onClick: async () => {
        const value = input.value.trim().replace(/\/+$/, '');
        testBtn.dataset.loading = 'true';
        const prev = getApiBase();
        setApiBase(value);
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

    return card({ title: '后端连接', subtitle: '决定数据来自哪里', body });
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
              message: '将删除浏览器中保存的主题、API 地址等偏好。任务数据在后端，不受影响。'
                + '清除后页面会重新加载。',
              confirmLabel: '清除',
              danger: true,
            });
            if (!ok) return;
            storage.remove('devagent.theme');
            storage.remove('devagent.apiBase');
            storage.remove('devagent.autorefresh');
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
    for (const k of ['devagent.theme', 'devagent.apiBase', 'devagent.autorefresh']) {
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

  void ctx;

  return () => {
    disposed = true;
  };
}
