/**
 * 评测中心。
 *
 * 评测是「证明系统真的有效」的手段，而不是一个装饰性按钮。
 * 因此本页强调三件事：
 *   1. 评测指标的口径（成功率 ≠ 一次通过率 ≠ Judge 通过率）
 *   2. 成本与耗时（跑一次评测要花多少钱，必须先告诉用户）
 *   3. 分类别下钻（哪个场景类型是短板）
 *
 * 评测是**长耗时**操作（可能数分钟），因此：
 *   - 运行期间禁用表单并显示明确的进度提示；
 *   - 支持中途查看已有结果；
 *   - 超时放宽到 5 分钟。
 */

import { api } from '../api.js';
import {
  alert,
  badge,
  button,
  card,
  emptyState,
  errorState,
  metricRow,
  skeletonBlock,
  statCard,
  toast,
} from '../components.js';
import { icon } from '../icons.js';
import { getState, navigate, set } from '../store.js';
import { clear, el, fmtCost, fmtInt, fmtPct, fromHTML, mount } from '../util.js';

/** 可选的评测类别 —— 与 golden set 的 category 字段一致。 */
const CATEGORY_OPTIONS = [
  { id: 'requirement', label: '需求澄清', desc: '检验不可验收词的过滤能力（forbidden_criteria）' },
  { id: 'context', label: '上下文装配', desc: '高冗余输入下的去重与非线性冗余惩罚' },
  { id: 'scale', label: '规模压力', desc: '超大上下文下的预算分配与压缩阈值触发' },
];

