"""chunk 切分：按章节切块（段落对齐 + 滑窗重叠），并生成 locator 元数据。

locator 是引用/溯源的生命线（开发计划 §1.1 判断 4）：
  卷 vol / 章号 chapter_no / 章题 chapter_title / 段落范围 para_start-end /
  虚拟页码 page_start-end（按每页 1200 字折算，电子版页码口径）。
"""
from ..config import settings
from .dedup import content_hash, dedup_paragraphs

PAGE_CHARS = 1200  # 虚拟页码折算：每页约 1200 字


def _page_of(book_offset: int, local_offset: int) -> int:
    return (book_offset + local_offset) // PAGE_CHARS + 1


def chunk_chapters(book) -> list:
    """输入 ParsedBook，输出 chunk dict 列表（不含 embedding/FTS）。"""
    chunks: list = []
    book_offset = 0
    chunk_size = settings.chunk_size
    overlap = settings.chunk_overlap
    for ch in book.chapters:
        paras = dedup_paragraphs(ch.paragraphs)
        if not paras:
            continue
        local_offsets = []
        offset = 0
        for p in paras:
            local_offsets.append(offset)
            offset += len(p) + 1
        # 段落窗口累积
        windows: list = []
        cur_idx: list = []
        cur_len = 0
        for i, p in enumerate(paras):
            plen = len(p) + 1
            if cur_len + plen > chunk_size and cur_idx:
                windows.append(cur_idx)
                # 回退 overlap：从尾部挑段落，凑出约 overlap 字符
                back: list = []
                back_len = 0
                for j in reversed(cur_idx):
                    back_len += len(paras[j]) + 1
                    back.insert(0, j)
                    if back_len >= overlap:
                        break
                cur_idx = back
                cur_len = back_len
            cur_idx.append(i)
            cur_len += plen
        if cur_idx:
            windows.append(cur_idx)
        # 去掉纯前缀重叠的冗余窗口（后窗完全被前窗包含时跳过）
        prev_key = None
        chunk_no = 0
        for idxs in windows:
            text = "\n".join(paras[i] for i in idxs)
            key = (idxs[0], idxs[-1])
            if key == prev_key:
                continue
            prev_key = key
            chunk_no += 1
            start_local = local_offsets[idxs[0]]
            end_local = local_offsets[idxs[-1]] + len(paras[idxs[-1]])
            chunks.append(
                {
                    "chunk_no": chunk_no,
                    "text": text,
                    "n_chars": len(text),
                    "vol": ch.vol,
                    "chapter_no": ch.no,
                    "chapter_title": ch.title,
                    "para_start": idxs[0] + 1,
                    "para_end": idxs[-1] + 1,
                    "page_start": _page_of(book_offset, start_local),
                    "page_end": _page_of(book_offset, end_local),
                    "content_hash": content_hash(text),
                }
            )
        book_offset += offset
    return chunks
