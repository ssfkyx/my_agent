# Setup — Terminal-Bench jsonl-aggregator

## 数据

来源：`harbor-framework/terminal-bench-1` 的
`original-tasks/jsonl-aggregator`。

- 官方生成器：`task-deps/generate_records.py`
- 生成 5 个 `records_*.jsonl`
- 每个文件 200,000 条，总计 **1,000,000 条**
- 约 78 MB

工作副本：

```text
artifacts/terminal_bench_jsonl_aggregator/
├── generate_records.py
├── records_1.jsonl ... records_5.jsonl
├── sample_records.jsonl     # 前 20 行，仅用于看 schema
├── task.md
├── test_aggregate.py        # 只查结构，不泄漏期望值
└── aggregate.py             # agent 要写
```

## 生成命令

```bash
cd artifacts/terminal_bench_jsonl_aggregator
python3 generate_records.py
head -20 records_1.jsonl > sample_records.jsonl
```

## Agent 任务

读取 `task.md`，实现 `aggregate.py`：

```bash
python3 -m unittest discover \
  -s artifacts/terminal_bench_jsonl_aggregator -p 'test_*.py'
```

烟测只检查：

- 能运行 `aggregate.py`
- 输出含 `top_5_users_by_amount` / `top_5_tags_by_count`
- 两组各 5 项
- 金额是数字、items/count 是整数

精确值由人工用 `reference_aggregates.json` 对比。

## 参考解校验

```bash
python3 /tmp/reference_aggregate.py > /tmp/reference_aggregates.json
```

2026-10-30 校验结果：与官方 `test_outputs.py` 的 expected **完全一致**。
