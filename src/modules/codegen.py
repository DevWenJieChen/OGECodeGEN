from __future__ import annotations

from typing import List
import re
from src.core.pipeline_state import PipelineState, KnowledgeDoc, docs_to_text
from src.tools.model_clients import ChatLLMClient  # Your Chat LLM wrapper
from src.tools import prompt_loader


def _safe_group(pls: PipelineState, key: str) -> List[KnowledgeDoc]:
    """Fetch one group of docs from pls.knowledge_docs Dict."""
    if not isinstance(pls.knowledge_docs, dict):
        return []
    docs = pls.knowledge_docs.get(key) or []
    return docs if isinstance(docs, list) else []


def _data_recommendations_text(pls: PipelineState, max_chars: int = 8000) -> str:
    """
    Data retrieval module output: pls.data_docs already contains one KnowledgeDoc per recommendation.
    Directly using docs_to_text is sufficient and keeps the size controllable.
    """
    if not pls.data_docs:
        return "(empty)"
    return docs_to_text(pls.data_docs, max_chars=max_chars)


def _task_steps_text(pls: PipelineState) -> str:
    """
    Convert task steps (task decomposition) to text using pls.task_steps, if the main field has been added; used for the first-stage generation process.
    """
    steps = getattr(pls, "task_steps", None)

    if not steps:
        return "(empty)"

    steps = [str(s).strip() for s in steps if str(s).strip()]
    return "\n".join([f"{i+1}. {s}" for i, s in enumerate(steps)])


def _get_last_delta_docs(pls: PipelineState, key: str) -> List[KnowledgeDoc]:
    """
    Read the most recent retrieval delta from trace (optional capability).
    Convention: after each RUN_RETRIEVAL_KNOWLEDGE action, write:
      pls.trace["react"]["last_delta"] = {"tasks": [...], "operators": [...], ...}
    """
    react = (pls.trace or {}).get("react") or {}
    last_delta = react.get("last_delta") or {}
    docs = last_delta.get(key) or []
    return docs if isinstance(docs, list) else []

def _docs_preview(docs, per_doc_chars=1000, max_docs=20):
    """
    Take the first max_docs entries from docs, using the first per_doc_chars characters from each entry.
    Simplify the full context.
    """
    out = []
    for d in (docs or [])[:max_docs]:
        text = (getattr(d, "text", "") or "").strip()
        preview = text[:per_doc_chars] + ("..." if len(text) > per_doc_chars else "")
        out.append(f"- name={getattr(d,'name',None)} score={getattr(d,'score',None)}\n  {preview}")
    return "\n".join(out) if out else "(empty)"

def _normalize_generated_code(raw: str) -> str:
    """
    Normalize the code text returned by the LLM:
    1) If a ```python ... ``` / ``` ... ``` code block exists, extract the first code block first.
    2) If there is no complete code block, try to remove leading and trailing fences.
    3) Finally, apply strip().
    """
    if not isinstance(raw, str):
        return raw

    s = raw.strip()
    if not s:
        return s

    # 1) Prefer extracting the fenced code block content.
    m = re.search(r"```(?:python|py)?\s*\n(.*?)\n```", s, flags=re.DOTALL | re.IGNORECASE)
    if m:
        return m.group(1).strip()

    # 2) If there is no complete block, try to remove the leading and trailing fences.
    s = re.sub(r"^\s*```[a-zA-Z0-9_+-]*\s*\n", "", s)
    s = re.sub(r"\n\s*```\s*$", "", s)

    return s.strip()

