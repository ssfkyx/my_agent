# Phase 1 optimizer 协议验证 — 2026-10-31

## 协议升级

`ChangeProposal` 升级到 `schema_version=2`，新增：

```text
current_behavior
root_cause
alternatives[] (option/description/tradeoff)
expected_metric (metric/baseline/target/method)
replay_plan[]
rollback
```

`harness.propose_change` 仍然只入队，不应用 patch。

## 成功运行

```text
t-20261031-005848-ddf02c
proposal_id: p-1793404750-f378b9bb
target: agent/core/harness.py:_PLANNER_PROMPT
REVIEW PASS 1.00
47.46 s
retries=0 failures=0
```

流程：

```text
workspace.read_file agent/core/harness.py
→ harness.propose_change (v2)
→ TOOL_RESULT 包含完整 proposal
→ ANSWER 概述提案
→ REVIEW PASS
→ REFLECTION
```

## 提案评价：6/10

### 优点

1. **协议字段完整**
   - `current_behavior`
   - `root_cause`
   - 2 个 alternatives，均带 tradeoff
   - `expected_metric: 3/10 → 0/30`
   - 3 项 replay plan
   - risk/rollback

2. **先读取目标文件**
   - 计划第一步是 `workspace.read_file agent/core/harness.py`
   - 比第一条提案更有依据

3. **target 精确**
   ```text
   agent/core/harness.py:_PLANNER_PROMPT
   ```
   不再是模糊的 “planner system prompt”。

4. **工具结果包含完整 proposal**
   - answer 能概述提案
   - REVIEW 能确认回答有证据
   - 这次 `REVIEW PASS`

### 问题

1. **仍然没有真正引用现有规则**
   - `_PLANNER_PROMPT` 已经包含：
     - “Never simulate a tool result”
     - “do not plan knowledge.read_section with a guessed doc_id”
   - 提案却说 “the system prompt doesn't explicitly forbid it”，与事实不符。
   - 读取了文件，但没有引用具体行/原文。

2. **baseline 是推断**
   - `3/10 planning turns contain fabricated elements` 没有来源。
   - 应来自 ledger/failures 统计，而不是模型猜测。

3. **replay_plan 不是可执行命令**

   ```text
   Re-run the 3 failing cases
   Audit 30 recent turns
   Verify no new plan invents a doc_id
   ```

   没有指定：
   - 哪 3 个 case
   - 用什么脚本
   - 如何断言
   - 通过标准

4. **rollback 不精确**

   ```text
   Revert the change to the planner prompt in the harness config
   ```

   `_PLANNER_PROMPT` 不是 config，而是代码常量。  
   应写成：
   ```text
   git revert <commit> 或恢复 agent/core/harness.py 中 _PLANNER_PROMPT 的上一版本
   ```

5. **仍然是 prompt-only**

   planner 的 plan args 在计划时固定，  
   `read_section` 无法使用后续 search 结果。  
   这个结构性根因没有被选为方案。

## 失败样例与修复

早期运行：

```text
t-20261031-005519-c1fc73
target: src/agents/planner.py   # 不存在
```

问题：

- 工具当时仍入队
- TOOL_RESULT 只返回 proposal_id，answer 只能确认 queued
- REVIEW FAIL：缺少提案字段

修复：

1. `ProposeChangeTool` 校验 target 路径必须存在
2. TOOL_RESULT 返回完整 proposal
3. answer payload 保留 proposal，不做 preview 截断
4. 重跑后 `t-20261031-005848-ddf02c` PASS

## 结论

> 协议升级成功：主脑现在能提交结构化、可回放、可回滚的实验提案。  
> 但提案质量仍只有 6/10：证据引用、baseline 来源、replay 可执行性、根因深度都不足。

Phase 1 控制器不能只看 `REVIEW PASS`。下一步需要独立的 `ProposalReviewer`，并强制：

- `current_behavior` 引用文件/行/ledger 证据
- `baseline` 来自实际统计
- `replay_plan` 至少一条可执行命令
- `rollback` 精确到文件/命令
- 至少一个方案触及结构机制，而不只是 prompt
