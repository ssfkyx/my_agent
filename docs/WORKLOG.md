# WORKLOG — 当前作业的持久化记录

> 目的：窗口/会话崩溃后，仅凭本文件即可判断「在做什么、做到哪、下一步是什么」。
> 规则：每完成一个可验证步骤就回写；记录命令、结果、失败原因，不写流水账。
> 最后一次更新：2026-10-31 09:19 CET

## 1. 本轮目标

把主脑从 `gemma4-v2-Q6_K.gguf` 切到 `gemma4-v2-Q4_K_M.gguf`，并把
`ServerSupervisor` 的 ngl 降档重试闭环：

1. Q6 在 12 GB 卡上启动失败：模型加载后 KV cache 申请 1088 MiB `cudaMalloc` OOM。
2. Q4 文件已就位（6.9 GiB，48 层，12B）。
3. 接手时代码里已有 `gpu_layers_ladder()` 与重试循环（注释标 ADR-034），但配置仍指向 Q6，
   且 ADR-033/034 尚未落文档。

## 2. 任务清单

- [x] T1 建立持久化作业记录（本文件）
- [x] T2 `models.main` 切到 Q4_K_M，`n_gpu_layers = 99`
- [x] T3 `ServerSupervisor.stop()` 按进程组清理；启动成功记录实际 ngl
- [x] T4 静态校验：compileall + config.load + ladder 断言
- [x] T5 实跑 `--once "ping" --json`，核对 ledger / report / main.log / 显存
- [x] T6 补 ADR-033（Q4 替换）与 ADR-034（ngl 降档）
- [x] T7 更新 `docs/pre_architecture.md`
- [x] T8 回写本文件最终状态
- [x] T9 把实际 `gpu_layers` 写入 RunReport（`RuntimeInfoPort` + `models` 段）
- [x] T10 SIGTERM 转为 KeyboardInterrupt，确保 `app.close()` 清理模型进程
- [x] T11 Web/TUI 冒烟（Web 真跑一轮 + TUI `/quit`）
- [x] T12 RunReport `models` 改为严格按轮统计（`begin_turn` + `ensure` 记录）
- [x] T13 外部评测：Terminal-Bench `parse-log-stats`，Q4 实跑并评分
- [x] T14 TUI 真跑一轮读文件任务（非仅 `/quit`）
- [x] T15 文档同步（ADR-034 / pre_architecture）与最终回归
- [x] T16 token 预算统一：`n_ctx=8192`、`max_tokens=8192`、`review_max_tokens=8192`
- [x] T17 长指令实测：计划不再截断，峰值显存 9136 MiB
- [x] T18 回归：ping 24.05 s、PASS、峰值 9133 MiB、无残留
- [x] T19 新增 ADR-035，回写 ADR-012/019 与 pre_architecture v1.6
- [x] T20 重建知识库索引：37 文档 / 318 chunks / dim=1024，ADR-034/035 可检索
- [x] T21 新建 `tests/`：per-turn 模型统计 + 进程组终止
- [x] T22 添加 `tests/run_all.py`，4 项测试全绿
- [x] T23 文档同步与最终回归
- [x] T24 Web `/upload`：multipart、类型/大小校验、会话目录落盘
- [x] T25 前端 Attach 按钮 + 文件选择/拖拽 + 附件 chips
- [x] T26 附件路径 + perception 工具提示注入 planner 输入
- [x] T27 新增上传/ASR 测试，测试总数 11
- [x] T28 Web 实跑：图片与音频均触发对应 perception 工具
- [x] T29 放大 CONVERSATION 面板：桌面/移动端至少 52vh
- [x] T30 选更难任务：Terminal-Bench `jsonl-aggregator`（1M 条 / 5 文件 / 78MB）
- [x] T31 参考解校验：与官方 expected 完全一致
- [x] T32 Q4 实跑：0/3 通过；记录代码生成与自修复失败
- [x] T33 评审门禁：REVISE/FAIL 打回 planner，传回 issues/suggestions
- [x] T34 证据校验：测试通过声明必须有 returncode=0 的测试结果
- [x] T35 动态步数：每次打回追加 4 步，总上限 12 步
- [x] T36 新建 `test_history/` 并加入 guardrail deny_names
- [x] T37 清空 runs/memory/旧上下文，重跑 jsonl-aggregator
- [x] T38 记录 post-gate 1/5 exact pass；更新 ADR-037/WORKLOG/pre_architecture
- [x] T39 新增 `docs/software-development-process.md` 并重建 RAG 索引
- [x] T40 双语检索 + 计划输出限额 + 重规划失败受限回退；原问题重跑 PASS
- [x] T41 父块扩展：Chunk.parent_id + section 父块
- [x] T42 SQLite FTS5 trigram + BM25 检索
- [x] T43 over-fetch、RRF 融合、按 doc 去重、启发式 rerank
- [x] T44 SearchTool 返回 parent_text / outline / vector_score
- [x] T45 配置注入 candidate_k / parent_chars / hybrid / rrf_k
- [x] T46 test_history/rag_golden 基线：Recall@k / MRR / outline coverage
- [x] T47 answer payload 预算 + 评审 outline 规则 + 20 项测试全绿
- [x] T48 ChangeProposal schema + proposals.jsonl 队列
- [x] T49 `harness.propose_change` 工具，只入队不执行
- [x] T50 所有 ERROR 写入 failures.jsonl
- [x] T51 Reflection 增加 change_request
- [x] T52 只读 `--evolve` 观察报告
- [x] T53 演进 Phase 0 测试、文档、回归
- [x] T54 真实对话生成首条提案并完成人工评价（6/10，不进入 patch）
- [x] T55 ChangeProposal 升级 v2 optimizer 协议
- [x] T56 harness.propose_change schema + target 路径校验
- [x] T57 TOOL_RESULT 返回完整 proposal，answer 保留完整结构
- [x] T58 真实对话验证 v2 协议（先失败后 PASS）
- [x] T59 ADR-040、文档、测试、索引回归
- [x] T60 Agents_opinion 归档功能 + 3 条历史提案回填；自进化机制冻结
- [x] T61 启动参数 --model / --disable / --list-models
- [x] T62 main+embed 必选；sub/asr/vl 可选运行
- [x] T63 无次脑/无媒体时 Harness/工具自动降级
- [x] T64 可选模型测试、ADR-042、文档与烟雾验证
- [x] T65 双击启动器 start.py + start.sh/command/bat
- [x] T66 默认 Web、自动开浏览器、参数透传
- [x] T67 启动器测试、README、ADR-043、烟雾验证