def run(
    pls: PipelineState,
    *,
    llm: ChatLLMClient,
    mode: str = "fresh",    # "fresh" | "repair"
    fix_instruction: str | None = None,
    # use_knowledge_delta: bool = False,
) -> PipelineState:
    """
    Code generation module supporting two modes:
    - fresh: first-time generation (the first pass of the linear workflow)
    - repair: ReAct repair workflow (repair based on previous_code + verify_report + fix_instruction)

    Parameters:
    - mode:
        "fresh"  -> generate with the fresh template
        "repair" -> repair with the repair template
    - fix_instruction:
        Strongly recommended in repair mode; used to tell the model exactly what to change.
    - use_knowledge_delta:
        Only affects whether the "delta knowledge section" is injected into the repair prompt; it does not change the full knowledge working set in state.
        True  -> additionally include trace.react.last_delta in the prompt (new knowledge retrieved during the most recent supplementary retrieval)
        False -> do not include the delta section (the full knowledge working set is still included)
    """
    pls.trace.setdefault("codegen", {})
    if mode not in ("fresh", "repair"):
        mode = "fresh"

    # Load the base context content from prompts.
    syntax_rules = prompt_loader.load("codegen/oge_syntax.md")
    intent_json = pls.intent_json or "(empty)"
    data_recommendations = _data_recommendations_text(pls, max_chars=8000)
    # Full working set (knowledge after the default merge).
    task_docs_full = _safe_group(pls, "tasks")
    operator_docs_full  = _safe_group(pls, "operators")

    task_knowledge_full  = docs_to_text(task_docs_full, max_chars=100000) if task_docs_full else "(empty)"
    operator_knowledge_full  = docs_to_text(operator_docs_full , max_chars=220000) if operator_docs_full  else "(empty)"

    # Initialize delta variables so they exist in all branches, although they are unused in fresh mode.
    task_docs_delta = []
    operator_docs_delta = []

    # ---------- Select template ----------
    if mode == "fresh":
        # Continue using the existing fresh template (do not add new fields; keep it stable).
        system_prompt = prompt_loader.render(
            "codegen/codegen_system_prompt_fresh.md",
            syntax_rules=syntax_rules,
            language="Python"
        )
        user_prompt = prompt_loader.render(
            "codegen/codegen_user_prompt_fresh.md",
            user_query=pls.user_query,
            intent_json=intent_json,
            data_recommendations=data_recommendations,
            task_steps=_task_steps_text(pls),  # If you added it
            task_knowledge=task_knowledge_full,
            operator_knowledge=operator_knowledge_full,
            user_lang=pls.lang

        )
        # user_prompt = f"The user's question is: {pls.user_query}. Please generate code according to the system instructions."

    else:
        # Additional context for repair, using the new template codegen_system_prompt_repair.md.
        task_docs_delta = _get_last_delta_docs(pls, "tasks")
        operator_docs_delta = _get_last_delta_docs(pls, "operators")
        task_knowledge_delta = docs_to_text(task_docs_delta, max_chars=80000) if task_docs_delta else "(empty)"
        operator_knowledge_delta = docs_to_text(operator_docs_delta,
                                                max_chars=120000) if operator_docs_delta else "(empty)"
        previous_code = pls.code
        verify_report = pls.verify_report
        operator_knowledge_full_preview = _docs_preview(operator_docs_full,)
        task_knowledge_full_preview = _docs_preview(task_docs_full)
        fix_instruction_text = (fix_instruction or "").strip() or "(empty)"

        system_prompt = prompt_loader.render(
            "codegen/codegen_system_prompt_repair.md",
            syntax_rules=syntax_rules
        )
        user_prompt = prompt_loader.render(
            "codegen/codegen_user_prompt_repair.md",
            user_query=pls.user_query,
            intent_json=intent_json,
            data_recommendations=data_recommendations,
            task_knowledge_full_preview=task_knowledge_full_preview,
            operator_knowledge_full_preview=operator_knowledge_full_preview,
            task_knowledge_delta=task_knowledge_delta,
            operator_knowledge_delta=operator_knowledge_delta,
            previous_code=previous_code,
            verify_report=verify_report,
            fix_instruction=fix_instruction_text,
            language="Python",
            user_lang=pls.lang
        )

    # Call the LLM to generate code.
    raw_code = llm.invoke(user_prompt=user_prompt, system_prompt=system_prompt)
    code = _normalize_generated_code(raw_code)

    # Write back to PipelineState.
    pls.code = code
    pls.trace["codegen"].update({
        "mode": mode,
        "data_docs": len(pls.data_docs or []),
        "task_docs_full": len(task_docs_full),
        "operator_docs_full": len(operator_docs_full),
        "task_docs_delta": len(task_docs_delta),
        "operator_docs_delta": len(operator_docs_delta),
        "has_intent": bool(pls.intent_json),
        "has_bbox": bool(pls.task_bbox),
        "has_previous_code": bool((pls.code or "").strip()) if mode == "repair" else False,
        "has_verify_report": bool((pls.verify_report or "").strip()) if mode == "repair" else False,
    })
    return pls