import assert from 'node:assert/strict';
import { filterTasks, latestSteps, runHeadline, verificationChecks } from './js/task-view.js?v=20261008-live';
import { extractChanges } from './js/pages/workbench.js?v=20261008-live';
import { ROUTES } from './js/store.js?v=20261008-live';

let passed = 0;
function test(name, fn) {
  try { fn(); passed += 1; }
  catch (error) { console.error(`✗ ${name}: ${error.message}`); process.exitCode = 1; }
}
const tasks = [
  { task_id: 'A', goal: '分页 API', status: 'running' },
  { task_id: 'B', goal: '修复取消', status: 'paused' },
  { task_id: 'C', goal: 'API 验证', status: 'succeeded' },
  { task_id: 'D', goal: '新增接口', status: 'pending' },
  { task_id: 'E', goal: '修复列表', status: 'failed' },
];
test('默认进入任务工作台', () => assert.equal(ROUTES[0].id, 'workbench'));
test('任务筛选组合关键词与状态', () => assert.deepEqual(filterTasks(tasks, 'api', 'active').map((t) => t.task_id), ['A']));
test('需关注只包含失败和暂停', () => assert.deepEqual(filterTasks(tasks, '', 'attention').map((t) => t.task_id), ['B', 'E']));
test('筛选不修改原始列表', () => { filterTasks(tasks, ' A '); assert.equal(tasks.length, 5); });
test('空搜索结果不退回全部任务', () => assert.equal(filterTasks(tasks, '不存在').length, 0));

const rejected = { step_id: 'T:N1:verify', agent: 'verifier', verdict: 'reject', failed_criteria: ['完整响应'] };
test('验证主视图显示最新结论，保留原始拒绝记录', () => {
  const detail = { steps: [rejected, { ...rejected, verdict: 'pass', failed_criteria: [] }] };
  assert.equal(verificationChecks(detail).length, 1);
  assert.equal(verificationChecks(detail)[0].passed, true);
  assert.equal(detail.steps[0].verdict, 'reject');
});
test('缺失或未知验证判定不能显示通过', () => {
  for (const verdict of ['', 'PASS', undefined]) assert.equal(verificationChecks({ steps: [{ ...rejected, verdict }] })[0].passed, false);
});
test('不同节点的验证不能相互覆盖', () => assert.equal(verificationChecks({ steps: [rejected, { ...rejected, step_id: 'T:N2:verify' }] }).length, 2));
test('乱序旧尝试不能覆盖最新尝试', () => {
  const newer = { step_id: 'T:N1', attempt: 2, output: 'new' };
  assert.equal(latestSteps([newer, { ...newer, attempt: 1, output: 'old' }])[0].output, 'new');
});

const coder = (attempt, file, code, id = 'N1') => ({
  step_id: `T:${id}`, agent: 'coder', attempt,
  output: `## 1. ${file}\n理由：实现需求\n\`\`\`diff\n+${code}\n\`\`\``,
});
test('代码主视图只展示最新尝试，移除已撤回文件', () => {
  const detail = { steps: [coder(1, 'obsolete.py', 'old'), coder(2, 'current.py', 'new')] };
  const changes = extractChanges(detail, '');
  assert.deepEqual(changes.map((c) => c.file), ['current.py']);
  assert.ok(!changes[0].diff.includes('old'));
});
test('新尝试没有 diff 时不能继续展示旧代码', () => {
  const detail = { steps: [coder(1, 'obsolete.py', 'old'), { step_id: 'T:N1', agent: 'coder', attempt: 2, output: '无法生成改动' }] };
  assert.equal(extractChanges(detail, '')[0].diff, '');
});
test('选中没有产物的节点不会借用其他节点代码', () => {
  assert.equal(extractChanges({ steps: [coder(1, 'a.py', 'new')] }, 'N2').length, 0);
});
test('保留不同节点的产物', () => {
  assert.equal(extractChanges({ steps: [coder(1, 'a.py', 'a'), coder(1, 'b.py', 'b', 'N2')] }, '').length, 2);
});
test('未读取详情时显示同步状态', () => assert.equal(runHeadline(null).title, '正在读取任务'));
test('暂停与失败有可操作的提示，成功不声称发布', () => {
  assert.equal(runHeadline({ status: 'paused' }).tone, 'warning');
  assert.equal(runHeadline({ status: 'failed', error: '超时' }).desc, '超时');
  assert.ok(runHeadline({ status: 'succeeded' }).title.includes('审阅'));
});
test('实时验证节点对应验证状态', () => {
  assert.equal(runHeadline({ status: 'running' }, [{ status: 'verifying', goal: '分页标准' }]).title, '正在独立验证');
});
if (!process.exitCode) console.log(`✓ Agent 工作台测试全部通过（${passed} 个）`);
