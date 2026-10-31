# ADR-016：本地 Markdown 的 RAG 索引与检索

- **状态**：已采纳（2026-10-30）
- **关联代码**：`agent/knowledge/markdown.py:MarkdownLoader`、`Chunker`、`agent/knowledge/base.py:KnowledgeBase`、`cosine_fp64`、`agent/adapters/sqlite_vectors.py:SqliteVectorStore`、`agent/tools/knowledge.py:SearchTool`、`ReadSectionTool`、`IndexTool`
- **关联 ADR**：ADR-003（stdlib + FP64）、ADR-009（路径沙箱）、ADR-017（嵌入服务）、ADR-006（工具大类）
- **上游文档**：`docs/pre_architecture.md` §3 TB-3、§6.1 `knowledge.*`、ADR-4（嵌入模型选型）

## 背景

需求要求"通过 RAG 阅读外部 .md 写就的流程文件作为计划参考"。TB-1 已能让 agent 用 `read_file`
整篇读文件，但那是**全量读取**：文档一多就会把 prompt 撑爆，且模型无法"按需"取用。

## 决策

1. **分块以标题为骨架**：`MarkdownLoader` 把 `.md` 切成"标题 + 段落"的 section；
   `Chunker` 在 section 内按段落累积到 `chunk_chars`（默认 800）为止，超长段落硬切。
   不做滑动窗口重叠（重叠会让同一事实被多次引用，评审时容易重复计分）。
2. **向量用 FP64 存**：SQLite BLOB 为 `array("d").tobytes()`，余弦用纯 Python 双精度累加
   （与 ADR-003 一致）；查询时整表扫描——文档语料在千级 chunk，扫描成本可忽略。
3. **索引是全量重建**：`KnowledgeBase.index()` 先清空再写入（幂等）。理由：流程文档数量小、
   增量删除的边界条件（改名/删文件/改标题）比"重建 2 秒"贵得多。
4. **检索返回可引用片段**：`search(query, k)` 返回 `{doc_id, heading, score, text}`；
   `read_section(doc_id, heading)` 按标题前缀取回完整 section，供模型引用原文而不是复述向量。
5. **文档根可配**（`[knowledge] docs_root`），`index` 工具的 `path` 参数走同一套护栏沙箱（ADR-009），
   因此"索引 /etc"同样会被拒绝。
6. 检索结果中的 `score` 一律 FP64 且落在 `[-1, 1]`；不排序时不得返回（必须按 score 降序）。

## 理由

- 标题骨架让每个 chunk 自带上下文（"这段属于哪个小节"），比固定窗口更贴合"流程文件"的写法。
- 全量重建把"索引是否与磁盘一致"变成一个不需要思考的问题；对几十份 .md 来说这是最便宜的确定性。
- 整表扫描避免了引入向量索引（HNSW 等）的依赖与调参成本，符合"非必要不集成"。

## 代价

- 大语料（>10 万 chunk）检索会线性变慢；届时再评估 SQLite 扩展或 ANN，需新 ADR。
- 每个 chunk 都是独立文本，跨小节的长论证可能被切断；`read_section` 是补偿手段。
- 全量重建期间若进程被杀，索引可能处于"已清空未写完"的中间态；下次 index 会重建，因此可接受。

## 验证方式

- 单测式断言：chunker 产出的 chunk 数 > 0 且每个 chunk 非空；`cosine_fp64(a, a) == 1.0`；
  SQLite 往返后向量逐位相等（FP64 无损）。
- 实跑：`index docs` 后 `search "guardrail"` 的 top-1 必须来自 `docs/`，且 `read_section` 能取回原文。

## 补充（2026-10-30 实跑结论）

- 索引实测：`docs/` → **18 文档 / 160–161 chunk / dim=1024**，耗时含两次模型切换约 51 s。
- 检索实测：查询 "how are tool calls guarded" 的 top-3 全为相关 ADR（0.689 / 0.663 / 0.584），
  答案被评审判为 `PASS`，内容与 ADR-009 原文一致。
- **已知限制（由静态计划导致）**：计划在检索前一次性生成，步骤参数无法引用后续步骤的输出，
  因此模型会臆造 `doc_id` 去调 `read_section`（得到 `NOT_FOUND`，被当作数据而非崩溃）。
  缓解措施已落地：检索片段预览提升到 800 字符（一次 search 即够作答）+ 提示词禁止臆造标识符；
  彻底解决需要"步骤间数据引用"，属于后续切片的动作空间改造（暂不引入占位符 DSL）。