## 3. 关键事实（已核对）

| 项 | 值 |
| --- | --- |
| 项目根 | `/home/cielo_1/projects/my_agent` |
| 主脑当前配置 | `gemma4-v2-Q4_K_M.gguf`，`n_ctx=8192`，`n_gpu_layers=99` |
| token 预算 | `max_tokens=8192`、`review_max_tokens=8192`（ADR-035） |
| 主脑旧配置 | `gemma4-v2-Q6_K.gguf`，`n_ctx=4096`，`n_gpu_layers=24` |
| Q6 失败日志 | `runs/server/main.log`：KV cache 1088 MiB `cudaMalloc` OOM |
| Q4 文件 | `/home/cielo_1/models/gemma4-v2-Q4_K_M.gguf`，6.9 GiB |
| Q4 元数据 | GGUF v3，`gemma4`，48 层，12B，16 heads，sliding window 1024 |
| Q4 sha256 | `0b9506cab36f7f818e34f9c0f5a3d6568d0b37100f3a3e1092e2eec3c4c96791` |
| GPU | RTX 4080 Laptop，12282 MiB；2026-10-30 20:18 服务退出后空闲约 10.9 GiB |
| 现有 ADR | ADR-001 ~ ADR-043；ADR-033～043 已补齐 |
| 知识库 | `runs/knowledge.db`：45 文档 / 392 chunks / dim=1024；FTS5 trigram（2026-10-31 09:19 重建） |
| 测试 | `python3 tests/run_all.py`：38 项 unittest 全绿 |
| RAG 基线 | `test_history/rag_golden/`：Recall@1 .909 / @3 1.0 / @5 1.0；MRR .955 |
| 演进协议 | `ChangeProposal v2`；3 条提案、1 条错误记录；最新 `p-1793404750-f378b9bb` |
| Agents_opinion | 3 条主脑意见已归档；不进入 RAG，guardrail 禁止 agent 读取 |
| 模型启动 | 必选 main/embed；可选 sub/asr/vl；`--model` / `--disable` / `--list-models` |
| 双击启动 | `start.bat` / `start.sh` / `start.command` → `start.py` → 默认 Web |
| 测试历史 | `test_history/2026-10-30-jsonl-aggregator/`；agent 不可读（deny_names） |

## 4. model_test 可复用经验

- ngl 阶梯实测：`20` OOM，`16` 可用（7397 MiB / 20 tok/s）；失败常是连续缓冲分配，
  不是绝对显存不足。
- `--parallel 1` 防止默认多 slot 摊薄 ctx。
- 进程提前退出要立即抛错并带日志尾部，否则降档永远触发不了。
- `start_new_session=True` + `killpg` 清理进程组，避免孤儿占显存。
- 显式激活并回传实际 ngl / 尝试序列，不要静默降级。

## 5. 执行记录

### 2026-10-31 09:19 CET

- T65–T67 完成：双击启动器。
  - 新增 `start.py`：默认 `python -m agent --web`，1.5 s 后打开浏览器；
    `--no-browser` 可关闭；其他参数原样透传。
  - 平台入口：`start.bat`（Windows）、`start.sh`（Linux）、`start.command`（macOS）。
  - 不加载模型；`--config` 只用于读取 Web host/port。
  - 烟雾：`python3 start.py --no-browser --web`，
    `curl /health` 返回 `{"ok":true,"ui":"web","auth":false}`。
  - 测试总数 38，全绿。
  - 文档：`README.md`、`docs/decisions/ADR-043-double-click-launcher.md`。

### 2026-10-31 09:14 CET

