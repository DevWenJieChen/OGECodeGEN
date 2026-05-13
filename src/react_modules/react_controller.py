import json
import threading
from typing import Any, Dict, Optional, Callable, List

from src.core.pipeline_state import PipelineState
from src.tools.model_clients import ChatLLMClient, EmbeddingClient
from src.modules import intent, retrieval_data, retrieval_knowledge, codegen, code_verify
from src.tools.milvus_store import MilvusVectorStore
from src.react_modules.decision_thinker import DecisionThinker, Decision, ALLOWED_ACTIONS

# Action = Literal[
#     "RUN_INTENT",
#     "RUN_RETRIEVAL_DATA",
#     "RUN_RETRIEVAL_KNOWLEDGE",
#     "RUN_CODEGEN",
#     "RUN_VERIFY",
#     "STOP",
# ]
StepCallback = Callable[[Dict[str, Any]], None]

def _parse_verify_report_json(verify_report: Optional[str]) -> Dict[str, Any]:
    """
    Parse pls.verify_report, which is a JSON string for both success and failure cases.
    If parsing fails, return raw content to avoid crashing the controller.
    """
    if not verify_report:
        return {}
    try:
        obj = json.loads(verify_report)
        return obj if isinstance(obj, dict) else {"raw": verify_report[:4000]}
    except Exception:
        return {"raw": verify_report[:4000]}

def _is_verify_ok(pls: PipelineState) -> bool:
    """
    Determine whether verification succeeded:
    - Prefer pls.verify_ok, which is the authoritative pipeline flag.
    - If verify_ok is empty but verify_report is JSON with ok=true, treat it as success as a fallback.
    """
    if pls.verify_ok is True:
        return True
    obj = _parse_verify_report_json(pls.verify_report)
    return bool(obj.get("ok") is True and obj.get("stage") == "ok")


def _error_signature(pls: PipelineState) -> str:
    """
    Generate an error signature from verify_report to detect repeated errors for deduplication and loop prevention.
    Example: runtime:TypeError:Service.getCoverageCollection() got an unexpected keyword argument 'time_range'

    Expected fields after normalizing verify_report JSON:
    - Success: {"ok": true, "stage":"ok", ...}
    - Failure: {"ok": false, "stage":"runtime|syntax|...", "err_type":..., "err_message":...}

    Note:
    - If verify_report parsing fails, fall back to "unknown:raw:<prefix>".
    """
    if _is_verify_ok(pls):
        return "OK"

    obj = _parse_verify_report_json(pls.verify_report)
    stage = obj.get("stage") or "unknown"

    # Structured fields for failure states, as produced by _format_error_json.
    err_type = obj.get("err_type") or "unknown"
    err_msg = (obj.get("err_message") or "").strip()

    if err_msg:
        err_msg = err_msg[:400] # Error messages may be long; useful information is usually near the beginning.
        return f"{stage}:{err_type}:{err_msg}" #

    # Fallback when parsing fails or err_* fields are absent.
    raw = (obj.get("raw") or "")[:400]
    return f"{stage}:raw:{raw}"

def _get_action_params(all_params: Dict[str, Any], action: str) -> Dict[str, Any]:
    # Use _get_action_params for unified adaptation, avoiding repeated structure checks across service/controller; return params[action] directly.
    if not isinstance(all_params, dict):
        return {}
    # New format: params[action] is a dict.
    if action in all_params and isinstance(all_params.get(action), dict):
        return all_params[action]
    # Old format: flattened params.
    return all_params

def _is_cancelled(cancel_event: Optional[threading.Event]) -> bool:
    return bool(cancel_event is not None and cancel_event.is_set())


