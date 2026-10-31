# ADR-041：Agents_opinion 主脑意见档案

- 状态：已落地（2026-10-31）
- 关联：ADR-039（Phase 0）、ADR-040（Phase 1 optimizer 协议）

## 背景

主脑可以提出 harness 修改方向，但不能可靠地把提案落地为正确 patch。
Phase 1 协议已经能记录结构化实验提案，但这些提案仍只存在 JSONL 队列里，
不适合作为人类阅读的“主脑意见实践档案”。

用户决定：

> 将主脑反馈记录在 `Agents_opinion`，作为主脑有能力提出 harness 修改方向的实践。  
> 自进化机制到此为止。

## 决策

### 1. 新增 Agents_opinion

目录：

```text
Agents_opinion/
├── README.md
├── INDEX.md
└── <timestamp>-<proposal_id>.md
```

每收到一条 `harness.propose_change`：

```text
写 proposals.jsonl
→ 同时渲染 Markdown 到 Agents_opinion
→ 更新 INDEX.md
```

### 2. 记录内容

- proposal_id / schema_version
- target_kind / target / risk / status
- 当前行为
- 根因
- alternatives 与 tradeoff
- 选中的 proposal
- expected_metric
- replay_plan
- rollback

兼容 v1 提案：缺失字段显示“未记录”，不阻断归档。

### 3. 隔离

`Agents_opinion`：

- 不进入 RAG 知识库（`[knowledge].exclude` 加 `Agents_opinion/*`）
- 被 guardrail 拒绝给 agent 读取（`deny_names` 加 `Agents_opinion`）
- 只由 `OpinionLog` 追加写
- 不自动应用、不自动合并

这样避免主脑读取自己的意见形成自反馈循环。

### 4. 已有提案迁移

现有 3 条提案已回填：

```text
p-1793403614-59a8919e   v1
p-1793404545-b3e6718f   v2，target 不存在
p-1793404750-f378b9bb   v2，成功验证
```

它们是“主脑能提出方向，但不等于能落地”的实践证据。

## 自进化机制冻结

本 ADR 之后停止继续扩展自进化：

- 不实现 patch 自动应用
- 不实现 ProposalReviewer
- 不实现 baseline/candidate A/B controller
- 不实现自动合并/灰度/回滚控制器

保留的只有：

```text
主脑提出方向 → 结构化提案 → Agents_opinion 归档 → 人工阅读
```

## 验证

```text
python3 tests/run_all.py
Ran 30 tests ... OK
```

新增 `tests/test_opinions.py`：

- v2 提案渲染与索引幂等
- v1 提案兼容
- `Agents_opinion` 被 guardrail 拒绝

`Agents_opinion/` 当前有 3 条提案。
