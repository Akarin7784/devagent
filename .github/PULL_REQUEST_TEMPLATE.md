## What

<!-- 这个 PR 改了什么？ -->

## Why

<!-- 为什么需要这个改动？关联 Issue：Closes #123 -->

## How

<!-- 关键实现思路。如果涉及设计取舍，请说明被否决的方案。 -->

## Testing

<!-- 如何验证的？列出运行的命令与结果 -->

```
pytest -q
# → 315 passed

ruff check src tests
mypy src
```

## Checklist

- [ ] `make check`（ruff + mypy --strict）通过
- [ ] `make test` 全部通过
- [ ] 新增功能带测试
- [ ] **Bug 修复带复现该 bug 的回归测试**
- [ ] 公共接口变更已更新 docstring
- [ ] 重大设计变更已撰写 ADR 并更新 `docs/adr/README.md` 索引
- [ ] 没有提交 API Key、真实业务数据或内网地址

## 设计变更专项（如适用）

<!-- 若本 PR 触及以下任一项，请勾选并补充说明 -->

- [ ] 引入了新的上下文污染路径（请说明 Verifier 是否仍被隔离）
- [ ] 新增了模型调用（请说明是否已纳入成本账本与熔断）
- [ ] 新增了失败路径（请说明重试 / 降级 / 部分失败的处理）
- [ ] 新增了机制但未带指标（请说明为何无法度量）

## Screenshots / Logs

<!-- UI 改动请附截图；行为改动请附关键日志 -->