class ReActController:
    """
    ReAct execution controller: responsible for Observe / Act / Control.
    Think is handled by DecisionThinker (LLM).
    Act corresponds to the five module processes represented by the action space below; Think provides action suggestions, and the Controller handles Observe and Control.

    Action space, consistent with DecisionThinker:
    - RUN_INTENT
    - RUN_RETRIEVAL_DATA
    - RUN_RETRIEVAL_KNOWLEDGE
    - RUN_CODEGEN
    - RUN_VERIFY
    - STOP
    """

    def __init__(
        self,
        llm: ChatLLMClient,
        embedder: EmbeddingClient,
        vector_store: MilvusVectorStore,
        cfg: Dict[str, Any],
    ):
        self.llm = llm
        self.embedder = embedder
        self.vector_store = vector_store
        self.cfg = cfg
        self.controller_cfg=cfg.get("controller", {})
        self.thinker = DecisionThinker(llm)

    def run(self, pls: PipelineState, *,
            on_step: Optional[StepCallback] = None, cancel_event: Optional[threading.Event] = None,) -> PipelineState:
        """
        Execute the ReAct loop until:
        - verification succeeds.
        - the decision outputs STOP.
        - loop-prevention conditions are triggered (max_iters / repeated errors).
        """
        # ---- Initialize trace to avoid KeyError. ----
        pls.trace.setdefault("react", {})
        pls.trace["react"].setdefault("steps", [])
        pls.trace["react"].setdefault("error_counts", {})

        history: List[Dict[str, Any]] = []

        for i in range(1, self.controller_cfg.get("max_iters", 3) + 1):
            # -------------------------
            # Observe: read current state and current error signature.
            # -------------------------

            # For SSE, check cancellation at the beginning of each round.
            if _is_cancelled(cancel_event):
                if on_step:
                    on_step({"type": "react_cancel", "iter": i, "reason": "cancel_event set"})
                return pls

            sig = _error_signature(pls)
            err_counts = pls.trace["react"]["error_counts"]
            err_counts[sig] = err_counts.get(sig, 0) + 1

            # Prevent loops caused by repeated occurrences of the same error.
            if sig != "OK" and err_counts[sig] > self.controller_cfg.get("repeat_error_limit", 2):
                pls.trace["react"]["steps"].append({
                    "iter": i,
                    "decision": {"action": "STOP", "reason": f"repeat_error_limit reached: {sig}"},
                    "status": {"verify_ok": pls.verify_ok, "error_signature": sig},
                })
                return pls

            # Stop immediately on success.
            if _is_verify_ok(pls):
                pls.trace["react"]["steps"].append({
                    "iter": i,
                    "decision": {"action": "STOP", "reason": "verify_ok is true"},
                    "status": {"verify_ok": True, "error_signature": "OK"},
                })
                return pls

            if pls.code:  # Count only when code already exists; this is usually true.
                pls.max_fix_num += 1

            # -------------------------
            # Think: call the LLM to decide the next action.
            # -------------------------
            try:
                decision: Decision = self.thinker.decide(pls, history=history)
                # actions = decision.actions
                # if isinstance(actions, str):
                #     actions = [actions]
            except Exception as e:
                # If decision-making fails, conservatively fall back to the generation -> verification loop.
                decision = Decision(
                    actions=["RUN_CODEGEN"],
                    params={"mode": "repair"} if pls.code else {"mode": "fresh"},
                    reason=f"decision_thinker_failed: {type(e).__name__}: {e}",
                )

            # The controller performs another defensive validation and parameter cleanup pass.
            decision = self._sanitize_decision(decision, pls)
            actions = decision.actions
            explicit_verify_requested = "RUN_VERIFY" in actions

            # -------------------------
            # Act: execute decision.actions one by one.
            # -------------------------
            codegen_happened = False
            for act in actions:
                # Check cancellation before each action for faster response.
                if _is_cancelled(cancel_event):
                    if on_step:
                        on_step({"type": "react_cancel", "fix_iter": i, "reason": "cancel_event set"})
                    return pls

                if act == "STOP":
                    if on_step:
                        on_step({"type": "react_stop", "fix_iter": i, "reason": decision.reason if pls.lang=="zh" else decision.reason_en})
                    return pls
                # before = {
                #     "has_intent": bool(pls.intent_json),
                #     "data_docs_count": len(pls.data_docs or []),
                #     "knowledge_keys": list((pls.knowledge_docs or {}).keys()),
                #     "has_code": bool(pls.code),
                #     "verify_ok": pls.verify_ok,
                #     "error_signature": sig,
                # }

                pls = self._dispatch_action(pls, act, decision.params)
                if act == "RUN_CODEGEN":
                    codegen_happened = True

                # after = {
                #     "has_intent": bool(pls.intent_json),
                #     "data_docs_count": len(pls.data_docs or []),
                #     "knowledge_keys": list((pls.knowledge_docs or {}).keys()),
                #     "has_code": bool(pls.code),
                #     "verify_ok": pls.verify_ok,
                #     "error_signature": _error_signature(pls),
                # }

                step_item = {
                    "type": "react_step",
                    "fix_iter": i,
                    "action": act,
                    "decision_all_action": {"actions": decision.actions},
                    "reason": decision.reason if pls.lang == "zh" else decision.reason_en,
                    # "before_pls": before,
                    # "after_pls": after,
                }

                pls.trace["react"]["steps"].append(step_item)
                history.append(step_item)
                if on_step:
                    on_step(step_item)

                # Auto-verify policy; trigger only when RUN_VERIFY is not explicitly requested.
                if (
                    codegen_happened
                    and not explicit_verify_requested
                    and self.controller_cfg.get("auto_verify_after_codegen", True)
                ):
                    # Check cancellation before auto-verify as well.
                    if _is_cancelled(cancel_event):
                        if on_step:
                            on_step({"type": "react_cancel", "fix_iter": i, "reason": "cancel_event set"})
                        return pls

                    pls = code_verify.run(pls)
                    auto_item = {
                        "type": "react_auto_verify",
                        "fix_iter": i,
                        "verify_ok": pls.verify_ok,
                        "verify_report": pls.verify_report,
                    }
                    pls.trace["react"]["steps"].append(auto_item)
                    history.append(auto_item)
                    if on_step:
                        on_step(auto_item)
                    if _is_verify_ok(pls):
                        if on_step:
                            on_step({"type": "react_done", "fix_iter": i, "verify_ok": True})
                        return pls

        # max_iters reached: stop and return the current state.
        pls.trace["react"]["steps"].append({
            "fix_iter": self.controller_cfg.get("max_iters"),
            "decision": {"action": "STOP", "reason": "max_iters reached"},
            "status": {"verify_ok": pls.verify_ok, "error_signature": _error_signature(pls)},
        })
        return pls

    def _sanitize_decision(self, decision: Decision, pls: PipelineState) -> Decision:
        """
        Defensive handling:
        - Ensure actions are valid.
        - Ensure params retain only allowed fields.
        - Correct actions based on the current state, for example avoid RUN_VERIFY when there is no code.
        """
        actions = decision.actions
        params = dict(decision.params or {})# Make a copy.
        if isinstance(actions, str):
            actions = [actions]
        sanitized_actions = []

        # ALLOWED_ACTIONS = {
        #     "RUN_INTENT",
        #     "RUN_RETRIEVAL_DATA",
        #     "RUN_RETRIEVAL_KNOWLEDGE",
        #     "RUN_CODEGEN",
        #     "RUN_VERIFY",
        #     "STOP",
        # }
        for action in actions:
            # ---- Action whitelist. ----
            if action not in ALLOWED_ACTIONS:
                continue

            # ---- State-based correction. ----
            if action == "RUN_CODEGEN":
                mode = params.get("mode", "fresh")
                if mode == "repair" and not pls.code:
                    params["mode"] = "fresh"

            sanitized_actions.append(action)
        # if not sanitized_actions:
        #     sanitized_actions = ["STOP"]

        return Decision(actions=sanitized_actions, params=params, reason=decision.reason, reason_en=decision.reason_en)

    def _dispatch_action(self, pls: PipelineState, action: str, params: Dict[str, Any]) -> PipelineState:
        """
        Map decisions to concrete module execution (Act).

        Notes:
        - This remains compatible with existing module signatures.
        - If hint/mode or other parameters are later added for codegen/retrieval_knowledge,
          just enable the commented parameters here.
        """
        p = _get_action_params(params, action)
        m = self.cfg.get("../modules", {})

        if action == "RUN_INTENT" and m.get("intent", True):
            return intent.run(pls, self.llm)

        if action == "RUN_RETRIEVAL_DATA" and m.get("retrieval_data", True):
            return retrieval_data.run(pls, self.llm, self.cfg)

        if action == "RUN_RETRIEVAL_KNOWLEDGE" and m.get("retrieval_knowledge", True):
            scope = p.get("scope", "both")
            query_hint = p.get("query_hint")
            top_k = p.get("top_k")

            # retrieval_knowledge.run does not currently implement query_hint/top_k, so do not pass them yet.
            # Enable them after implementation.
            return retrieval_knowledge.run(
                pls,
                llm=self.llm,
                embedder=self.embedder,
                vector_store=self.vector_store,
                cfg=self.cfg,
                scope=scope,
                query_hint=query_hint,
                top_k=top_k,
                enable_step_op_search = False
            )

        if action == "RUN_CODEGEN" and m.get("codegen", True):
            mode = p.get("mode", "fresh")
            # use_knowledge_delta = params.get("use_knowledge_delta", True)
            fix_instruction = p.get("fix_instruction")

            # codegen.run does not currently implement mode/fix_instruction.
            # Enable them after implementation.
            try:
                return codegen.run(
                    pls,
                    llm=self.llm,
                    mode=mode,
                    fix_instruction=fix_instruction,
                    # use_knowledge_delta=use_knowledge_delta,
                )
            except TypeError:
                return codegen.run(pls, llm=self.llm)

        if action == "RUN_VERIFY" and m.get("code_verify", True):
            return code_verify.run(pls)

        # STOP or disabled module: return unchanged.
        return pls