- T61–T64 完成：启动时可选模型。
  - `--model ALIAS=PATH` 覆盖 GGUF 路径；`--model ALIAS=none` / `--disable` 禁用可选模型。
  - `--list-models [--json]` 显示 required/optional、角色、路径是否存在。
  - 必选 main/embed；可选 sub/asr/vl/llama31。
  - 无 sub：Reviewer/Reflector=None，delegate/switch_role 不注册，`review=n/a`。
  - 无媒体：不构造 LlamaCppMedia，不注册 perception；单模态只注册对应工具。
  - 主脑-only 烟雾测试 `t-20261031-091304-97636a`：
    `pong`，17.62 s，`review=n/a`，`retries=0 failures=0`。
  - 测试总数 36，全绿。
  - 文档：`docs/decisions/ADR-042-optional-models.md`。

### 2026-10-31 09:04 CET

- T60 完成：`Agents_opinion` 主脑意见档案。
  - 新增 `agent/adapters/opinions.py:OpinionLog`，把 proposal 渲染为
    `Agents_opinion/<timestamp>-<proposal_id>.md` 并维护 `INDEX.md`。
  - `harness.propose_change` 入队后同时写归档；TOOL_RESULT 返回
    `opinion_path`。
  - 兼容 v1/v2；现有 3 条提案已回填。
  - `Agents_opinion/*` 加入知识库 exclude；目录加入 guardrail deny_names。
  - 自进化机制冻结：不实现 patch 应用、ProposalReviewer、A/B controller、
    自动合并。
  - 测试总数 30，全绿。
- 归档目录：
  `Agents_opinion/README.md`、`INDEX.md`、3 条 proposal Markdown。

### 2026-10-31 01:01 CET

- T55–T59 完成：`harness.propose_change` 升级为 Phase 1 optimizer 协议。
  - `ChangeProposal` v2 新增 current_behavior/root_cause/alternatives/
    expected_metric/replay_plan/rollback；`alternatives` 至少 2 个。
  - `ProposeChangeTool` 校验 target 文件路径必须存在；
    TOOL_RESULT 返回完整 proposal。
  - answer payload 对 proposal 不做 preview 截断，保留完整字段。
  - 验证：
    - `t-20261031-005519-c1fc73`：target 不存在且 answer 未概述，REVIEW FAIL；
    - `t-20261031-005848-ddf02c`：先读文件，v2 提案完整，
      `PASS 1.00`，47.46 s，`retries=0 failures=0`。
  - 提案质量仍为 6/10：没有引用具体规则、baseline 来源不足、
    replay_plan 不是可执行命令。
  - 归档：`test_history/evolution/2026-10-31-phase1-protocol/`。
- 测试总数 27，全绿。

### 2026-10-31 00:41 CET

- T54 完成：真实对话生成首条 harness 演进提案。
  - 运行 `t-20261031-003950-40e07c`：主脑调用
    `harness.propose_change`，写入提案 `p-1793403614-59a8919e`。
  - `REVIEW PASS 1.00`，46.23 s，`retries=0 failures=0`；
    `models={'main':99,'sub':99}`。
  - `python3 -m agent --evolve` 显示 `proposals: 1`、`failures: 0`。
  - 人工评价 6/10：方向正确，但规则与现有 `_PLANNER_PROMPT` 重复，
    target 不精确、expected_effect 不可度量、测试不足；
    不进入 Phase 1 patch。
  - 归档：`test_history/evolution/2026-10-31-first-proposal/`
    （proposals/ledger/evaluation）。

### 2026-10-31 00:13 CET

- T48–T53 完成：Harness 演进 Phase 0（只观察、只提案）。
  - 新增 `agent/core/evolution.py`：`ChangeProposal` + `EvolutionStore`；
    提案写入 `runs/evolution/proposals.jsonl`，没有 apply 方法。
  - 新增 `harness.propose_change`：主脑只能入队，不修改代码、配置或工具注册表。
  - 所有 `ERROR` 事件额外写 `runs/evolution/failures.jsonl`；
    初次规划失败、取消、评审不可用等错误不再丢失证据。
  - `Reflection` 增加 `change_request`，反思可给一句具体改进建议。
  - 新增只读 `python3 -m agent --evolve [--json]`，不启动模型。
  - `tests/test_evolution.py` 新增 5 项；总测试 25，全绿。
- 只读报告当前为空：
  `proposals: 0`、`failures: 0`。
- 尚未实现 Phase 1：隔离 worktree、patch 应用、baseline/candidate A/B、
  次脑 diff 评审、人工批准。

### 2026-10-30 23:50 CET

- 修正父块粒度：从文档级改为 **section 级**（`doc_id#section-NNN`）。
  - 命中 section 返回该 section 文本；不再默认返回整篇文档。
  - `document_outline` 只作为评审覆盖度元数据，不替代 section 父块。
  - 流程/步骤类问题增加 sibling roll-up：把 top 文档的其余 section 一并加入结果，
    每项仍是 section 父块；最多 12 个 sibling。
- `WORKLOG.md` 移出知识库：`[knowledge].exclude = ["WORKLOG.md"]`，
  `KnowledgeBase.index` 用 fnmatch 跳过。
- answer payload 去重键改为 `(doc_id, heading)`，保留同文档不同 section；
  总字符预算仍为 8000。
- 重建索引：`documents=45 chunks=392 dim=1024`；`WORKLOG indexed: False`。
- 基线：`Recall@1=0.909`、`Recall@3=1.0`、`Recall@5=1.0`、`MRR=0.955`、
  `outline coverage=1.0`。
