# RAG Golden Set

人工维护的检索基线，`test_history` 已被 guardrail 拒绝，agent 不能读取这里的期望值。

检索模型：父块是 Markdown section；流程/步骤类查询会对 top 文档做 sibling roll-up；
`WORKLOG.md` 通过 `[knowledge].exclude` 排除。

## 文件

- `queries.jsonl`：查询、期望文档、期望标题、是否可回答
- `eval_rag.py`：检索层评测脚本

## 运行

先重建索引：

```bash
PYTHONPATH=. python3 -c "..."   # 或使用 knowledge.reindex
```

再运行：

```bash
PYTHONPATH=. python3 test_history/rag_golden/eval_rag.py
PYTHONPATH=. python3 test_history/rag_golden/eval_rag.py --k 3
```

## 指标

- `Recall@1/3/5`：期望文档是否进入前 k
- `MRR`：第一个正确文档的排名
- `Outline coverage`：期望标题是否出现在命中文档的 outline 中
- 无答案问题：人工检查最终回答是否明确拒答，而不是编造

## 维护

- 每次修改 chunking、父块扩展、hybrid、rerank 后重跑
- 生产失败案例追加到 `queries.jsonl`，不要覆盖旧案例
- 期望文档用稳定的 `doc_id`（相对 docs 的路径），不要用 chunk id
