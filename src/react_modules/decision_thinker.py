from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Dict, Literal, List, Set

from src.core.pipeline_state import PipelineState
from src.tools.model_clients import ChatLLMClient
from src.tools import prompt_loader

Action = Literal[
    "RUN_INTENT",
    "RUN_RETRIEVAL_DATA",
    "RUN_RETRIEVAL_KNOWLEDGE",
    "RUN_CODEGEN",
    "RUN_VERIFY",
    "STOP",
]
# Global action whitelist
ALLOWED_ACTIONS: Set[Action] = {
    "RUN_INTENT",
    "RUN_RETRIEVAL_DATA",
    "RUN_RETRIEVAL_KNOWLEDGE",
    "RUN_CODEGEN",
    "RUN_VERIFY",
    "STOP",
}
# Action mapping
_ACTION_ORDER = [
    ("intent", "RUN_INTENT"),
    ("retrieval_data", "RUN_RETRIEVAL_DATA"),
    ("retrieval_knowledge", "RUN_RETRIEVAL_KNOWLEDGE"),
    ("codegen", "RUN_CODEGEN"),
    ("code_verify", "RUN_VERIFY"),
]

# ActionOrList = Action | List[Action]


@dataclass
class Decision:
    """
    Decision output: which action to execute next, along with its parameters.

    Design notes:
    - action must be in the whitelist; the controller validates it again.
    - params contains only fields allowed for that action; the controller performs defensive handling again.
    """
    actions: List[Action]
    params: Dict[str, Any]
    reason: str = ""
    reason_en: str = ""


def _safe_json_extract(text: str) -> Dict[str, Any]:
    """
    Extract JSON from the model output. The prompt should strongly require generation of one JSON object.
    Because model output can be unstable, allow small amounts of explanatory text before or after it, but it must ultimately contain a parseable JSON object.
    """
    text = (text or "").strip()
    if not text:
        raise ValueError("empty LLM output")

    # Prefer this path if the whole text is JSON.
    try:
        return json.loads(text)
    except Exception:
        pass

    # Fallback: extract the first {...} JSON block from the text.
    m = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if not m:
        raise ValueError(f"no JSON object found in LLM output: {text[:200]}")

    return json.loads(m.group(0))


def _summarize_docs(docs: List[Any], max_text_len: int = 160) -> List[Dict[str, Any]]:
    """
    Summarize List[KnowledgeDoc] to avoid an overly long decision prompt.
    Expected fields: source/text/score; tolerate missing fields where possible.
    """
    out: List[Dict[str, Any]] = []
    for d in docs or []:
        source = getattr(d, "source", None)
        name = getattr(d, "name", None)
        score = getattr(d, "score", None)
        text = getattr(d, "text", None) or ""
        preview = (text[:max_text_len] + "...") if len(text) > max_text_len else text
        out.append({"source": source,"name":name, "score": score, "text_preview": preview})
    return out


def _summarize_state_for_thinking(pls: PipelineState) -> Dict[str, Any]:
    """
    Build a controlled observation summary for the decision maker.
    Goal: provide enough information for decision-making without injecting full knowledge text or long tracebacks.
    """
    # verify_report is expected to be a JSON string; try to parse it here.
    verify_obj = None
    if pls.verify_report:
        try:
            verify_obj = json.loads(pls.verify_report)
        except Exception:
            verify_obj = {"raw": pls.verify_report[:5000]}

    kd = pls.knowledge_docs or {}
    tasks = kd.get("tasks", [])
    ops = kd.get("operators", [])

    error_summary = None
    if not pls.verify_ok and verify_obj:
        error_summary = {
            "stage": verify_obj.get("stage"),
            "err_type": verify_obj.get("err_type"),
            "err_message": verify_obj.get("err_message"),
        }

    obs: Dict[str, Any] = {
        "user_query": pls.user_query,
        "has_intent": bool(pls.intent_json),
        "intent_json": pls.intent_json[:2500] if pls.intent_json else None,

        "data_docs_count": len(pls.data_docs or []),
        "data_docs_summary": _summarize_docs(pls.data_docs or [], max_text_len=140),

        "task_knowledge_count": len(tasks),
        "task_knowledge_summary": _summarize_docs(tasks, max_text_len=140),

        "operator_knowledge_count": len(ops),
        "operator_knowledge_summary": _summarize_docs(ops, max_text_len=140),

        "has_code": bool(pls.code),
        # Code is important for decision-making; provide the full text, or truncate if it is too long.
        "code": pls.code, #if (pls.code and len(pls.code) <= 120000) else (pls.code[:120000] + "\n# ...(truncated)") if pls.code else None,

        "verify_ok": pls.verify_ok,
        "verify_report": verify_obj,
        "error_summary": error_summary
    }
    return obs