export default async function renderEval(root, ctx) {
  const container = el('div');
  root.append(container);
  let disposed = false;

  // 表单状态（本地）
  const form = {
    dataset: '',
    categories: new Set(),
    maxSamples: '',
    useJudge: true,
  };

  function paint() {
    if (disposed) return;
    clear(container);
    container.append(buildLayout());
  }

  /* ---------- 布局 ---------- */

  function buildLayout() {
    const wrap = el('div');

    wrap.append(
      el('div', { class: 'page-head' }, [
        el('div', { class: 'page-head-text' }, [
          el('h1', { text: '评测中心' }),
          el('p', {
            class: 'page-head-desc',
            text: '基于 golden set 对系统做任务级、轨迹级与质量级评测。评测是判断「改动是否真的变好了」的唯一手段。',
          }),
        ]),
      ])
    );

    const result = getState('evalResult');

    wrap.append(
      el('div', {
        class: 'grid',
        style: {
          'grid-template-columns': 'minmax(320px, 400px) minmax(0, 1fr)',
          gap: 'var(--space-4)',
          'align-items': 'start',
        },
      }, [buildFormCard(), result ? buildResultPanel(result) : buildEmptyResult()])
    );

    return wrap;
  }

  /* ---------- 评测表单 ---------- */

  function buildFormCard() {
    const body = el('div', { class: 'field', style: { gap: 'var(--space-4)' } });
    // 所有控件显示值都从 snapshotFormState 取 —— 与提交用的 payload 同源。
    // 这样"看得见的值"与"提交的值"不可能再分叉（样本上限就曾分叉过）。
    const { fields } = snapshotFormState(form);

    // 数据集路径
    const datasetInput = el('input', {
      class: 'input',
      attrs: {
        type: 'text',
        id: 'dataset-path',
        placeholder: '留空使用配置中的默认数据集',
        value: fields.dataset,
      },
    });
    datasetInput.addEventListener('input', () => {
      form.dataset = datasetInput.value.trim();
    });
    body.append(
      el('div', { class: 'field' }, [
        el('label', { class: 'label', attrs: { for: 'dataset-path' } }, [
          el('span', { text: '数据集路径' }),
          el('span', { class: 'optional', text: '可选' }),
        ]),
        datasetInput,
        el('div', { class: 'field-hint', text: '默认读取 datasets/golden_set.jsonl（JSONL 格式，每行一个样本）' }),
      ])
    );

    // 类别多选
    const catWrap = el('div', { class: 'field' });
    catWrap.append(el('div', { class: 'label', text: '类别筛选' }));
    const catList = el('div', { style: { display: 'flex', 'flex-direction': 'column', gap: 'var(--space-2)' } });
    for (const c of CATEGORY_OPTIONS) {
      // checked 同样要回填：类别筛选是"看起来没选、实际仍然生效"的同一类缺陷
      const cb = el('input', {
        attrs: {
          type: 'checkbox',
          value: c.id,
          id: `cat-${c.id}`,
          checked: fields.categories.includes(c.id),
        },
      });
      cb.addEventListener('change', () => {
        if (cb.checked) form.categories.add(c.id);
        else form.categories.delete(c.id);
      });
      catList.append(
        el('label', { class: 'checkbox', attrs: { for: `cat-${c.id}`, title: c.desc } }, [
          cb,
          el('span', {}, [
            el('span', { text: c.label }),
            el('span', { class: 'hint-text', text: ` — ${c.desc}`, style: { 'margin-left': '6px' } }),
          ]),
        ])
      );
    }
    catWrap.append(catList);
    catWrap.append(el('div', { class: 'field-hint', text: '不选则评测全部类别' }));
    body.append(catWrap);

    // 样本上限
    // `value` 必须回填：paint() 会重建整张表单，而 form.maxSamples 是本地状态。
    // 不回填的后果是"输入框看起来是空的，但提交时旧值仍然生效" ——
    // 界面与 payload 不一致，用户会以为上限被清掉了。
    const maxInput = el('input', {
      class: 'input',
      attrs: { type: 'number', min: '1', id: 'max-samples', placeholder: '不限', value: fields.maxSamples },
    });
    maxInput.addEventListener('input', () => {
      form.maxSamples = maxInput.value.trim();
    });
    body.append(
      el('div', { class: 'field' }, [
        el('label', { class: 'label', attrs: { for: 'max-samples' } }, [
          el('span', { text: '样本上限' }),
          el('span', { class: 'optional', text: '可选' }),
        ]),
        maxInput,
        el('div', { class: 'field-hint', text: '小样本量试跑（如 5）可快速验证改动，避免一次烧掉大量 token' }),
      ])
    );

    // Judge 开关
    const judgeCb = el('input', { attrs: { type: 'checkbox', checked: fields.useJudge, id: 'use-judge' } });
    judgeCb.addEventListener('change', () => {
      form.useJudge = judgeCb.checked;
    });
    body.append(
      el('label', { class: 'switch', attrs: { for: 'use-judge' } }, [
        judgeCb,
        el('span', { class: 'switch-track' }),
        el('span', { class: 'switch-label' }, [
          el('div', { text: '启用 LLM Judge 质量评分' }),
          el('div', { class: 'hint-text', text: '关闭可省一半成本，但失去质量级评测与判别力指标' }),
        ]),
      ])
    );

    body.append(
      alert({
        tone: 'warning',
        title: '评测会真实调用模型，产生费用',
        body: '评测同步执行，样本量大时可能耗时数分钟。建议先用「样本上限 = 5」验证链路，再放开全量。',
      })
    );

    const runBtn = button('开始评测', {
      icon: 'play',
      variant: 'primary',
      onClick: () => runEvaluation(),
    });
    body.append(el('div', {}, [runBtn]));

    // 运行中禁用整张卡片
    if (getState('evalRunning')) {
      body.querySelectorAll('input, button').forEach((n) => {
        n.disabled = true;
      });
      runBtn.dataset.loading = 'true';
      runBtn.setAttribute('aria-busy', 'true');
    }

    return card({ title: '评测配置', body });
  }

  function buildEmptyResult() {
    return card({
      title: '评测结果',
      body: emptyState({
        icon: 'flask',
        title: '尚未运行评测',
        desc: '配置左侧参数后点击「开始评测」。也可以直接在命令行运行：make cli-eval，或 python -m devagent.cli eval。',
      }),
    });
  }

  /* ---------- 执行 ---------- */

  async function runEvaluation() {
    if (getState('evalRunning')) return;
    set({ evalRunning: true });
    paint();

    const { payload } = snapshotFormState(form);

    try {
      const result = await api.runEvaluation(payload);
      set({ evalResult: result });
      toast({
        tone: 'success',
        title: '评测完成',
        desc: `共 ${fmtInt(result.total)} 个样本，成功率 ${fmtPct(result.success_rate)}`,
      });
    } catch (err) {
      toast({
        tone: 'error',
        title: '评测失败',
        desc: err?.message || String(err),
        duration: 9000,
      });
    } finally {
      set({ evalRunning: false });
      paint();
    }
  }

  /* ---------- 结果面板 ---------- */

  function buildResultPanel(r) {
    const wrap = el('div', { style: { display: 'flex', 'flex-direction': 'column', gap: 'var(--space-4)' } });

    // KPI 行
    const kpis = el('div', { class: 'grid grid-cols-4' });
    kpis.append(
      statCard({
        label: '任务成功率',
        value: fmtPct(r.success_rate, 1),
        icon: 'target',
        tone: r.success_rate >= 0.8 ? 'success' : r.success_rate >= 0.5 ? 'warning' : 'danger',
        foot: `${fmtInt(r.total)} 个样本`,
      }),
      statCard({
        label: '一次通过率',
        value: fmtPct(r.first_pass_rate, 1),
        icon: 'award',
        foot: '无需回退重试的比例',
      }),
      statCard({
        label: 'Judge 通过率',
        value: r.judge_model || r.judge_pass_rate ? fmtPct(r.judge_pass_rate, 1) : '—',
        icon: 'shield-check',
        foot: r.judge_model ? `裁判模型：${r.judge_model}` : '未启用 Judge',
      }),
      statCard({
        label: '平均 Judge 分',
        value: r.mean_judge_score ? r.mean_judge_score.toFixed(2) : '—',
        icon: 'percent',
        foot: '质量级评测均分',
      })
    );
    wrap.append(kpis);

    // 详细指标
    const detail = el('div', { class: 'metric-list' });
    detail.append(
      metricRow({
        name: '上下文平均节省',
        value: fmtPct(r.mean_context_savings, 1),
        ratio: r.mean_context_savings,
        tone: 'success',
      }),
      metricRow({
        name: 'Judge 判定不一致率',
        value: fmtPct(r.inconsistent_judge_rate, 1),
        ratio: r.inconsistent_judge_rate,
        tone: r.inconsistent_judge_rate > 0.2 ? 'warning' : '',
      }),
      metricRow({
        name: '判别力（高分与低分样本的分离度）',
        value: typeof r.discrimination === 'number' ? r.discrimination.toFixed(3) : '—',
      })
    );

    const costRow = el('div', {
      class: 'grid grid-cols-2',
      style: { gap: 'var(--space-4)', 'margin-top': 'var(--space-4)' },
    }, [
      el('div', {}, [
        el('div', { class: 'kv-key', text: '总 token 消耗' }),
        el('div', { class: 'kv-value tnum', text: fmtInt(r.total_tokens) }),
      ]),
      el('div', {}, [
        el('div', { class: 'kv-key', text: '总成本' }),
        el('div', { class: 'kv-value tnum', text: fmtCost(r.total_cost_usd) }),
      ]),
    ]);

    wrap.append(
      card({
        title: '质量指标',
        subtitle: r.dataset ? `数据集：${r.dataset}` : '',
        body: el('div', {}, [detail, costRow]),
      })
    );

    // 分类别下钻
    const cats = r.categories || {};
    const catKeys = Object.keys(cats);
    if (catKeys.length) {
      const rows = catKeys.map((k) => {
        const c = cats[k] || {};
        return el('tr', {}, [
          el('td', { class: 'primary', dataset: { label: '类别' }, text: k }),
          el('td', { class: 'num', dataset: { label: '样本数' }, text: fmtInt(c.total ?? c.count ?? 0) }),
          el('td', {
            class: 'num',
            dataset: { label: '成功率' },
            text: c.success_rate != null ? fmtPct(c.success_rate, 1) : '—',
          }),
          el('td', {
            class: 'num',
            dataset: { label: '平均分' },
            text: c.mean_judge_score != null ? Number(c.mean_judge_score).toFixed(2) : '—',
          }),
        ]);
      });

      const table = el('table', { class: 'table' }, [
        el('thead', {}, [
          el('tr', {}, [
            el('th', { text: '类别' }),
            el('th', { class: 'num', text: '样本数' }),
            el('th', { class: 'num', text: '成功率' }),
            el('th', { class: 'num', text: '平均分' }),
          ]),
        ]),
        el('tbody', {}, rows),
      ]);

      wrap.append(
        card({
          title: '分类别表现',
          subtitle: '定位短板场景',
          body: el('div', { class: 'table-wrap table-wrap-stack' }, [table]),
          flush: true,
        })
      );
    }

    // 判别力提示
    if (typeof r.discrimination === 'number') {
      const weak = r.discrimination < 0.3;
      wrap.append(
        alert({
          tone: weak ? 'warning' : 'success',
          title: weak ? '判别力偏低：Judge 可能区分不出好坏答案' : '判别力良好：Judge 能有效区分质量差异',
          body: weak
            ? '判别力低于 0.3 时，Judge 的分数意义有限。可尝试更换裁判模型，或细化评分标准（rubric）。这是「用 LLM 评 LLM」时最容易被忽略的失效模式。'
            : `分离度 ${r.discrimination.toFixed(3)}，说明高分样本与低分样本在 Judge 视角下差异明显，评测结论可信。`,
        })
      );
    }

    return wrap;
  }

  paint();

  return () => {
    disposed = true;
  };
}