- 对话验证 `t-20261030-233952-a66888`：中英双查询，列 8 步 + DoD，
  `PASS 1.00`，45.02 s，`retries=0 failures=0`。
- 测试总数 20，全绿。

### 2026-10-30 23:27 CET

- T41–T47 完成：RAG 父块扩展、FTS5 hybrid、排序与基线。
  - `Chunk` 增加 `parent_id`；当前父块为文档级；`search` 返回 `parent_text` 和 `outline`。
  - SQLite 新增 `chunks_fts`（trigram），`replace_all` 事务内同步；旧库自动补列并重建 FTS。
  - `candidate_k=20` over-fetch；向量 + BM25 用 RRF 融合；按 doc 去重；轻量启发式 rerank。
  - answer payload 按 doc 去重，父块总字符上限 8000；`_fallback_answer` 不再打印原始大结果。
  - 新增 `tests/test_rag.py`（4 项）；总测试 18，全绿。
- 索引与基线：
  - `documents=41 chunks=377 dim=1024`，FTS5 trigram；
  - `Recall@1=0.727`、`Recall@3=1.0`、`Recall@5=1.0`、`MRR=0.864`、
    `outline coverage=1.0`。
- 对话验证：
  - `t-20261030-232653-af1fa3`：中文 + 英文各一次 `knowledge.search`，
    回答列 8 步 + DoD，`PASS 1.00`，42.62 s，`retries=0 failures=0`。
  - `t-20261030-232349-6c25c1`：也列出 8 步并 PASS，但 `retries=1 failures=1`；
    失败步是 planner 猜了不存在的 `read_section` doc_id。
  - `t-20261030-232536-f8e6f8`：两个查询的父块太长导致 answer 400；
    加入 payload 去重/8000 字符预算后修复。
- 残余：`WORKLOG.md` 是主要召回噪声，Recall@1 的 3 个 miss 中 2 个由它造成；
  后续可评估排除 WORKLOG 或加文档类型权重。

### 2026-10-30 23:12 CET

- T40 完成：修复“软件开发流程”对话失败暴露的三个问题。
  - planner prompt：先判断用户语言，回答使用该语言；`knowledge.search` 主查询用
    用户语言，再追加一次中文/英文同概念查询。
  - 计划输出限额：`_PLAN_MAX_TOKENS=2048`；`_PLAN_SCHEMA` 限制
    `steps.maxItems=12`、`goal/rationale/why` 最大长度，避免重规划挤爆 `n_ctx`。
  - 重规划失败不再 `_fail`：保留草稿和已有证据，追加“未完成说明/评审意见/
    失败原因/证据数量”，以较低 confidence 交付。
  - `tests/test_review_gate.py` 增至 3 项；总测试 14 项，全绿。
- 失败记录复盘 `t-20261030-230043-b103eb`：
  - 原计划英文查询 `software development lifecycle project planning` 只命中
    文档开头和 ADR-005；草稿未列具体步骤；
  - 评审 `REVISE 0.0` 后，重规划输出被 8192 上下文截断，以 `PLAN_INVALID` 结束。
- 重跑 `t-20261030-231058-ba2b88`：
  - 计划：`knowledge.search("软件开发", k=3)` +
    `knowledge.search("software development lifecycle", k=3)` + answer；
  - `REVIEW PASS 1.00`，43.71 s，`retries=0`，无失败；
  - 回答为中文，列出 3 个步骤；两个查询各 `k=3`，仍未覆盖完整 8 步，
    说明 RAG 覆盖问题仍在（后续可做父文档扩展或提高 k）。
  - 重跑前清理了外部遗留的 8090 `llama-server`，否则会触发
    `ModelBusyError`。

### 2026-10-30 22:59 CET

- T39 完成：新增软件开发流程文档并重建 RAG 索引。
  - 文件：`docs/software-development-process.md`，8 个关键步骤 + DoD。
  - 全量重建：`documents=40 chunks=359 dim=1024`；
    新文档 `software-development-process.md` 共 10 个 chunk。
  - 检索验证：
    - `软件开发流程 关键步骤` → 0.7981，top-1 命中该文档；
    - `最小完成标准 DoD 测试通过 评审` → 0.8929，top-1 命中 DoD 小节。
  - 索引后 embed 进程已退出，无残留 `llama-server`。

### 2026-10-30 21:19 CET

- T33–T38 完成：评审门禁、证据校验、动态步数、test_history。
  - `Harness.run_turn` 现在在 `REVISE/FAIL` 且有预算时重新 `_plan`；
    反馈包含 review issues/suggestions + 最近工具失败 + 非零 returncode。
  - 默认 `max_review_repairs=2`、`repair_step_bonus=4`；
    基础 4 步，每次打回 +4，总上限 12。
  - 证据校验：answer 声称 tests passed/verified 但无 `returncode=0` 的测试
    `workspace.run_command` 时，合成 REVISE issue（`gate=evidence`）。
  - 新增 `tests/test_review_gate.py`：验证 2 次 planner 调用、1 次 RETRY、
    动态步数、evidence gate、反馈注入；测试总数 12，全绿。
  - `test_history/` 保存任务、期望值、pre/post-gate ledger 与报告；
    `test_history` 已加入 `[guardrail].deny_names`，agent 读取会被拒绝。
  - 每次重跑前清空 `runs/*.jsonl`、`runs/*.report.*`、`runs/memory.jsonl`、
    `runs/server`，保留 `runs/knowledge.db`。