def build_action_whitelist(has_modules: Dict[str, bool]) -> List[str]:
    """
    Convert pls.has_modules into an action whitelist in a fixed order, and always append STOP.
    - has_modules must include: intent, retrieval_data, retrieval_knowledge, codegen, code_verify.
    - True -> add the corresponding RUN_* action.
    - False -> skip it.
    Example return value:
      ["RUN_INTENT","RUN_RETRIEVAL_DATA","RUN_CODEGEN","STOP"]
    """
    actions: List[str] = []
    for key, action in _ACTION_ORDER:
        if has_modules.get(key, False):
            actions.append(action)

    # Always allow STOP so the controller can terminate.
    actions.append("STOP")
    return "\n".join(f"- {a}" for a in actions)


class DecisionThinker:
    """
    LLM-based next-action decision maker.

    Typical usage:
        thinker = DecisionThinker(llm)
        decision = thinker.decide(pls, history=[...])

    Return format:
    {
        "actions": ["RUN_RETRIEVAL_KNOWLEDGE", "RUN_CODEGEN", "RUN_VERIFY"],
        "params": {
        "mode": "repair",
        "fix_instruction": "xxx"
        },
        "reason": "..."
    }
    """

    def __init__(self, llm: ChatLLMClient):
        self.llm = llm

    def decide(self, pls: PipelineState, history: List[Dict[str, Any]]) -> Decision:
        """
        Input PipelineState and recent history, then output the next decision.
        """
        obs = _summarize_state_for_thinking(pls)
        # system_prompt = _build_system_prompt()
        # user_prompt = _build_user_prompt(obs, history)
        system_prompt = prompt_loader.render("react_decision/system_prompt.md")
        user_prompt = prompt_loader.render(
            "react_decision/user_prompt.md",
            observation=json.dumps(obs, ensure_ascii=False, indent=2),
            history=json.dumps(history[-6:], ensure_ascii=False, indent=2),
            user_lang=pls.lang,
            allow_actions=build_action_whitelist(pls.has_modules),
        )

        # raw = _call_llm(self.llm, system_prompt=system_prompt, user_prompt=user_prompt)
        llm_out= self.llm.invoke(system_prompt=system_prompt, user_prompt=user_prompt)
        obj = _safe_json_extract(llm_out)

        raw_actions = obj.get("actions")
        # Allow str or list.
        if isinstance(raw_actions, str):
            actions = [raw_actions] # Convert a single action to a list as a fallback.
        elif isinstance(raw_actions, list):
            actions = raw_actions
        else:
            raise ValueError(f"invalid actions type: {type(raw_actions)}")

        for act in actions:
            if act not in ALLOWED_ACTIONS:
                raise ValueError(f"invalid action from LLM: {act}")
        params = obj.get("params") or {}
        reason = obj.get("reason") or ""

        if not isinstance(params, dict):
            params = {}

        if pls.lang == "en":
            # print(obj.get("reason_en",reason))
            # print(obj)
            return Decision(actions=actions, params=params, reason=reason, reason_en=obj.get("reason_en",reason))
        return Decision(actions=actions, params=params, reason=reason)
