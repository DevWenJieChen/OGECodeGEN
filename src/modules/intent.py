from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from typing import Any, Dict, Optional
from pydantic import BaseModel


from src.tools.model_clients import ChatLLMClient
from src.core.pipeline_state import PipelineState
from src.tools import prompt_loader

CORE_KEYS = ["time_range", "space_region", "object_type", "task_type", "data_constraints","required_outputs"]

class IntentEnInfo(BaseModel):
    user_query_zh: Optional[str] = None
    user_query_en: Optional[str] = None
    time_range: Optional[str] = None
    space_region: Optional[str] = None
    object_type: Optional[str] = None
    task_type: Optional[str | list[str]] = None
    data_constraints: Optional[str] = None
    required_outputs: Optional[list[str] | str] = None


class IntentOutput(BaseModel):
    time_range: Optional[str] = None
    space_region: Optional[str] = None
    object_type: Optional[str] = None
    task_type: Optional[str | list[str]] = None
    data_constraints: Optional[str] = None
    required_outputs: Optional[list[str] | str] = None
    en_info: Optional[IntentEnInfo] = None

_JSON_DECODER = json.JSONDecoder()


def _normalize_required_outputs(value):
    if value is None:
        return None

    if isinstance(value, list):
        items = [str(x).strip() for x in value if str(x).strip()]
        return items or None

    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        parts = re.split(r"[；;,\n]+", text)
        items = [x.strip() for x in parts if x.strip()]
        return items or [text]

    return None


def _normalize_json_text(text: str) -> str:
    text = (text or "").strip()
    if not text:
        return text

    # Extract fenced block.
    m = re.search(r"```(?:json|python|py)?\s*(.*?)\s*```", text, flags=re.DOTALL | re.IGNORECASE)
    if m:
        text = m.group(1).strip()

    # Fix invalid \u sequences.
    text = re.sub(r'\\u(?![0-9a-fA-F]{4})', r'\\\\u', text)
    # Fix invalid backslash escapes.
    text = re.sub(r'\\(?!["\\/bfnrtu])', r'\\\\', text)

    return text.strip()

def _extract_first_json_value(text: str) -> Optional[str]:
    if not text:
        return None
    for m in re.finditer(r'[\{\[]', text):
        start = m.start()
        try:
            _, end = _JSON_DECODER.raw_decode(text[start:])
            return text[start:start + end]
        except Exception:
            continue
    return None

def _safe_load_json(text: str, dump_prefix: str = "intent") -> Dict[str, Any]:
    raw = "" if text is None else str(text)

    debug_dir = Path("debug_llm")
    debug_dir.mkdir(parents=True, exist_ok=True)
    dump_path = debug_dir / f"{dump_prefix}_{uuid.uuid4().hex}.txt"
    dump_path.write_text(raw, encoding="utf-8")

    text = _normalize_json_text(raw)
    if not text:
        raise RuntimeError(f"{dump_prefix}: empty LLM output, dumped to {dump_path}")

    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass

    candidate = _extract_first_json_value(text)
    if candidate:
        candidate = _normalize_json_text(candidate)
        obj = json.loads(candidate)
        if isinstance(obj, dict):
            return obj

    raise RuntimeError(
        f"{dump_prefix}: non-JSON or unsupported JSON output, dumped to {dump_path}. "
        f"raw_head={text[:1000]!r}"
    )



def run(pls: PipelineState, llm: ChatLLMClient) -> PipelineState:

    system_prompts = prompt_loader.load("intent/system_prompts.md")
    # system = "You are an OGE platform task semantic parsing expert. Your goal is to convert the user's natural-language description into a normalized, structured spatiotemporal task intent."
    user_prompts = prompt_loader.render(
        "intent/user_prompts.md",
        user_query=pls.user_query,
        user_lang = pls.lang
    )

    try:
        obj_model = llm.invoke_structured(
            system_prompt=system_prompts,
            user_prompt=user_prompts,
            schema=IntentOutput,
        )
        obj = obj_model.model_dump()
    except Exception:
        raw = llm.invoke(system_prompt=system_prompts, user_prompt=user_prompts)
        obj = _safe_load_json(raw, dump_prefix="intent")
    # raw = llm.invoke(system_prompt=system_prompts, user_prompt=user_prompts)
    # obj = _safe_load_json(raw, dump_prefix="intent")

    if not isinstance(obj, dict):
        raise ValueError("intent output is not a JSON object")
    obj["required_outputs"] = _normalize_required_outputs(obj.get("required_outputs"))
    core = {k: obj.get(k, None) for k in CORE_KEYS}

    # intent_json stores only core; en_info is stored separately.
    pls.intent_json = json.dumps(core, ensure_ascii=False)

    if getattr(pls, "lang", "zh") == "en":
        en_info = obj.get("en_info") or {}
        if isinstance(en_info, dict):
            pls.user_query_en = en_info.get("user_query_en")
            pls.user_query = en_info.get("user_query_zh") or pls.user_query
            core_en = {k: en_info.get(k, None) for k in CORE_KEYS}
            pls.en_info["intent_json"] = core_en

    pls.trace["intent"] = pls.intent_json
    return pls
