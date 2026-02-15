from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Literal


SourceType = Literal["pdf", "docx", "text"]
ArtifactType = Literal["contract_original", "contract_text", "chunks", "qad_json", "qad_excel", "qad_csv", "review_packet"]


@dataclass(frozen=True)
class Citation:
    """
    Evidence pointer back to the contract source.
    Keep this small but useful for reviewer trust.
    """
    source_type: SourceType
    source_name: str  # uploaded filename
    page_start: Optional[int] = None
    page_end: Optional[int] = None
    section_hint: Optional[str] = None
    excerpt: Optional[str] = None  # short snippet (safe length)


@dataclass(frozen=True)
class DocumentBlock:
    """
    A normalized unit extracted from a contract:
    - from a PDF page region
    - from a DOCX paragraph
    - from a DOCX table cell
    """
    id: str
    text: str
    source_type: SourceType
    source_name: str
    page_num: Optional[int] = None
    block_kind: Optional[str] = None  # "paragraph" | "table_cell" | "ocr_page" etc.
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Chunk:
    """
    Chunk used for embeddings + RAG retrieval.
    """
    id: str
    text: str
    source_blocks: List[str]  # DocumentBlock ids included
    citation: Citation


@dataclass(frozen=True)
class QADFieldEvidence:
    """
    One piece of evidence supporting a specific QAD field value.
    """
    citation: Citation
    rationale: str  # why this evidence supports the value


@dataclass(frozen=True)
class QADRowMapping:
    """
    Canonical JSON output target (model produces this).
    This maps SharePoint column names -> values, plus evidence.
    """
    program: Optional[str]
    contract_id: Optional[str]
    contract_version: Optional[str]
    effective_date: Optional[str]  # ISO date preferred, e.g., 2025-12-28

    columns: Dict[str, Any]  # SharePoint column name -> value
    evidence: Dict[str, List[QADFieldEvidence]]  # column name -> evidence list

    notes: Optional[str] = None