- T38 实跑 `jsonl-aggregator`（post-gate）：
  - `t-20261030-204736-5b7959`：默认 2 次打回；3 PLAN / 2 RETRY / 12 步；
    路径写错到 `artifacts/terminal_bench/...`，FAIL。
  - `t-20261030-205441-35cb13`：默认；schema/逻辑仍错，FAIL。
  - `t-20261030-210137-5cbc6c`：默认；数值基本正确但 key 为
    `top_users/top_tags`，REVISE 0.5，FAIL。
  - `t-20261030-210820-0f52fd`：`max_review_repairs=3` + 显式 schema 提示；
    **PASS 1.00，retries=0，120.04 s**；`aggregates.json` 与官方 expected 完全一致。
  - `t-20261030-211034-820a7d`：默认 2 次打回，同提示；429.74 s，
    仍 FAIL（`len(items)` 误用，未写 `aggregates.json`）。
  - 结论：门禁/动态步数/原因回传均已实际触发；Q4 代码生成方差仍大，
    post-gate exact pass 为 1/5。下一步优先增加“写后验证新鲜度”硬检查。
- 文档：新增 ADR-037，ADR-013 advisory 条目标注被取代；
  `pre_architecture.md` 升到 v1.8 并新增第 26 条。

### 2026-10-30 20:35 CET

- T30：选择 Terminal-Bench `original-tasks/jsonl-aggregator` 作为更重任务。
  - 官方生成器生成 5 个 `records_*.jsonl`，共 **1,000,000 条**、约 78MB；
  - 工作目录 `artifacts/terminal_bench_jsonl_aggregator/`；
  - 增加了任务说明、20 行 schema 样本、结构烟测；完整期望值未放入工作区。
- T31：参考解校验。
  - 用官方 solution 的聚合逻辑跑全量数据；
  - 输出与官方 `test_outputs.py` 的 expected **完全一致**：
    `user211 2131992.47/149035` … `lime 28874`。
- T32：Q4 实跑结果 **FAIL（0/3）**。
  - `t-20261030-203118-18224f`（默认 4 步）：
    写成 `json.loads(line)['count']`，使用不存在的字段，烟测 `CalledProcessError`；
    用尽 4 步后未修复。
  - `t-20261030-203228-0da24f`（临时 `max_steps=7`）：
    写成 `top_users`/`top_tags`/`total_records`，且 `json.dump` 括号不匹配；
    烟测失败；planner 仍只安排 4 步，没有 verify/fix 循环。
  - `t-20261030-203343-620e83`（继续修复轮）：
    仍留下 f-string 引号 `SyntaxError`，未生成 `aggregates.json`；
    最终 answer 声称 “All tests passed”，但 ledger 中没有运行烟测的 TOOL_CALL，
    属于无工具证据的虚假成功。
- 结论：
  1. Q4 对 1M 条多文件聚合的 schema 遵循和代码生成明显不足；
  2. planner 在任务要求“失败后修复”时仍不把验证步骤排进计划；
  3. 工具失败后没有自动 replan/repair，最终 answer 可能与工具结果脱节。
- 回归：仓库 11 项 unittest 仍全绿；无残留 `llama-server`。

### 2026-10-30 20:26 CET

- T29 完成：放大 Web 的 CONVERSATION / ANSWER 面板。
  - 桌面 grid 改为：
    `minmax(110px, 26vh) minmax(52vh, 1fr) auto auto`
  - `#conversation` 固定 `min-height: 52vh`，并占用 `1fr` 剩余高度；
    在 1080p 下实际约 61% 视口高度，超过一半。
  - `#status`、`#composer` 改为显式第 3/4 行，避免自动放置错位。
  - 移动端（≤760px）对话区也改为 `52vh`，其余面板按 auto 高度堆叠。
  - CSS 花括号 64/64 平衡；页面脚本 `node --check` 通过。

### 2026-10-30 20:18 CET

- T24–T28 完成：Web 多模态附件上传（ADR-036）。
  - 后端 `POST /upload`：stdlib `email.parser` 解析 multipart；保存到
    `artifacts/uploads/<session>/<uuid>_<safe_name>`；只收图片/音频，
    按 `media.max_image_bytes` / `max_audio_bytes` 限流；文件名 basename + 白名单化。
  - `/message` 新增 `attachments: string[]`；后端 `resolve()` 校验路径必须在本会话
    upload 目录内；拼入 `[attachments]` 与 `perception.attach_file` /
    `describe_image` / `transcribe` 提示。
  - 前端：Attach 按钮 + hidden file input + 拖拽区 + 附件 chips；上传中禁用发送。
  - ASR：修复 Qwen3-ASR 的 `language X<asr_text>` 前缀，只保留正文；静音返回空串。
