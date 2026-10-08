/** Task presentation decisions shared by the workbench and its regression tests. */
export const TASK_FILTERS = [
  { id: 'all', label: '全部' },
  { id: 'active', label: '进行中' },
  { id: 'attention', label: '需关注' },
];

export function filterTasks(tasks, query = '', filter = 'all') {
  const term = query.trim().toLocaleLowerCase();
  return tasks.filter((task) => {
    const matches = !term || `${task.goal} ${task.task_id}`.toLocaleLowerCase().includes(term);
    const inGroup = filter === 'active' ? ['pending', 'running'].includes(task.status)
      : filter === 'attention' ? ['paused', 'failed'].includes(task.status) : true;
    return matches && inGroup;
  });
}

/** Keep the latest attempt of each logical step, including attempts with no diff. */
export function latestSteps(steps = []) {
  const latest = new Map();
  for (const step of steps) {
    const previous = latest.get(step.step_id);
    if (!previous || (step.attempt || 1) >= (previous.attempt || 1)) latest.set(step.step_id, step);
  }
  return [...latest.values()];
}

export function verificationChecks(detail) {
  return latestSteps(detail?.steps || []).filter((step) => step.agent === 'verifier').map((step) => ({
    ...step,
    // A missing verdict must never appear as a successful validation.
    passed: step.verdict === 'pass',
    label: step.verdict === 'pass' ? '通过' : step.verdict === 'reject' ? '未通过' : '无有效结论',
  }));
}

export function runHeadline(detail, nodes = []) {
  if (!detail) return { title: '正在读取任务', desc: '同步执行状态与产物…', tone: 'neutral' };
  if (detail.status === 'succeeded') return {
    title: '执行完成，产物可审阅', desc: '查看代码改动与验证记录，再决定下一步。', tone: 'success',
  };
  if (detail.status === 'paused') return {
    title: '执行已暂停，需要关注', desc: detail.error || '检查预算和执行记录，调整需求后可创建新任务。', tone: 'warning',
  };
  if (detail.status === 'failed') return {
    title: '本次执行未完成', desc: detail.error || '查看失败记录，修改需求后重新发起。', tone: 'danger',
  };
  if (detail.status === 'cancelled') return {
    title: '任务已取消', desc: '已有产物和执行记录仍可查看。', tone: 'neutral',
  };
  const active = nodes.find((node) => ['running', 'verifying'].includes(node.status));
  return {
    title: active ? (active.status === 'verifying' ? '正在独立验证' : 'Agent 正在执行') : '任务处理中',
    desc: active?.goal || (nodes.length ? '等待就绪节点执行，状态会实时更新。' : '正在澄清需求并生成执行计划。'),
    tone: 'running',
  };
}
