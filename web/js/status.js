/**
 * 状态与角色的映射表 —— **前端唯一真源**。
 *
 * 为什么单独抽出这个模块（这是一次真实的翻车教训）：
 *
 * 之前 `graph.js`（DAG 节点）和 `components.js`（通用徽章/圆点）各自维护了
 * 一套状态映射表。补 `graph.js` 时漏了 `components.js`，于是同一批
 * `StepStatus` 在两个地方表现不一致：DAG 节点正确显示"校验中"，
 * 而任务列表里的圆点却是中性灰、文案回显英文 `verifying`。
 *
 * 根因不是"忘了改"，而是**同一个知识存在两份**。所以这里把它收敛成一份，
 * 两个消费方都从这里取。跨语言的那一半（Python 枚举）由
 * `test_contract_words.json` 保证同步，见 `status.test.js`。
 *
 * 关键陷阱：所有查表函数**不得**用 `|| 兜底` 掩盖缺项。
 * 缺项必须能被测试发现 —— `statusGroup()` 的兜底值是可断言的常量，
 * 而不是随手一个 `undefined`。
 */

/* ------------------------------------------------------------------ *
 * 步骤状态（对应 Python `devagent.enums.StepStatus`）
 * ------------------------------------------------------------------ */

/**
 * StepStatus → 视觉分组。分组名会拼进 CSS 类名（`dot-<group>`），
 * 所以必须是 `[a-z][a-z0-9-]*` 形式，且每个分组都要有图例解释。
 */
export const STATUS_GROUP = {
  pending: 'pending',
  ready: 'ready', // 依赖已满足，等待调度
  running: 'running',
  verifying: 'running', // 与 running 同组：都在占用资源
  success: 'ok',
  rejected: 'backtrack', // 被驳回，必然触发上游重跑
  failed: 'fail',
  skipped: 'muted',
  backtracked: 'backtrack', // 事件层派生态，非 StepStatus 成员
};

/** 兜底分组。只有真正未知的状态才会落到这里。 */
export const FALLBACK_GROUP = 'pending';

/** 状态 → 中文短标签。漏登记会**回显英文枚举名**，中文界面里很扎眼。 */
export const STATUS_LABEL = {
  pending: '待执行',
  ready: '待调度',
  running: '执行中',
  verifying: '校验中',
  success: '成功',
  rejected: '已驳回',
  failed: '失败',
  skipped: '跳过',
  backtracked: '回退重跑',
};

/**
 * 状态 → 徽章色调（`badge-<tone>`）。取值须是 components.css 里
 * 已定义的 tone：success / danger / warning / info / neutral / brand / running。
 */
export const STATUS_TONE = {
  pending: 'neutral',
  ready: 'info',
  running: 'running',
  verifying: 'running',
  success: 'success',
  rejected: 'warning',
  failed: 'danger',
  skipped: 'neutral',
  backtracked: 'warning',
};

/** 状态 → 图标名（必须存在于 icons.js）。 */
export const STATUS_ICON = {
  pending: 'clock',
  ready: 'clock',
  running: 'loader',
  verifying: 'shield',
  success: 'check-circle',
  rejected: 'refresh',
  failed: 'x-circle',
  skipped: 'ban',
  backtracked: 'refresh',
};

/* ------------------------------------------------------------------ *
 * 任务级状态（对应 `tasks` 表里的 status 字段，**不是** StepStatus）
 * ------------------------------------------------------------------ *
 * 任务的生命周期状态与步骤状态是两个不同的枚举：
 * 任务有 `cancelled`（用户主动取消），步骤没有；
 * 步骤有 `verifying`，任务没有。因此必须分开维护，
 * 但共用的成员（running / success / failed）要保证视觉一致 ——
 * 复用同一批 tone 常量即可，不要各写一遍字面量。
 */

