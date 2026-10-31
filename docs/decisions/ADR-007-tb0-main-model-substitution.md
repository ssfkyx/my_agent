# ADR-007：TB-0 主脑临时替换为 Llama-3.1-8B（gemma4 GGUF 在当前 build 下不可用）

- **状态**：已采纳（2026-10-29）→ **已被 ADR-008 修正**：替换的真实原因是**模型文件损坏**（sha256 不符），
  而非 build 版本不匹配；文件重新下载并校验通过后，`main_alias` 将改回 `"main"`，本 ADR 降级为历史记录。
- **关联代码**：`agent/config/defaults.toml`（`harness.main_alias`、`[models.llama31]`）、`agent/config.py:load`、`agent/app.py:App.__init__`
- **关联 ADR**：ADR-002（单常驻模型）、ADR-004（BrainPort 可替换性正是本措施成立的前提）、ADR-006（TB-0 范围）、**ADR-008（根因与完整性策略）**
- **上游文档**：`docs/pre_architecture.md` §11.1 模型清单

> 后来验证：三个不同 build（b9553 / b11574 / b12002）都输出 `<unused32>`，且本地文件 sha256 与 HF 不一致
> （大小相同、内容不同）→ 判定为下载期静默损坏。本 ADR 的"版本不匹配"假设**已被证伪**，保留以记录判断过程。

## 背景（实测证据链）

目标是 `gemma4-v2-Q6_K.gguf`（`general.name = "Gemma4 v2 Merged Fp16"`，12B，文件日期 2026-10-28）作为主脑。首次实跑 `--once "ping"` 失败：

| 观测 | 结果 |
| --- | --- |
| 模型加载 | 正常，37 s 内 `/health=ok`，无 OOM |
| `/v1/chat/completions` + json_schema | `content` 为空，`finish_reason=length`（512 tokens 全被消耗） |
| 关闭 thinking（`chat_template_kwargs.enable_thinking=false`） | `content` 变成 4593 字符的 `<unused12><unused15>…<tool_call|>` |
| 纯 `/completion`（`"The capital of France is"`） | 恒定输出 `<unused32>` 重复 —— 连原始补全都不成立 |
| 同一 prompt 走 CPU（`-ngl 0`） | 同样 `<unused32>` 重复 → 与显存/量化无关 |
| `LFM2.5-8B`、`Meta-Llama-3.1-8B` 同参数对照 | `" Paris. city of..."`、`" a city of romance..."` → 运行时光栈正常 |
| GGUF 词表自检 | `tokens=262144`，`token_embd.weight=(3840,262144)`，维度自洽 |
| 架构自检 | 48 层 / 3840 隐层 / 16 头 / `final_logit_softcapping=30` / SWA 模式齐备 |
| 版本对照 | llama.cpp HEAD `8bb3cf0d2`（2026-10-12）；模型文件 2026-10-28，**比运行时间新 16 天** |

结论：不是配置错误、不是显存、不是量化精度问题，而是**该 gemma4 v2 GGUF 与当前 llama.cpp build 不匹配**（加载期已警告 `'</s>' / '<|tool_response>' 的 token type 异常`）。

## 决策

1. TB-0 的 `harness.main_alias` 暂时指向 `llama31`（`Meta-Llama-3.1-8B-Instruct-Q8_0`，端口 8085）。
2. `[models.main]`（gemma4）**保留在配置中不删除**，作为修复后的切换目标，切换只需改一行 `main_alias`。
3. 该替换是**临时措施**，必须由以下任一路径收敛：
   - 升级 llama.cpp 到包含 gemma4 v2 修复的提交后重建（首选）；
   - 或换成与当前 build 匹配的 gemma4 GGUF 转换产物。
4. 在收敛前，不得据 gemma4 的输出做任何质量评估或记忆沉淀。

## 理由

- TB-0 的验收对象是**管线**（信封 → 计划 → 工具 → 回执 → 答案 → ledger），不是模型选型；用可用的 8B 先把管线钉死，才能在模型修好后立刻验证替换。
- `BrainPort` 抽象使替换成本为「改一个 alias」，这正是 ADR-004 想要的收益；如果这里需要改代码，说明抽象失败。
- 不删除 gemma4 配置，是为了让修复后能一行切回，避免"临时方案变成永久方案"。

## 代价

- TB-0 的答案质量由 8B 决定，不代表最终主脑水平（8B 的计划能力弱于 12B）。
- 配置里多了一个 port（8085），多一份模型目录项。
- 必须在 gemma4 修好后回头重跑一次 TB-0 验收（记入 §12 待办）。

## 验证方式

- `harness.main_alias = "llama31"` 时 `python -m agent --once "ping" --json` 返回 `kind=ANSWER`，ledger ≥5 事件。
- 修复 gemma4 后，把 `main_alias` 改回 `"main"`，同一命令应再次通过（不改任何代码）。
