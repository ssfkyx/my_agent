# ADR-040：Phase 1 optimizer 提案协议

- 状态：已落地（2026-10-31）
- 关联：ADR-039（Phase 0 观察层）、ADR-037（评审门禁）、ADR-038（RAG 基线）

## 背景

Phase 0 的 `harness.propose_change` 只有：

```text
target_kind / target / rationale / proposal / expected_effect / tests / risk
```

它能入队，但不足以支撑“可验证的架构实验”：

- 没有记录当前行为
- 没有根因假设
- 没有多个方案和取舍
- 预期效果不可测
- 没有 replay 计划
- 没有明确回滚

因此升级为 Phase 1 optimizer 协议。

## 协议

```text
ChangeProposal v2
├── proposal_id
├── schema_version = 2
├── target_kind
├── target                  # 精确文件/符号/tool/config key
├── current_behavior        # 已观察到的行为，必须有证据
├── root_cause              # 解释失败机制，不是复述症状
├── alternatives[]          # 至少 2 个
│   ├── option
│   ├── description
│   └── tradeoff
├── proposal                # 选中的改动/说明/diff
├── expected_metric
│   ├── metric
│   ├── baseline
│   ├── target
│   └── method
├── replay_plan[]           # 可复现命令/案例
├── risk                    # low | medium | high
├── rollback
├── status = proposed
├── created_at
└── source_call
```

校验规则：

- `alternatives` 至少 2 个、最多 5 个，option 不得重复
- `expected_metric` 四个字段都必填
- `replay_plan` 至少 1 项
- `target` 如果是 `.py` / `.toml` 路径，必须真实存在
- `risk` 只能是 low/medium/high

## 工具行为

`harness.propose_change`：

- 仍然只写 `runs/evolution/proposals.jsonl`
- 不应用 patch
- 不修改源码/prompt/config
- 返回完整 proposal，便于 answer/reviewer 引用
- 路径目标不存在时返回 `BAD_ARGS`

## 上下文与评审

- `_compact_answer_payload` 对 proposal 结果不做“preview 截断”，
  保留完整结构化提案
- answer 可以概述 proposal
- Phase 1 仍然没有独立的 ProposalReviewer；当前 REVIEW 只检查回答是否有工具证据

## 验证

测试：

```text
python3 tests/run_all.py
Ran 27 tests ... OK
```

真实对话：

### 失败样例：目标不存在

```text
t-20261031-005519-c1fc73
proposal: p-1793404545-b3e6718f
target: src/agents/planner.py   # 不存在
```

- 工具当时仍入队
- answer 只确认 queued，没有概述字段
- REVIEW FAIL
- 随后补上 target 路径校验和完整 proposal 返回

### 成功样例

```text
t-20261031-005848-ddf02c
proposal: p-1793404750-f378b9bb
target: agent/core/harness.py:_PLANNER_PROMPT
REVIEW PASS 1.00
47.46 s
retries=0 failures=0
```

提案包含：

- 2 个 alternatives
- `expected_metric: 3/10 → 0/30`
- 3 项 replay plan
- rollback

## 仍然不足

- `current_behavior` / `root_cause` 仍可能泛化，没有强制引用具体代码行
- `baseline` 可能来自模型推断，而不是 ledger 统计
- `replay_plan` 仍可能是自然语言，而不是可执行命令
- REVIEW 不是独立的 ProposalReviewer
- 没有自动 replay/A-B
- 没有 patch 应用

## 后续

Phase 1 真正执行层：

- 隔离 worktree 应用 patch
- 自动跑 replay_plan
- baseline vs candidate 指标
- 独立 ProposalReviewer
- 人工批准
- 可回滚合并