- 测试：`python3 tests/run_all.py` → **11 tests，OK，exit 0**。
  - 新增 `tests/test_web_upload.py`（上传/注入/后缀/超限/跨会话路径）
  - 新增 `tests/test_media_asr.py`（marker 剥离/空转写/无 marker）
- T28 实跑（Web）：
  - 图片 `t-20261030-201402-25a3e3`：`attach_file → describe_image`；
    `models={'main':99,'vl':None,'sub':99}`；35.66 s；`PASS 1.00`；
    答案“The image is a solid blue color…”。
  - 音频 `t-20261030-201705-062f82`：`attach_file → transcribe`；
    `models={'main':99,'asr':None,'sub':99}`；33.95 s；`PASS 1.00`；
    静音返回 `data.text=""`，答案说明无语音。
  - 页面已包含 `#attach`、`#file`、`#attachments` 与
    `accept="image/*,audio/*,.wav,.mp3,.flac,.ogg,.m4a"`。
- 备注：第一次图片测试遇到外部遗留的 8081 `llama-server`，supervisor 拒绝附着未知模型；
  清理后重跑通过。说明保护逻辑正常，不是上传功能问题。
- 清理：测试结束后无残留 `llama-server`，端口释放。

### 2026-10-30 19:54 CET

- T20 完成：重建 docs 知识库索引。
  - 使用 `KnowledgeBase.index(cfg.knowledge.docs_root)` + `LlamaCppEmbedder`；
  - 结果：`documents=37 chunks=318 dim=1024`，`runs/knowledge.db` 约 2.8 MB；
  - 检索验证：
    - `ADR-034 ngl ladder 降档重试` 顶部命中
      `decisions/ADR-034-ngl-ladder-retry.md`（score 0.5904）；
    - `ADR-035 token 预算 8192` 顶部命中
      `decisions/ADR-035-token-budget-8192.md`（score 0.6894）。
  - 索引结束后无残留 `llama-server`。
- T21 完成：把 `/tmp` 验证脚本固化进仓库。
  - `tests/test_turn_models.py`：`begin_turn` 重置、复用驻留模型仍计数、
    未 ensure 时快照为空；使用 `ModelProfile` 假对象，不依赖真实模型文件。
  - `tests/test_terminate.py`：验证 `start_new_session` 进程组父/子进程均被终止、
    二次调用幂等；异常路径会清理进程组。
  - `tests/__init__.py` 使 `unittest discover` 可用。
- T22 完成：`tests/run_all.py`（stdlib unittest discover + 非零失败退出码）。
  - `python3 tests/run_all.py`：4 tests，`OK`，exit 0；
  - `python3 -m unittest discover -s tests -t . -v` 也通过。
- T23 完成：
  - `pre_architecture.md` 新增第 24 条（知识库重建 + 仓库测试）；
  - 本文件回写 T20–T23 与关键事实；
  - 最终检查：`compileall` 通过，无残留测试监控/模型进程。
- 下一步：无阻塞。

### 2026-10-30 19:48 CET

- T16–T19 完成：token 预算统一为 8192（ADR-035）。
  - `[models.main].n_ctx`: 4096 → **8192**
  - `[harness].max_tokens`: 1024 → **8192**
  - `[harness].review_max_tokens`: 1536 → **8192**
  - `load()` 复核：`n_ctx=8192`、`max_tokens=8192`、`review_max_tokens=8192`；`compileall` 通过。
- 背景证据：T13 长指令的 planner prompt 3602–3690 tokens，4096 上下文只剩约 430 输出；
  日志 `n_tokens=4095, truncated=1`。单独提高 `max_tokens` 无效。
- T17 实跑（同一完整 Terminal-Bench 指令）：
  - `t-20261030-194629-b918e0`：计划成功（3 步），27.52 s，`review=PASS`、`failures=0`；
  - 峰值显存 **9136 MiB** / 12282 MiB；
  - `workspace.list_dir` + `workspace.write_file` 都成功，脚本内容 `grep -c ERROR "$1"`；
  - 但最终 answer 写成“I'll read the existing solution.sh…”（与实际写入不一致），
    review 仍判 PASS；属模型质量/评审覆盖问题。
  - 脚本评测：官方样例 → `2`、扩展隐藏日志 → `4`；无 ERROR 日志只输出 `0` 但退出码 1
    （`grep -c` 无匹配时返回 1），若用 `check_output` 仍会判失败。
- T18 回归：
  - `t-20261030-194707-7cb4b1`（ping）：24.05 s，`review=PASS`，`models={'main': 99, 'sub': 99}`；
  - 峰值 **9133 MiB**，无 OOM、无降档；结束后无残留 `llama-server`。
- T19 文档：
  - 新增 `docs/decisions/ADR-035-token-budget-8192.md`；
  - ADR-012 / ADR-019 标注数值由 ADR-035 取代；
  - `pre_architecture.md` 升到 v1.6，§11.1 增 `n_ctx=8192`，§12 新增第 23 条。
- 备注：临时对比测试（`n_ctx=8192` + `max_tokens=10000`）也通过计划阶段，
  峰值 9117 MiB；说明 8192 上下文本身足够，输出上限取 8192 是保守统一值。
- 清理：测试用的 `/tmp/agent_ctx8192.toml` 与 VRAM 监控进程已停止，无残留模型进程。

### 2026-10-30 18:31 CET