export { badge, fromHTML, navigate, skeletonBlock, errorState };

/* ------------------------------------------------------------------ *
 * 纯函数助手（可单测）
 * ------------------------------------------------------------------ */

/**
 * 把表单状态编译成「请求 payload」与「重绘时各控件应显示的值」。
 *
 * ## 为什么需要这个函数
 *
 * 本页的表单状态是本地 `form` 对象，而 `paint()` 会整套重建 DOM。
 * 两者一旦不同步，就会出现最阴险的一类 bug：**看得见的值与提交的值不一致**。
 * 真实发生过：`样本上限` 输入框重建时忘了回填 `value`，
 * 于是字段看起来是空的，提交时却仍然带着上次的上限值 ——
 * 用户以为清空了上限，实际只跑了一小部分样本。
 *
 * 把"payload"和"控件显示值"放在同一个函数里返回，任何新增字段都
 * 被迫同时考虑这两侧；测试也只需断言一次（见 pages.test.js）。
 *
 * @param {{dataset?: string, categories?: Set<string>, maxSamples?: string, useJudge?: boolean}} form
 * @returns {{payload: object, fields: {dataset: string, maxSamples: string, useJudge: boolean, categories: string[]}}}
 */
export function snapshotFormState(form = {}) {
  const dataset = String(form.dataset ?? '').trim();
  const maxSamples = String(form.maxSamples ?? '').trim();
  const categories = [...(form.categories || [])];
  const useJudge = form.useJudge !== false;

  return {
    payload: {
      ...(dataset ? { dataset_path: dataset } : {}),
      ...(categories.length ? { categories } : {}),
      ...(maxSamples ? { max_samples: Number(maxSamples) } : {}),
      use_judge: useJudge,
    },
    // 这一份就是 paint() 时必须回填到控件上的值，必须与 payload 同源
    fields: { dataset, maxSamples, useJudge, categories },
  };
}
