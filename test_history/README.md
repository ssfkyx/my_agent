# test_history

这里保存**人工维护**的评测任务、参考结果和每次运行记录。

- 该目录已加入 `[guardrail].deny_names`，agent 工具不能读取或写入，避免期望值泄漏。
- 每次测试一个子目录：`YYYY-MM-DD-<task-name>/`
- 推荐结构：

```text
<date>-<task>/
├── task.md                     # 给 agent 的任务说明（副本）
├── setup.md                    # 数据生成、测试与评分方法
├── result.md                   # 截至目前的运行结论
├── reference_*.json            # 参考解结果（隐藏答案）
└── agent_runs/
    ├── pre_gate/               # 改动前记录
    └── post_gate/              # 改动后记录
```

- `result.md` 是更新入口：后续在同一任务上重跑，追加一节，不改写历史结论。
- agent 的活动副本仍放在 `artifacts/`，不要把 `test_history/` 作为工作目录。

## 索引

| 日期 | 任务 | 结果 |
| --- | --- | --- |
| 2026-10-30 | Terminal-Bench `jsonl-aggregator` | 门禁前 0/3；门禁后 1/5 exact pass（Q4 方差大） |