export const TASK_STATUS_BADGE = {
  pending: ['neutral', '待执行', 'clock'],
  running: ['running', '运行中', 'loader'],
  success: ['success', '成功', 'check-circle'],
  succeeded: ['success', '成功', 'check-circle'],
  completed: ['success', '已完成', 'check-circle'],
  failed: ['danger', '失败', 'x-circle'],
  error: ['danger', '失败', 'x-circle'],
  // TaskStatus.PAUSED —— orchestrator 在预算耗尽时把任务终态置为 paused。
  // 漏登记的后果不是报错，而是任务列表回显英文 `paused`（正是本表要防的坑）。
  paused: ['warning', '已暂停', 'pause'],
  cancelled: ['neutral', '已取消', 'ban'],
  skipped: ['neutral', '已跳过', 'ban'],
  backtracked: ['warning', '回退重试', 'refresh'],
};

/** 未知任务状态的兜底（保留原文，不吞掉信息）。 */
export const TASK_STATUS_FALLBACK_TONE = 'neutral';

/* ------------------------------------------------------------------ *
 * 角色
 * ------------------------------------------------------------------ */

/** AgentType → 中文标签。漏登记会回显英文枚举名。 */
export const AGENT_LABEL = {
  orchestrator: '编排',
  requirement: '需求',
  architect: '架构',
  coder: '编码',
  tester: '测试',
  verifier: '验证',
  reviewer: '审查',
};

/**
 * AgentType → 稳定配色。
 *
 * 按**角色**而非状态着色，避免状态与角色两种语义挤在同一个视觉通道上。
 * 必须覆盖 AgentType 全集：漏登记会让多个角色共用同一个兜底灰，
 * 于是"编排器"和"评审员"长得一样，图例失去意义。
 */
export const AGENT_COLOR = {
  orchestrator: '#8b7bd8', // 编排器：紫，只做调度不参与执行
  requirement: '#c77dd8', // 需求澄清：洋红，任务最上游
  architect: '#4aa3df', // 方案设计：蓝，产出 DAG 本身
  coder: '#5b8cff', // 编码：亮蓝，图上占比最大的角色
  tester: '#e0a83c', // 测试：橙
  verifier: '#35c48a', // 独立验证：绿，与 coder 的蓝刻意拉开
  reviewer: '#d98b5f', // 代码审查：棕橙，与 tester 的橙区分
};

export const AGENT_FALLBACK_COLOR = '#7a8291';

/* ------------------------------------------------------------------ *
 * 查表函数
 * ------------------------------------------------------------------ */

export function statusGroup(status) {
  return STATUS_GROUP[status] || FALLBACK_GROUP;
}

export function statusLabel(status) {
  return STATUS_LABEL[status] || status || FALLBACK_GROUP;
}

export function statusTone(status) {
  return STATUS_TONE[status] || 'neutral';
}

export function statusIcon(status) {
  return STATUS_ICON[status] || '';
}

/** 状态点的色调类名（`dot-<tone>`）。 */
export function statusDotClass(status) {
  return `dot-${statusTone(status)}`;
}

/**
 * 任务状态 → 徽章。统一映射，避免各页面各写一套。
 * @returns {[tone: string, label: string, icon: string]}
 */
export function taskStatusBadge(status) {
  return TASK_STATUS_BADGE[status]
    || [TASK_STATUS_FALLBACK_TONE, status || '未知', ''];
}

/**
 * 任务状态 → 中文标签（只要文字，不要徽章）。
 *
 * 存在的理由：任务级行（任务列表、执行摘要）**不能**用 `statusLabel()`。
 * 那是 StepStatus 的表，`succeeded` / `cancelled` / `paused` 在里面都没有
 * 条目，于是 `statusLabel()` 会把英文枚举名原样回显到中文界面上。
 * 两套状态共用同一个字面量 `running`，正是这个巧合让该 bug 长期没被发现。
 *
 * 与 `taskStatusBadge()` 共用同一张表 —— 不允许出现第二份文案。
 */
export function taskStatusLabel(status) {
  return taskStatusBadge(status)[1];
}

export function agentLabel(type) {
  return AGENT_LABEL[type] || type || '未知';
}

export function agentColor(agentType) {
  return AGENT_COLOR[agentType] || AGENT_FALLBACK_COLOR;
}
