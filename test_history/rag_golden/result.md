# RAG Baseline — 2026-10-30 23:50 CET

## 索引

```text
documents=45  chunks=392  dim=1024
FTS5: trigram
WORKLOG indexed: False
```

`WORKLOG.md` 是运行记录，不是知识；通过 `[knowledge].exclude` 排除。

## 检索配置

```text
top_k=5
candidate_k=20
parent_chars=2400
hybrid=true
rrf_k=60
parent = Markdown section
procedure roll-up = sibling sections of top document
```

## 指标

```text
Recall@1: 0.909
Recall@3: 1.000
Recall@5: 1.000
MRR: 0.955
Outline coverage: 1.000
```

## 对话验证

### section 父块 + roll-up

```text
t-20261030-233952-a66888
```

- 计划：中文查询 + 英文查询各一次
- 回答：列出 8 个步骤 + DoD
- `REVIEW PASS 1.00`
- 45.02 s
- `retries=0 failures=0`

### 文档级父块对照

```text
t-20261030-232653-af1fa3
```

- 中文 + 英文双查询
- 列出 8 步 + DoD
- `PASS 1.00`
- 42.62 s
- `retries=0 failures=0`

### 旧 chunk 检索

```text
t-20261030-231058-ba2b88
```

- 只列出 3 个步骤
- `PASS 1.00`，但 RAG 覆盖不足

## 结论

- 父块现在是 section，符合“门牌号”粒度；
- 流程/步骤类问题通过 sibling roll-up 拿到同文档的其余 section；
- `WORKLOG.md` 已排除，Recall@1 从 0.727 提升到 0.909；
- answer prompt 按 section 去重并限制总字符数，避免父块膨胀导致 400。
