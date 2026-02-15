import re
import uuid
from typing import List, Dict, Any, Optional


# Matches headers emitted by ingest flattening, e.g.:
# [PDF page 1] [ocr_page]
# [PDF page 12] [pdf_text]
_PAGE_HEADER_RE = re.compile(
    r"^\s*\[PDF\s+page\s+(?P<page>\d+)\]\s*(?:\[(?P<kind>[^\]]+)\])?\s*$",
    re.IGNORECASE,
)


def _new_id() -> str:
    return str(uuid.uuid4())


def _normalize(text: str) -> str:
    text = text.replace("\x00", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _split_paragraphs(text: str) -> List[str]:
    parts = re.split(r"\n\s*\n", text)
    return [p.strip() for p in parts if p.strip()]


def _split_sentences(text: str) -> List[str]:
    parts = re.split(r"(?<=[.!?])\s+", text.strip())
    return [p.strip() for p in parts if p.strip()]


def approx_token_count(text: str) -> int:
    return max(1, len(re.findall(r"\S+", text)))


def _parse_sections_with_page_headers(contract_text: str) -> List[Dict[str, Any]]:
    """
    Parse flattened contract_text.txt into sections that preserve PDF page boundaries
    based on inline headers like: [PDF page 1] [ocr_page].

    Returns list of sections:
      { page_num: Optional[int], block_kind: Optional[str], text: str }
    """
    text = contract_text.replace("\r\n", "\n")
    lines = text.split("\n")

    sections: List[Dict[str, Any]] = []
    cur_page: Optional[int] = None
    cur_kind: Optional[str] = None
    cur_lines: List[str] = []

    def flush():
        nonlocal cur_lines
        body = _normalize("\n".join(cur_lines))
        if body:
            sections.append(
                {
                    "page_num": cur_page,
                    "block_kind": cur_kind,
                    "text": body,
                }
            )
        cur_lines = []

    for line in lines:
        m = _PAGE_HEADER_RE.match(line)
        if m:
            # New header begins => flush previous section, then switch page/kind.
            flush()
            cur_page = int(m.group("page"))
            cur_kind = m.group("kind")
            continue

        cur_lines.append(line)

    flush()

    # If no headers were found, treat whole text as one section
    if not sections and _normalize(contract_text):
        sections = [{"page_num": None, "block_kind": None, "text": _normalize(contract_text)}]

    return sections


def chunk_contract_text(
    contract_text: str,
    max_tokens: int = 650,
    overlap_tokens: int = 120,
    *,
    source_name: Optional[str] = None,
    source_type: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Deterministic chunking that preserves PDF page boundaries when present.

    - Detects inline headers created by ingestion flattening: [PDF page N] [kind]
    - Splits each section into paragraphs -> sentences
    - Packs sentences into chunks up to max_tokens
    - Overlap is applied within the same page section (by default)

    Returned chunk dict:
      {
        "id": str,
        "text": str,
        "token_estimate": int,
        "page_num": Optional[int],
        "source_name": Optional[str],
        "source_type": str,
        "block_kind": Optional[str],
      }
    """
    text = _normalize(contract_text)
    if not text:
        return []

    st = (source_type or "unknown").strip().lower()
    if st not in {"pdf", "docx", "unknown"}:
        st = "unknown"

    sections = _parse_sections_with_page_headers(text)

    chunks: List[Dict[str, Any]] = []

    # Chunk state (per section)
    cur_sents: List[str] = []
    cur_tok = 0

    # Metadata state
    cur_page_num: Optional[int] = None
    cur_block_kind: Optional[str] = None

    def flush():
        nonlocal cur_sents, cur_tok
        if not cur_sents:
            return
        chunk_text = _normalize(" ".join(cur_sents))
        chunks.append(
            {
                "id": _new_id(),
                "text": chunk_text,
                "token_estimate": approx_token_count(chunk_text),
                "page_num": cur_page_num,
                "source_name": source_name,
                "source_type": st,
                "block_kind": cur_block_kind,
            }
        )
        cur_sents = []
        cur_tok = 0

    for sec in sections:
        # Start a new metadata context for this section
        # We don't force-flush here; we flush and reset so overlap doesn't cross pages.
        if cur_sents:
            flush()

        cur_page_num = sec.get("page_num")
        cur_block_kind = sec.get("block_kind")

        paras = _split_paragraphs(sec["text"])

        for para in paras:
            sents = _split_sentences(para) or [para]
            for s in sents:
                t = approx_token_count(s)

                if cur_tok + t > max_tokens and cur_sents:
                    prev = " ".join(cur_sents)
                    flush()

                    # overlap seed from previous chunk tail (same section/page)
                    if overlap_tokens > 0 and prev:
                        prev_sents = _split_sentences(prev)
                        tail: List[str] = []
                        tail_tok = 0
                        for ss in reversed(prev_sents):
                            tt = approx_token_count(ss)
                            if tail_tok + tt > overlap_tokens:
                                break
                            tail.insert(0, ss)
                            tail_tok += tt

                        cur_sents = tail[:] if tail else []
                        cur_tok = sum(approx_token_count(x) for x in cur_sents)

                cur_sents.append(s)
                cur_tok += t

        flush()

    return chunks
