# ADR-008：模型文件完整性校验（sha256 门禁 + 禁止续传坏文件）

- **状态**：已采纳（2026-10-29）／**已验证闭环**（重新下载 → sha256 一致 → 切回 `main` → 三个组件冒烟通过）
- **关联代码**：`agent/config/defaults.toml`（`harness.main_alias`、模型路径）、`agent/adapters/llama_cpp.py:ServerSupervisor._start`（加载前的唯一入口）
- **关联 ADR**：**ADR-007（被本 ADR 修正的临时替换）**、ADR-002（单常驻模型）、ADR-004（BrainPort）
- **上游文档**：`docs/pre_architecture.md` §0（环境事实）、§11.1（模型清单）

## 背景（决定性证据）

`gemma4-v2-Q6_K.gguf` 在所有可用运行时下都输出退化 token（`<unused32>` 重复），于是逐层排除：

| 排除项 | 做法 | 结论 |
| --- | --- | --- |
| 显存/量化 | 同 prompt 走 `-ngl 0`（纯 CPU） | 同样退化 → 与 GPU/量化无关 |
| 采样参数 | `temp=1.0, repeat_penalty=1.1, top_k=64, top_p=0.95`（作者推荐） | 仍然退化 → 与采样无关 |
| 模板/工具格式 | 绕开模板，直接 `/completion` 原始补全 | 仍为 `<unused32>` → 与 chat template 无关 |
| 运行时版本 | b9553（作者"验证可用"）/ b11574 / b12002 三版 | 三版一致退化 → 与 build 无关 |
| 权重是否为零 | 按 GGUF 张量偏移抽样 `token_embd` / `blk.24.*` | 非零 → 不是空文件 |
| 前向是否退化 | `llama-perplexity` 单 chunk | **PPL = 262144.0000 = 词表大小** → logits 近似全等（均匀分布） |
| 文件是否完整 | 本地 `sha256` vs HF LFS `sha256` | 本地 `dbb7f496…c80f7` ≠ 官方 `edb98c93…1fe61`，**大小完全相同** |

结论：**下载期静默损坏**（大小对、内容错），加载器读到错误权重 → 输出均匀分布 → 采样出 `<unusedN>`。
这也解释了为什么"换 build""调采样"全部无效。

## 决策

1. 任何本地模型文件在投入使用前，必须与其来源仓库的 LFS `sha256` 一致；**不一致即视为不可用**，不得用"先跑跑看"绕过。
2. 重新下载时**禁止对可疑文件使用续传**（`curl -C -`）：损坏可能是断点续传拼接造成的，续传会把坏字节固化。
   下载到 `*.part`，校验通过后再原子改名。
3. 校验通过后，`harness.main_alias` 从 `"llama31"` 改回 `"main"`，并把 `llama31` 保留为应急回退（不删除）。
4. 已损坏文件保留为 `gemma4-v2-Q6_K.gguf.corrupt` 直至新文件校验通过，再删除。
5. 已知哈希记录在此，供后续复核：

   | 文件 | 大小 (B) | sha256 |
   | --- | --- | --- |
   | `gemma4-v2-Q6_K.gguf` | 9 786 020 384 | `edb98c934c50c8a5a32074b27f9ccf6e22552619e70245c0f83ccfbea9b1fe61` |
   | `Qwen3-Embedding-0.6B-Q8_0.gguf` | 639 150 592 | 见 HF `Qwen/Qwen3-Embedding-0.6B-GGUF` |
   | `mmproj-Qwen3VL-8B-Instruct-Q8_0.gguf` | 752 289 728 | 见 HF `Qwen/Qwen3-VL-8B-Instruct-GGUF` |

6. llama.cpp 保持 master（`418f905a4`，`0.8.0-dev b12002`）；b9553 的临时降级已回滚。

## 理由

- 这类损坏的症状（均匀 logits）与"模型能力差"极其相似，若不先做哈希对照，会浪费大量时间在采样/模板/版本的排列组合上——本次就先后误判了两次（先怀疑 thinking 吃 token，再怀疑 build 版本）。
- 大小校验（`content-length`）挡不住这种损坏，只有内容哈希能挡。
- 禁止续传是廉价的防御：重下 9.8 GB 的代价远小于"带着坏字节调三天参"。

## 代价

- 每次换模型多一步哈希核对（9.8 GB 约 13 s）。
- 重新下载 9.8 GB（约 80 分钟，本次网络 ~1.9 MB/s）。
- 不引入自动校验代码：`config.load` 若每次都算哈希会给启动加 13 s，故本阶段只作为**人工门禁 + ADR 记录**；
  若后续模型数量增多，再单开 ADR 加 `--verify-models` 子命令。

## 验证方式与结果（2026-10-29 23:05 实际执行）

| # | 检查 | 结果 |
| --- | --- | --- |
| 1 | `sha256sum gemma4-v2-Q6_K.gguf` | `edb98c93…1fe61`，**与上表一致** |
| 2 | `/completion` `"The capital of France is"` | `" Paris. The capital of word English is ..."` → 正常续写 |
| 3 | `llama-perplexity`（同 512-token chunk） | **PPL = 61.9955 ± 18.38**（损坏时为 262144.0000） |
| 4 | `python -m agent --once "ping" --json`（`main_alias="main"`） | `kind=ANSWER`，5 事件，`"pong! tool pipeline is alive and responding."`，28.9 s |
| 5 | 重建 build 下 embedding 冒烟 | `dim=1024`，`norm=1.000000` |
| 6 | 重建 build 下 VL 冒烟 | 红/蓝图 → `Red and blue.` |
| 7 | 显存回收 | `nvidia-smi` 1156 MiB，无残留 `llama-server` |

损坏文件副本已在第 1 项通过后删除。`llama31` profile 保留作应急回退。

## 后续（本 ADR 不覆盖）

- 若模型数量继续增加，再评估把 sha256 校验做成 `--verify-models` 子命令（当前只需人工核一次）。
- 下载仍需人工盯：本次 HTTP/2 流被 CANCEL（curl err 92）中断过一次，改用 `--http1.1` + 续传循环才完成；
  ADR 结论是"下载完成后必须校验"，而不是"下载必定成功"。
