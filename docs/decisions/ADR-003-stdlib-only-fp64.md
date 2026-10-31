# ADR-003：运行时零第三方依赖 + FP64 数值策略

- **状态**：已采纳（2026-10-29）
- **关联代码**：`agent/core/harness.py:Harness._answer`（`confidence` 为 `float`）、`agent/adapters/llama_cpp.py:LlamaCppBrain.complete`（`elapsed_s` 为 `time.perf_counter` 差值）、`agent/core/protocol.py:ToolResult.elapsed_s`
- **关联 ADR**：ADR-004（HTTP 用 `urllib`）
- **上游文档**：`docs/pre_architecture.md` ADR-3、ADR-7

## 背景

- 现场 Python 3.14.4 **没有** numpy / httpx / pydantic / rich（实测 `ModuleNotFoundError`）。
- 约束要求"优先使用语言/框架/标准库已有能力""非必要不使用集成"。
- 约束要求"使用浮点数时统一使用 FP64"。

## 决策

1. 运行时依赖为零：HTTP 用 `urllib.request`，配置用 `tomllib`，进程用 `subprocess`，锁用 `fcntl`，时间用 `time.perf_counter`，JSON 用 `json`。
2. 所有浮点一律 Python `float`（IEEE-754 binary64）：评分、置信度、耗时、相似度。禁止 `float32`、禁止 `struct` 的 `"f"` 格式。
3. TB-3 的向量层（尚未实现）已定调：向量用 `array("d")`，余弦用纯 Python 双精度累加——与 FP64 一致，且不引入 numpy。
4. 若未来确实需要 numpy，必须新增 ADR 并说明"为什么纯 float 不够"，不允许悄悄引入。

## 理由

- 无第三方包 = 换机器/换 WSL 不会因 wheel 缺失而失败；这条环境里根本没有 pip 包可用。
- 文档语料是 .md 流程文件，量级在千级 chunk，纯 Python 双精度点积足够快，numpy 的收益不足以抵消依赖成本。
- FP64 统一后，`0.6200000000000001` 这类比较问题只会在一个精度域里出现，调试面更小。

## 代价

- 大语料（>10 万 chunk）检索会慢于 numpy；届时按 ADR 流程再评估。
- 没有 pydantic，深层校验要手写（见 `Plan.from_payload`）。

## 验证方式

`python -c "import agent"` 在无任何第三方包的 3.14 环境通过；`python -X importtime -m agent --once ping` 的依赖列表只含 stdlib。