- T12 完成：`RunReport.models` 从“本进程曾加载过”改为“本轮 ensure 过”。
  - `ServerSupervisor` 新增 `_turn_used`、`begin_turn()`、`_record_use()`；
    `ensure()` 的冷启动与复用驻留分支都会记录；`snapshot()` 只返回本轮集合。
  - `RuntimeInfoPort` 增加 `begin_turn()`；`Harness.run_turn()` 在开头重置统计窗口。
  - 单元测试 `/tmp/test_turn_models.py`：`per-turn model accounting: OK`。
  - Web 同进程两轮实测：
    - 第 1 轮 `t-20261030-182753-a554bc`（knowledge.search）：
      `models={'main': 99, 'embed': None, 'sub': 99}`；
    - 第 2 轮 `t-20261030-182845-5985d6`（ping）：`models={'main': 99, 'sub': 99}`，
      `embed` 不再出现。
- T13 完成：外部 agent 评测（Terminal-Bench `parse-log-stats`，
  来源 `https://github.com/ayush0824/parse-log-stats`）。
  - 任务：写 Bash 脚本，接收一个日志路径，只输出含 `ERROR`（区分大小写）的行数。
  - 第 1 次（`t-20261030-182917-205617`）用完整任务描述：Q4 计划 JSON 被截断，
    `PLAN_INVALID`，0/1。
  - 第 2 次（`t-20261030-182940-186bd5`）用精简描述：29.64 s，review `PASS 1.00`，
    写出 `grep -c ERROR "$1"`。
  - 评测：官方样例 → `2`（通过）；扩展隐藏日志 → `4`（通过）；
    无 `ERROR` 的日志 → 输出两行 `0`（`grep -c` 退出码 1 触发 `|| echo 0`），边界失败。
  - 结果：官方样例 1/1；鲁棒性 2/3。暴露 Q4 两个问题：长指令导致计划 JSON 截断、
    空结果分支重复输出。
- T14 完成：TUI 真跑一轮（`t-20261030-183030-17e501`）。
  - PTY 下输入“Read artifacts/hello.txt…”，`workspace.read_file` 成功，
    答案 `The file contains: hello from agent`，20.0 s，`PASS 1.00`；
    `models={'main': 99, 'sub': 99}`，`/quit` 退出码 0，无残留进程。
- T15 完成：
  - ADR-034 第 7 条改为“按轮统计”，新增验证行 7/8；
  - `pre_architecture.md` 更新报告按轮统计、TUI 真跑，并新增第 22 条外部评测；
  - 最终回归：`compileall` OK；`per-turn model accounting: OK`；
    `process-tree termination: OK`；`--once "ping"`（`t-20261030-183219-00c89c`）
    17.73 s、`PASS 1.00`、`models={'main': 99, 'sub': 99}`、退出码 0；
    无残留进程/端口，显存 1094 MiB 已用。
- 下一步：无阻塞。若要继续，可针对 T13 暴露的两个 Q4 问题做提示/预算调优。

### 2026-10-30 18:22 CET

- T10 完成：修复外部 SIGTERM 导致的孤儿 `llama-server`（Web 冒烟时实际复现：
  python 进程被 SIGTERM 后 reviewer 模型仍占用 8081）。
  - `agent/__main__.py` 新增 `_sigterm_as_keyboard_interrupt()` 上下文管理器，
    把 SIGTERM 转为 `KeyboardInterrupt`，让现有 `finally: app.close()` 得以执行；
    退出码按惯例为 130。
  - 实测：Web 服务加载模型后 `kill -TERM <python-pid>`，1 s 内退出，
    进程退出码 **130**；`pgrep llama-server` 无结果、8090/8765 端口释放、
    显存回到约 1022 MiB。
- T11 完成：
  - Web：`/health` 200；`POST /message`（`X-Agent-Session: smoke`）后 SSE 收齐
    `USER_INPUT → PLAN → TOOL_CALL → TOOL_RESULT → REVIEW → REFLECTION → ANSWER → RUN_REPORT`；
    report `t-20261030-181931-eeff01` 为 `PASS 1.00`、`models={'main': 99, 'sub': 99}`，
    19.79 s，无失败。
  - TUI：`TERM=xterm-256color script -qec "python3 -m agent --tui" /dev/null` 启动成功，
    发送 `/quit` 后退出码 0，无残留进程。
  - `--once "ping" --json` 回归仍退出码 0（`t-20261030-182057-4055a5`），
    确认 SIGTERM 包装没有破坏单轮路径。
  - 最终回归（18:22 CET）`t-20261030-182214-b98121`：18.76 s，`PASS 1.00`，
    `models={'main': 99, 'sub': 99}`，退出码 0，无残留进程。
- 下一步：可继续用 Q4 主脑跑真实任务；当前没有阻塞项。

### 2026-10-30 18:20 CET

