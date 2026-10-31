# Evolution Test History

人工保存的 harness 演进提案与评价；`test_history` 已被 guardrail 拒绝，agent 不能读取。

## 记录

| 日期 | 记录 | 结论 |
| --- | --- | --- |
| 2026-10-31 | `2026-10-31-first-proposal/` | v1 提案；方向正确，但重复已有规则、目标不可度量、测试不足；6/10，不进入 patch |
| 2026-10-31 | `2026-10-31-phase1-protocol/` | v2 协议验证；结构完整、target 精确，但证据/基线/replay 仍弱；6/10 |

每条记录包含：

- `proposals.jsonl`：原始提案
- `ledger.jsonl`：产生提案的完整对话
- `evaluation.md`：人工评价与 Phase 1 改写建议
