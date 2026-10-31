# ADR-038：section 父块、FTS5 hybrid 与 RAG 基线

- 状态：已落地（2026-10-30）
- 关联：ADR-016（Markdown RAG）、ADR-017（embedding）、ADR-037（评审门禁）

## 背景

原先 `knowledge.search` 只返回 top-k **chunk**。命中长文档时经常只拿到开头或某一小节，
导致回答只覆盖部分步骤；同时没有词法召回，中文/英文查询的覆盖和排序都不稳定。
父块文本变长后，两个查询的结果又可能把 answer prompt 挤出 `n_ctx`。

## 决策

### 1. 父块是 section，不是文档

- `Chunk` 增加 `parent_id`；父块粒度是 **Markdown section**：
  `parent_id = doc_id#section-NNN`。
- 索引仍只嵌入 child chunk；检索命中 child 后返回该 **section** 的父块文本。
- `parent_chars=2400`；超长时优先保留命中 chunk 的前后相邻 chunk。
- `Hit` 增加 `parent_text`、`outline`、`document_outline`、`vector_score`、`lexical_score`。
- `knowledge.search` 的结果返回：
  - `text`：命中 section 的文本
  - `matched_text`：命中的 child 预览
  - `outline`：该 section 的标题列表
  - `document_outline`：整篇文档的标题列表，仅用于评审覆盖度，不替代 section 父块

### 2. 流程类问题的 sibling roll-up

“父块是 section”不等于“流程问题只能拿到一个房间”。当 query 命中
`步骤 / 流程 / 有哪些 / steps / process / how to / checklist` 时：

- top hit 的文档的所有 sibling section 会一并加入结果；
- 每个结果仍然是 section 父块，不是整篇文档父块；
- roll-up 最多 12 个 section；
- 结果顺序是：top section → 同文档 sibling sections → 其他文档。

这样既保留“门牌号”粒度，又能让“完整流程”问题拿到全部步骤。

### 3. 召回与融合

- `candidate_k=20`：先 over-fetch，再按 section 父块去重。
- 向量排名与 FTS5 BM25 排名用 RRF 融合，`rrf_k=60`。
- 轻量启发式 rerank：RRF + query 字符覆盖率 + heading 覆盖率，不引入额外模型。

### 4. WORKLOG 不进知识库

`WORKLOG.md` 是运行记录，不是可检索知识。新增：

```toml
[knowledge]
exclude = ["WORKLOG.md"]
```

`KnowledgeBase.index` 用 `fnmatch` 按相对 `docs_root` 的路径跳过它。

### 5. SQLite FTS5

- 新增 `chunks_fts` 虚表，优先 `tokenize='trigram'`，失败回退 `unicode61`。
- 中文 trigram 可匹配 3 字以上片段；短查询回退 `LIKE`。
- `replace_all` 在同一事务中同步 `chunks` 与 `chunks_fts`。
- 旧库启动时自动补 `parent_id` 列并重建 FTS。

### 6. answer context 预算

- `_compact_answer_payload` 按 `(doc_id, heading)` 去重，保留同文档的不同 section。
- 父块总字符预算 `_ANSWER_CONTEXT_CHARS=8000`；超长截断并加省略号。
- 非检索工具结果超过 2000 字符时改为 `preview`。
- `_fallback_answer` 也只输出压缩后的内容，避免再次撑爆上下文。

### 7. 评审覆盖度

`_REVIEW_PROMPT` 增加规则：用户要求步骤/流程且 hit 带
`outline` / `document_outline` 时，评审要检查草稿是否覆盖主要标题；
只覆盖一小部分则 `REVISE`。

### 8. 基线

- `test_history/rag_golden/queries.jsonl`（12 条，含 1 条无答案）。
- `test_history/rag_golden/eval_rag.py`：Recall@1/3/5、MRR、document outline coverage。
- `test_history` 仍在 guardrail `deny_names` 中，agent 不能读取期望值。

## 验证

```text
python3 tests/run_all.py
Ran 20 tests ... OK
```

索引：

```text
documents=45  chunks=392  dim=1024
FTS5: trigram
WORKLOG indexed: False
```

基线：

```text
Recall@1: 0.909
Recall@3: 1.000
Recall@5: 1.000
MRR: 0.955
Outline coverage: 1.000
```

对话验证：

| 运行 | 结果 |
| --- | --- |
| `t-20261030-231058-ba2b88`（旧 chunk 检索） | 只列 3 个步骤，`PASS 1.00` |
| `t-20261030-232653-af1fa3`（文档级父块） | 中文 + 英文双查询，列 8 步 + DoD，`PASS 1.00`，42.62 s |
| `t-20261030-233952-a66888`（section 父块 + roll-up） | 中文 + 英文双查询，列 8 步 + DoD，`PASS 1.00`，45.02 s，`retries=0 failures=0` |

## 代价与限制

- section 父块可能缺少跨 section 的上下文；流程问题依赖 roll-up 规则。
- roll-up 是查询模式触发，不是所有多步问题都能自动覆盖。
- FTS5 trigram 对 2 字中文查询不可用；会回退 LIKE。
- 启发式 rerank 不是 cross-encoder，只做轻量排序。
- 父块扩展会增加 prompt token；已用 2400 字符上限和 answer payload 去重控制。

## 后续

- 用更多真实查询校准 roll-up 触发词。
- 在有评测数据后，再考虑真正的 cross-encoder reranker。
- 评估 section 级摘要索引，减少跨 section 依赖。
