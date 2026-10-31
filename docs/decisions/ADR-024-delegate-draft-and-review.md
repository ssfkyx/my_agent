# ADR-024：委派草稿与第二意见（delegate.draft / delegate.review）

- **状态**：已采纳（2026-10-30）
- **实现状态**：已落地（2026-10-30）
- **关联代码**：`agent/tools/delegate.py:DelegateDraftTool`、`DelegateReviewTool`、
  `agent/core/learning.py:REVIEW_SCHEMA`、`agent/core/harness.py:_ROLE_SWITCH_TOOLS`、
  `agent/app.py`（工具注册）
- **关联 ADR**：ADR-014（role 路由）、ADR-023（角色切换与 ROLE_SWITCH）、ADR-013（Review 契约）、
  ADR-015（结构化调用关闭思考）
- **上游文档**：`docs/pre_architecture.md` §6.1（delegate 大类）、R6（隐式换角色必须标 ROLE_SWITCH）

## 背景

`delegate.switch_role` 已经能切换主脑身份，但工具目录中的 `delegate.draft` / `delegate.review`
仍未实现。两者都需要调用次脑（sub）：

- 草稿：让 sub 先写一份候选答案；
- 第二意见：让 sub 按 Review 契约（verdict/score/issues/suggestions）评审一份草稿。

R6 要求“隐式换角色时 supervision 必标 `ROLE_SWITCH`”，因此不能只是悄悄调用 sub。

## 决策

1. **两个工具都使用 `role="review"`**：sub 同时是 reviewer 与 drafter（见 §11.1 模型表）。
   `delegate.draft` 生成 `{"draft": string}`；`delegate.review` 复用
   `core.learning.REVIEW_SCHEMA` 和 `Review.from_payload`，避免两套 Review 契约漂移。
2. **先调用模型，成功后再切换 registry**：如果模型调用/输出校验失败，`RoleRegistry.current`
   保持原值；失败不会留下“已切换但没结果”的半状态。
3. **成功后把当前角色切到 review**：与 `delegate.switch_role("review")` 语义一致；
   不自动切回 main。计划若想回到主脑，必须显式再调 `delegate.switch_role`。
4. **统一 ROLE_SWITCH 事件**：Harness 对 `delegate.switch_role` / `delegate.draft` /
   `delegate.review` 的成功结果都追加 `ROLE_SWITCH`（位于 `TOOL_RESULT` 之后），
   payload 仍为 `{from, to, alias, changed}`；事件不包含模型正文。
5. **错误映射**：缺少/空参数 → `BAD_ARGS`；模型网络或输出结构失败 → `UPSTREAM_ERROR`，
   `retryable=True`，由既有 TurnBudget 重试/询问；未知 review 角色 → `UPSTREAM_ERROR`，不重试。
6. **工具只返回证据**：draft/review 的结果作为 `ToolResult.data` 进入答案提示；
   最终交付仍由 Harness 的 ANSWER 步骤完成。

## 理由

- 复用同一份 REVIEW_SCHEMA 与 `Review.from_payload`，保证“自动评审”和“工具评审”结论同构。
- 成功后才切换，避免失败工具污染后续答案的角色；同时让 ROLE_SWITCH 与真实模型调用一致。
- 不自动切回 main 是有意为之：同一 Turn 内角色是显式状态，隐式回切会让 ledger 难以解释。
- 事件由 Harness 写、工具不写 ledger，保持“工具只返回 ToolResult”的分层约束。

## 代价

- `delegate.draft` / `delegate.review` 会加载 sub；换卡成本与自动评审相同。
- 调用方若忘记切回 main，后续答案/反思会继续用 sub 角色。
- 结构化 schema 可能被模型的长输出截断；失败会进入重试/升级预算，而不是静默降级。

## 验证方式

- 断言：draft 成功返回 draft/from/to/alias/changed；review 成功返回 Review 字段；
  缺参 → `BAD_ARGS` 且不调用模型；模型失败 → `UPSTREAM_ERROR` 且 current 不变；
  Harness 集成中 `delegate.review` 成功后 ledger 有 `ROLE_SWITCH`，答案使用 review 角色。
- 实跑：`t-20261030-132217-829ca9` 中 `delegate.draft` 返回草稿并产生
  `ROLE_SWITCH main -> review alias=sub`；随后自动评审因 LFM 输出非 JSON 降级为
  `ERROR(UPSTREAM_ERROR)`（既有 ADR-013 降级路径，答案仍交付）。
