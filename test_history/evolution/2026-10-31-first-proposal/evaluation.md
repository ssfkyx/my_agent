# 主脑提案评价 — 2026-10-31

## 运行

```text
t-20261031-003950-40e07c
tool: harness.propose_change
proposal_id: p-1793403614-59a8919e
```

结果：

```text
PLAN 2 steps → TOOL_CALL harness.propose_change → TOOL_RESULT ok
REVIEW PASS 1.00
retries=0 failures=0
46.23 s
models={'main': 99, 'sub': 99}
```

## 提案

```json
{
  "proposal_id": "p-1793403614-59a8919e",
  "target_kind": "prompt",
  "target": "planner system prompt",
  "rationale": "planner sometimes invents a doc_id before it has seen one from knowledge.search, leading to NOT_FOUND; doc_id must be copied verbatim from a prior search result",
  "proposal": "add a rule to the planner system prompt: 'never invent a doc_id; it must be copied verbatim from a prior knowledge.search result. If you need a doc_id and haven't seen one, you must plan a search first.'",
  "expected_effect": "zero guessed doc_ids and no NOT_FOUND errors from un-seen doc_ids",
  "tests": ["python3 tests/run_all.py"],
  "risk": "low",
  "status": "proposed",
  "created_at": 1793403614.5247843,
  "source_call": "c-0003"
}
```

## 评价

**结论：方向正确，但不足以直接进入 Phase 1 patch。评分 6/10。**

### 优点

1. 诊断准确：`knowledge.read_section` 的 `doc_id` 被 planner 猜测，是实际出现过的失败模式。
2. 结构化字段完整：
   - `target_kind=prompt`
   - `rationale` 明确说明失败链
   - `proposal` 是可读的具体规则
   - `risk=low`
3. 没有越权：只入队，没有修改源码。
4. 工具调用正确，返回值里包含 `proposal_id` 和路径。

### 问题

1. **规则已经存在**
   - `agent/core/harness.py` 的 `_PLANNER_PROMPT` 已经有：
     > do not plan `knowledge.read_section` with a guessed `doc_id`
   - 这条提案基本是重复已有规则，不能解释为什么现有 prompt 仍然失效。

2. **target 不精确**
   - `planner system prompt` 不是文件/符号。
   - Phase 1 需要一个可定位的 target，例如：
     ```text
     agent/core/harness.py:_PLANNER_PROMPT
     ```
     或
     ```text
     agent/tools/knowledge.py:ReadSectionTool.spec.args_schema
     ```

3. **expected_effect 不可度量**
   - “zero guessed doc_ids”没有定义数据集、运行次数、失败统计口径。
   - 应改成：
     ```text
     在 20 次回放中，knowledge.read_section 的 NOT_FOUND 从 N 降到 0
     ```
     或
     ```text
     planner 计划中出现 read_section 但 doc_id 不在先前搜索结果中的次数 = 0
     ```

4. **测试不足**
   - 只有 `python3 tests/run_all.py`。
   - 需要一个针对性测试：
     - 构造一个 plan，`read_section` 的 `doc_id` 不是来自先前搜索结果；
     - 期望 harness 拒绝/修复/replan；
     - 断言不会产生 `NOT_FOUND` 工具调用。

5. **方案可能无效**
   - planner 的 args 在计划时固定，无法引用后续 `knowledge.search` 的输出；
   - 因此“必须先搜索再复制 doc_id”在单轮 plan 里可能根本无法执行。
   - 更结构化的修法是：
     - 禁止 planner 在同一 plan 里计划 `read_section`；或
     - 引入“搜索后二次规划”，让 planner 看到搜索结果再决定是否 `read_section`；或
     - 让 `read_section` 支持从最近一次搜索结果中取 doc_id。

6. **评审没有评价提案本身**
   - 次脑 `REVIEW PASS 1.00` 只说明“回答说提案已入队”有工具证据；
   - 它没有评价提案的技术正确性。
   - Phase 1 需要独立的 `ProposalReviewer` schema，不能复用答案评审。

## 建议

不要直接应用原提案。建议改写成 Phase 1 候选：

```text
target_kind: tool_schema 或 tool_impl
target: agent/tools/knowledge.py:ReadSectionTool 或 harness plan validator
proposal:
  - 在 plan 执行前校验 read_section 的 doc_id；
  - 或把 read_section 从首轮 planner 的可用工具中移除；
  - 或增加“搜索后二次规划”路径。
expected_effect:
  - 20 次回放中 read_section NOT_FOUND = 0；
  - 现有 25 项测试全绿；
  - RAG golden Recall@k 不下降。
tests:
  - python3 tests/run_all.py
  - 新增 test_plan_rejects_unseen_read_section_doc_id
  - PYTHONPATH=. python3 test_history/rag_golden/eval_rag.py --k 5
risk: medium
```

一句话：

> 主脑能提出方向正确的提案，但当前提案是“重复 prompt 规则 + 不可测目标 + 无针对性测试”，只能作为 Phase 1 的讨论起点，不能直接进入 patch 阶段。