- T9 完成：RunReport 现在记录本进程实际拉起过的模型与 ngl。
  - `ServerSupervisor` 新增 `_loaded` 与 `snapshot()`（成功加载后写 alias→ngl，跨切换保留）。
  - `agent/ports.py` 新增 `RuntimeInfoPort`；`Harness` 注入 `runtime=self._supervisor`，
    构建报告时传 `models=snapshot()`。
  - `RunReport` 新增 `models` 字段；JSON payload 与 Markdown 新增 `## Models` 表。
  - 实测 `t-20261030-181800-13cdd1`：32.13 s，`retries=0 failures=0`，review `PASS 1.00`；
    报告 `models={'main': 99, 'sub': 99}`，Markdown 为 `| main | 99 |` / `| sub | 99 |`；
    ledger 的 `RUN_REPORT.payload.models` 与两份报告一致。
  - `compileall` 通过；运行后无残留 `llama-server`。
- 文档同步：ADR-034 增加第 7 条与验证行；`pre_architecture.md` §12 第 20 条补充报告证据。
- 下一步：TUI/Web 在 Q4 下的冒烟（可选）。

### 2026-10-30 18:12 CET

- T5 通过：`timeout 420 python3 -m agent --once "ping" --json` 退出码 0。
  - 输出：`kind=ANSWER`，`turn_id=t-20261030-181139-0ee453`，8 事件，
    `text="pong"`，`elapsed_s=33.47`。
  - ledger/report 均生成：`runs/t-20261030-181139-0ee453.{jsonl,report.json,report.md}`；
    `retries=0 failures=0`，plan/review/answer = 14.32/12.36/2.27 s。
  - `runs/server/main.log`：`=== start attempt 1/10: -ngl 99 ===` →
    `model loaded` → `listening` → `=== ready: main -ngl 99 (attempt 1/10) ===`；
    无 OOM，生成 41.80 tok/s。
  - 运行结束后无残留 `llama-server`，显存约 940 MiB（空闲 11056 MiB）。
  - 备注：reviewer 对 `pong` 判 `REVISE`（要求逐字回显 `ping`），属评审策略，
    不影响加载/推理/报告链路；`ping` 任务本身已端到端完成。
- T5 追加回归（18:13 CET）：`--once "Read artifacts/hello.txt and tell me its contents." --json`
  退出码 0，`turn_id=t-20261030-181332-3eea54`，18.22 s；
  计划 `workspace.read_file`，`TOOL_RESULT ok`（16 bytes），答案
  `"The file contains: hello from agent"`，review `PASS score=1.00`，`retries=0 failures=0`。
  日志再次为 `start attempt 1/10: -ngl 99` → `ready: main -ngl 99`，退出后无残留进程。
- T6 完成：新增
  `docs/decisions/ADR-033-main-model-q4-k-m.md`、
  `docs/decisions/ADR-034-ngl-ladder-retry.md`。
- T7 完成：`docs/pre_architecture.md` 更新到 v1.5：
  - §0 模型文件行、§11.1 `main` 行改为 Q4_K_M；
  - §12 第 2 条保留 TB-0 历史并注明当前主脑；
  - §12 新增第 20 条「Q4 主脑与 ngl 降档」，含本次实测数据。
- T8 完成：本文件任务清单与执行记录已回写。
- 复核：重新生成并运行 `/tmp/test_terminate.py`（`PYTHONPATH=<repo>`），
  验证 `start_new_session` 进程组内父/子进程均被终止且二次调用幂等：
  `process-tree termination: OK`。
- 下一步（可选）：把 `gpu_layers` 写入 run report，让每次运行的实际层数可追溯；
  若要继续验证，可再跑一次 TUI/Web 冒烟。

### 2026-10-30 17:51 CET

- 创建本文件。
- 待执行：T2 ~ T8。

### 2026-10-30 17:03 CET

- T2 完成：`defaults.toml` 的 `models.main` 改为 `gemma4-v2-Q4_K_M.gguf`，
  `n_gpu_layers = 99`；注释与 ADR 引用同步。
- T3 完成：
  - `llama_cpp.py` 新增 `_escalate()` 与 `ServerSupervisor._terminate_process_tree()`，
    `stop()` 改为 SIGTERM/SIGKILL 整个进程组；
  - 启动日志改为带 `attempt i/n`，成功时追加
    `=== ready: <alias> <-ngl N> (attempt i/n) ===`。
- T4 通过：
  - `python3 -m compileall -q agent` 无报错；
  - `load()` 输出 `main=/home/cielo_1/models/gemma4-v2-Q4_K_M.gguf`、
    `n_ctx=4096`、`n_gpu_layers=99`；
  - ladder = `(99, 32, 28, 24, 20, 16, 12, 8, 4, 0)`；
  - `/tmp/test_terminate.py` 验证进程组终止与幂等：`process-tree termination: OK`。
- 下一步：T5 实跑 `--once "ping" --json`。

## 6. 崩溃后的恢复方式

1. 读本文件第 2 节，找到第一个未勾选任务。
2. 读第 5 节最近一条记录，确认最后执行的命令与结果。
3. 用第 7 节命令复核当前状态，不要假设上一步已成功。
4. 从第一个未勾选任务继续；每完成一步立即回写第 5 节。

## 7. 状态复核命令

```bash
cd /home/cielo_1/projects/my_agent
python3 -m compileall -q agent
python3 - <<'PY'
from agent.config import load
c = load()
print(c.models['main'])
PY
rg -n "gemma4-v2" agent/config/defaults.toml
tail -40 runs/server/main.log
```
