# ADR-039：Harness 演进 Phase 0（只观察、只提案）

- 状态：已落地（2026-10-31）
- 关联：ADR-018（反思与记忆）、ADR-037（评审门禁）、ADR-038（RAG 基线）

## 背景

主脑权重已冻结，不能靠模型自训练提升。能演进的是 harness：
prompt、tool schema、tool 实现、配置、测试和文档。

现有 `memory.request_tool_change` 只能记录一条“tool + reason”，
`Reflection` 只写 lessons，没有结构化变更提案、失败日志和只读观察入口。
因此先做 Phase 0：**只观察、只提案，不自动应用**。

## 决策

### 1. ChangeProposal

新增 `agent/core/evolution.py`：

```text
target_kind: prompt | tool_schema | tool_impl | config | test | docs
target
rationale
proposal      # 变更说明或 unified diff
expected_effect
tests[]
risk: low | medium | high
status: proposed
```

提案保存到：

```text
runs/evolution/proposals.jsonl
```

`EvolutionStore` 只有 `queue/load`，**没有 apply**。

### 2. 主脑可调用 `harness.propose_change`

新增工具：

```text
harness.propose_change
```

它只把提案写进队列并返回 `proposal_id`：

- 不修改工具注册表
- 不修改 prompt/config
- 不修改源码
- 不执行 patch
- 不影响当前 turn 的答案

`memory.request_tool_change` 保留为旧的轻量入口。

### 3. 硬失败日志

所有 `ERROR` 事件（包括 `_fail` 的硬失败）现在额外写入：

```text
runs/evolution/failures.jsonl
```

字段：

```json
{
  "turn_id": "...",
  "user_text": "...",
  "code": "PLAN_INVALID | ABORTED | ...",
  "message": "...",
  "state": "FAILED",
  "ledger": "runs/<turn>.jsonl",
  "created_at": 0.0
}
```

这样初次规划失败、取消等“没有走到 REFLECTION”的轮次也不会丢证据。

### 4. Reflection 增加 change_request

`Reflection` 新增可选字段：

```text
change_request: str
```

反思 prompt 要求：如果存在一个具体的 harness/tool/prompt 改动能避免本次失败，
就用一句话提出；否则留空。该字段只是提示，不是可执行 patch。

### 5. 只读观察入口

新增：

```bash
python3 -m agent --evolve
python3 -m agent --evolve --json
```

只读取 proposals/failures，打印计数、分类和最近条目；不启动模型、不应用提案。

## 安全边界

Phase 0 明确禁止：

- 自动应用 patch
- 自动修改测试/评测器
- 自动修改 guardrail
- 自动修改模型文件
- 主脑直接写 agent 源码
- 提案自我批准
- 访问网络/密钥

主脑只能在 `artifacts/` 写文件，提案也只是队列数据。

## 验证

```text
python3 tests/run_all.py
Ran 25 tests ... OK
```

新增 `tests/test_evolution.py`：

- 提案工具入队但不执行
- 非法 target_kind 被拒绝
- 所有 ERROR 写入 failures.jsonl
- 只读报告统计
- Reflection.change_request 往返

只读报告：

```text
proposals: 1
failures: 0
by kind: {}
by risk: {}
by code: {}
```

## 首条真实提案（2026-10-31）

- 运行 `t-20261031-003950-40e07c`：主脑调用 `harness.propose_change`，
  生成 `p-1793403614-59a8919e`。
- `REVIEW PASS 1.00`，46.23 s，`retries=0 failures=0`。
- 提案针对 planner 猜测 `knowledge.read_section` doc_id；
  人工评价 6/10：方向正确，但规则与现有 `_PLANNER_PROMPT` 重复、
  target 不精确、expected_effect 不可度量、测试不足；**不进入 patch**。
- 归档：`test_history/evolution/2026-10-31-first-proposal/`。

## 后续

Phase 1 提案协议已由 ADR-040 升级（ChangeProposal v2）。
Phase 1 才考虑：

- 隔离 worktree 应用 patch
- baseline vs candidate A/B
- 次脑评审 diff 和指标
- 人工批准
- 回滚
