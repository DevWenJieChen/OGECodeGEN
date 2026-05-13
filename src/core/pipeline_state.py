from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, List, Dict


@dataclass
class KnowledgeDoc:
    """
    Unified retrieval document structure, regardless of whether it comes from the data knowledge base or the task/operator knowledge base.
    """
    source: str          # Document source identifier (collection/file name/URL, etc.)
    text: str            # Document body text (already chunked)
    score: float | None = None  # Optional: similarity score/reranking score
    name: str = ""  # Name


@dataclass
class PipelineState:
    """
    Full-pipeline context: the five modules exchange information only through this object to achieve decoupling.
    """
    # Input: user_query_raws is the original input; user_query is the translated version and is always kept in Chinese.
    user_query: str
    user_query_en: Optional[str] = None

    # Task region
    task_bbox:Optional[list] = None

    # 1 Intent-understanding output (using str to store JSON text is the simplest approach here)
    intent_json: Optional[str] = None

    # 2 Data knowledge retrieval output
    data_docs: List[KnowledgeDoc] = field(default_factory=list)

    # 3 Task/operator knowledge retrieval output {"operators": [], "tasks": []}; the accumulated knowledge working set for codegen. Each retrieval appends to it and writes it back after deduplication.
    knowledge_docs: Dict[str, List[KnowledgeDoc]] = field(default_factory=dict)

    # 4 Code generation output
    code: Optional[str] = None

    # 5 Verification and execution output
    verify_ok: Optional[bool] = None
    verify_report: Optional[str] = None

    # Debugging/tracing (optional): records key intermediate values, etc.
    trace: Dict[str, Any] = field(default_factory=dict)

    # Task decomposition steps
    task_steps: list[str] | None = None

    # Output language control ("zh" / "en")
    lang: str = "zh"

    # English information: used to display information in English when running in en mode.
    en_info: Dict[str, Any] = field(default_factory=dict)

    # Whether the corresponding modules exist; used only for ablation experiments in the paper to indicate whether each module is used.
    has_modules: Dict[str, bool] = field(default_factory=dict)

    # Maximum number of repair attempts
    max_fix_num: int = 0

    # Experiment-specific field
    data_info: Optional[str] = ""

def docs_to_text(docs: List[KnowledgeDoc], max_chars: int = 6000) -> str:
    """
    Convert a Doc list into text that can be injected into a prompt, and limit its length to avoid token explosion (the current context is already large, so optimization is not considered yet).
    """
    chunks: List[str] = []
    total = 0
    for i, d in enumerate(docs, 1):
        block = f"[{i}] name={d.name} \n score={d.score}\n{d.text}\n"
        if total + len(block) > max_chars:
            break
        chunks.append(block)
        total += len(block)
    return "\n".join(chunks) if chunks else "(empty)"
