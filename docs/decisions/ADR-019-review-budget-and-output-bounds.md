# ADR-019：评审输出必须有界，预算独立于主脑

- **状态**：已采纳（2026-10-30）；数值部分由 ADR-035 取代（2026-10-30：1536 → 8192）
- **关联代码**：`agent/core/learning.py:_REVIEW_SCHEMA`、`_REVIEW_PROMPT`、`agent/config.py:AppConfig.review_max_tokens`、`agent/config/defaults.toml`（`[harness] review_max_tokens`）
- **关联 ADR**：ADR-013（评审契约）、ADR-015（评审关闭思考）、ADR-012（主脑 token 预算）、ADR-018（反思共用该预算）、ADR-035（token 预算统一 8192）
- **上游文档**：`docs/pre_architecture.md` §5.3

## 背景

TB-4 首次实跑时评审失败，错误为：

```
review unavailable: model content is not JSON: { "verdict": "REVISE", "score": 0, "issues": [ "The …
```

即 LFM2.5 输出了**合法开头但被截断**的 JSON——`issues` 里写了长段落，撞上 `max_tokens=1024`。
这与 ADR-015 记录的"content 为空"是同一预算问题的另一种表现：**输出格式对，但长度失控**。

## 决策

1. 评审/反思的 token 预算独立配置：`[harness] review_max_tokens = 1536`（主脑仍是 `max_tokens = 1024`）。
2. schema 层面加界：`issues`/`suggestions` 各 `maxItems: 3`，元素 `maxLength: 240`。
3. 提示词明确要求简短，并说明"长文会截断你的 JSON"（让模型知道代价）。
4. 不引入"截断后修复 JSON"的逻辑：修补残缺 JSON 等于猜测，会让评审结论不可信。

> **后续修订（2026-10-30）**：ADR-035 将 `review_max_tokens` 提升到 8192，
> 主脑 `max_tokens` 同步为 8192。第 2 条的 schema 上界仍是第一道约束，预算放大不等于允许无限写长。

## 理由

- 评审的价值在于**结论与可引用的缺陷**，不在篇幅；给出上界既省时间（LFM 约 210 tok/s）也降低截断概率。
- 预算独立于主脑，是因为两者的输出形状不同（主脑答用户，次脑答结构化结论），共用预算会互相拖累。
- 拒绝 JSON 修复：ADR-001 的"严格校验"原则——形状不合法就该暴露，让降级路径（ERROR + review=null）生效。

## 代价

- 最坏情况评审多花约 5 s（1536 vs 1024 tokens）。
- `maxItems/maxLength` 未必被 llama.cpp 的 GBNF 全部强制执行，仍可能出现超长（提示词是第一道闸）。

## 验证方式

- 单测：`Review.from_payload` 对超长 issues 仍接受（长度是提示约束，不是契约拒绝项）。
- 实跑：连续两轮 `--once`，`REVIEW` 事件存在且 `verdict` 非空；`runs/memory.jsonl` 出现对应反思行。
