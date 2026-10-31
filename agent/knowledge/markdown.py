"""markdown — Markdown 加载与按标题分块。

Module: agent.knowledge.markdown
Responsibility: 把 .md 解析成带标题层级的 Section，再按字符预算切成语义自洽的 Chunk。
Depends on: stdlib (dataclasses, re, pathlib)
Depended by: knowledge.base（索引流程）
ADRs: ADR-016（标题骨架 + 无重叠分块）
"""

import re
from dataclasses import dataclass
from pathlib import Path

from agent.ports import Chunk

_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")


@dataclass(frozen=True, slots=True)
class Section:
    """文档中的一个小节。

    前置条件：level 在 1..6；heading 非空。
    后置条件：text 为标题下的正文（不含标题行）。
    禁止事项：不要把多级标题压平（层级用于拼回上下文）。
    """

    level: int
    heading: str
    text: str


@dataclass(frozen=True, slots=True)
class Document:
    """一份 Markdown 文档。

    前置条件：doc_id 在索引内唯一（通常为相对路径）。
    后置条件：sections 按出现顺序排列；首个 section 的 heading 可为文件名（无标题时）。
    禁止事项：不要在多份文档间复用 doc_id。
    """

    doc_id: str
    title: str
    sections: tuple[Section, ...]


class MarkdownLoader:
    """把 .md 文件读成 Document。

    前置条件：path 指向可读的 UTF-8 文件。
    后置条件：返回 Document；无标题文件会生成一个以文件名命名的 section。
    禁止事项：不要在此做分块（分块属于 Chunker，便于单独调参）。
    """

    def load(self, path: Path, *, doc_id: str) -> Document:
        """读取并解析一份 Markdown。

        前置条件：path 存在且为文件。
        后置条件：返回 Document；空文件产生空 sections。
        禁止事项：不要吞掉解码错误（用 replace 并保留内容更好排查）。
        异常：OSError（不可读）。
        """
        text = path.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()

        sections: list[Section] = []
        level = 0
        heading = path.stem
        body: list[str] = []
        title = path.stem

        for line in lines:
            match = _HEADING.match(line)
            if match:
                sections.append(Section(level=level, heading=heading, text="\n".join(body).strip()))
                level = len(match.group(1))
                heading = match.group(2).strip() or path.stem
                if level == 1 and title == path.stem:
                    title = heading
                body = []
                continue
            body.append(line)
        sections.append(Section(level=level, heading=heading, text="\n".join(body).strip()))

        # 标题行之前的空 section 没有内容，丢弃以免产生空 chunk。
        kept = tuple(section for section in sections if section.text)
        return Document(doc_id=doc_id, title=title, sections=kept)


class Chunker:
    """把 Document 切成带标题前缀的 Chunk。

    前置条件：max_chars >= 100。
    后置条件：每个 chunk 的 text 以标题行开头；ordinal 在文档内连续递增。
    禁止事项：不要做滑动窗口重叠（ADR-016 明确不重叠）。
    """

    def __init__(self, *, max_chars: int = 800) -> None:
        if max_chars < 100:
            raise ValueError("max_chars must be >= 100")
        self._max_chars = max_chars

    def split(self, document: Document) -> list[Chunk]:
        """切分一份文档。

        前置条件：document 来自 MarkdownLoader。
        后置条件：返回按原文顺序排列的 Chunk 列表（可能为空）。
        禁止事项：不要跨 section 合并（会让 heading 与实际内容不符）。
        异常：无。
        """
        chunks: list[Chunk] = []
        ordinal = 0
        for section_index, section in enumerate(document.sections):
            prefix = f"{'#' * max(section.level, 1)} {section.heading}\n\n"
            parent_id = f"{document.doc_id}#section-{section_index:03d}"
            for piece in self._split_text(section.text):
                chunks.append(
                    Chunk(
                        chunk_id=f"{document.doc_id}#{ordinal:03d}",
                        doc_id=document.doc_id,
                        heading=section.heading,
                        ordinal=ordinal,
                        text=f"{prefix}{piece}",
                        parent_id=parent_id,
                    )
                )
                ordinal += 1
        return chunks

    def _split_text(self, text: str) -> list[str]:
        """按段落累积切分正文。

        前置条件：text 可为空串。
        后置条件：每个片段长度不超过 max_chars（超长段落被硬切）。
        禁止事项：不要丢弃空白段落之外的任何字符。
        异常：无。
        """
        if not text:
            return []
        budget = self._max_chars
        pieces: list[str] = []
        buffer = ""
        for paragraph in text.split("\n\n"):
            paragraph = paragraph.strip()
            if not paragraph:
                continue
            candidate = f"{buffer}\n\n{paragraph}" if buffer else paragraph
            if len(candidate) <= budget:
                buffer = candidate
                continue
            if buffer:
                pieces.append(buffer)
            while len(paragraph) > budget:
                pieces.append(paragraph[:budget])
                paragraph = paragraph[budget:]
            buffer = paragraph
        if buffer:
            pieces.append(buffer)
        return pieces
