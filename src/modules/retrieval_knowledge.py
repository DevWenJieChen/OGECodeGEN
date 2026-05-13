from typing import List, Optional, Tuple
import json
import re

from src.tools.model_clients import ChatLLMClient, EmbeddingClient
from src.core.pipeline_state import PipelineState, KnowledgeDoc, docs_to_text
from src.tools.milvus_store import MilvusVectorStore
from src.tools.vector_store_base import SearchHit
from src.tools import prompt_loader


# =========================
#   Simple retrieval test
# =========================
def knowledge_search(
    *,
    embedder: EmbeddingClient,
    store: MilvusVectorStore,
    collection_name: str,
    query: str,
    top_k: int,
) -> List[SearchHit]:
    """
    Run a vector retrieval test with a natural-language query and return a list of SearchHit objects.
    Parameters:
        - collection_name: Milvus collection name
        - query: retrieval query, which may come from user_query or query_hint
        - top_k: number of results to return
    """
    q_vec = embedder.embed_query(query)
    hits_batch = store.search(collection_name, [q_vec], top_k=top_k, output_fields=["id", "name", "payload"])
    return hits_batch[0] if hits_batch else []


def _build_task_doc_text(payload: dict) -> str:
    parts = []
    name = payload.get("name")
    domains = payload.get("domains")
    summary = payload.get("summary")
    knowledge = payload.get("knowledge")
    workflow = payload.get("workflow")
    data_req = payload.get("data_and_prerequisites")

    if name:
        parts.append(f"name: {name}")
    if domains:
        if isinstance(domains, list):
            parts.append("domains: " + "；".join(map(str, domains)))
        else:
            parts.append(f"domains: {domains}")
    if summary:
        parts.append(f"summary: {summary}")
    if knowledge:
        parts.append(f"knowledge: {knowledge}")
    if workflow:
        if isinstance(workflow, list):
            parts.append("workflow: " + "；".join(map(str, workflow[:6])))
        else:
            parts.append(f"workflow: {workflow}")
    if data_req:
        parts.append(f"data_and_prerequisites: {data_req}")

    return "\n".join(parts)


def _build_operator_doc_text(payload: dict) -> str:
    parts = []
    for k in ("name", "display_name", "category", "functional_semantic", "details_description"):
        v = payload.get(k)
        if v:
            parts.append(f"{k}: {v}")

    inputs = payload.get("inputs") or []
    outputs = payload.get("outputs") or []
    if inputs:
        parts.append("inputs: " + json.dumps(inputs, ensure_ascii=False))
    if outputs:
        parts.append("outputs: " + json.dumps(outputs, ensure_ascii=False))

    examples = payload.get("examples") or []
    if examples:
        ex = examples[0]
        ex_title = ex.get("title", "")
        ex_desc = ex.get("description", "")
        ex_code = ex.get("code", "")
        parts.append(f"example_title: {ex_title}")
        parts.append(f"example_description: {ex_desc}")
        if ex_code:
            parts.append(f"example_code:\n{ex_code}")

    return "\n".join(parts)


def _hits_to_docs(
    hits: List[SearchHit],
    *,
    source: str,
) -> List[KnowledgeDoc]:
    docs: List[KnowledgeDoc] = []
    is_operator = "operator" in source
    for h in hits:
        payload = h.payload or {}
        text = _build_operator_doc_text(payload) if is_operator else _build_task_doc_text(payload)
        docs.append(
            KnowledgeDoc(
                source=source,
                score=h.score,
                name=payload.get("name"),
                text=text,
            )
        )
    return docs

# =========================
#   LLM task decomposition (steps)
# =========================
def _safe_json_extract_steps(text: str) -> List[str]:
    """
    Extract the steps JSON from the model output.
    Allows a small amount of extra text before or after the JSON to tolerate unstable LLM output,
    but it must still be possible to parse {"steps":[...]}.
    """
    text = (text or "").strip()
    if not text:
        return []

    # Parse directly
    try:
        obj = json.loads(text)
        steps = obj.get("steps")
        if isinstance(steps, list):
            return [str(s).strip() for s in steps if str(s).strip()]
    except Exception:
        pass

    # Extract the JSON block
    m = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if not m:
        return []
    try:
        obj = json.loads(m.group(0))
        steps = obj.get("steps")
        if isinstance(steps, list):
            return [str(s).strip() for s in steps if str(s).strip()]
    except Exception:
        return []

    return []


def _decompose_steps_by_llm(llm: ChatLLMClient, user_query: str,task_knowledge_hint: str = "(empty)",) -> List[str]:
    """
    Call the LLM to decompose user_query into an ordered list of steps for stepwise operator retrieval.
    """
    system_prompt = prompt_loader.load("retrieval_knowledge/step_decompose_system_prompt.md")
    user_prompt = prompt_loader.render(
        "retrieval_knowledge/step_decompose_user_prompt.md",
        user_query=user_query,
        task_knowledge_hint=task_knowledge_hint,
    )

    raw = llm.invoke(user_prompt=user_prompt, system_prompt=system_prompt)
    steps = _safe_json_extract_steps(raw)

    steps = [s for s in steps if s]
    # if len(steps) > 8:
    #     steps = steps[:8]
    return steps


# =========================
#   Merge/deduplication logic
# =========================
def _dedupe_operators(docs: List[KnowledgeDoc]) -> List[KnowledgeDoc]:
    """
    Deduplicate operators by unique name.
    If name is missing, fall back to the text hash.
    """
    seen: set[str] = set()
    out: List[KnowledgeDoc] = []
    for d in docs or []:
        name = getattr(d, "name", None)
        key = f"name:{name}" if name else f"text:{hash(getattr(d, 'text', ''))}"
        if key in seen:
            continue
        seen.add(key)
        out.append(d)
    return out

def _dedupe_tasks(docs: List[KnowledgeDoc]) -> List[KnowledgeDoc]:
    """
    Deduplicate tasks. Task knowledge usually has no stable unique ID, so source + text hash is used.
    """
    seen: set[str] = set()
    out: List[KnowledgeDoc] = []
    for d in docs or []:
        src = getattr(d, "source", None)
        text = getattr(d, "text", "") or ""
        key = f"{src}:{hash(text)}"
        if key in seen:
            continue
        seen.add(key)
        out.append(d)
    return out


def _merge_docs(
    old_docs: List[KnowledgeDoc],
    new_docs: List[KnowledgeDoc],
    *,
    kind: str,
) -> Tuple[List[KnowledgeDoc], List[KnowledgeDoc]]:
    """
    Merge by appending and return:
    - merged: the full set after merge and deduplication
    - delta_added: newly added items in this run after deduplication against old
    """
    old_docs = old_docs or []
    new_docs = new_docs or []

    if kind == "operators":
        # Build the key set for old docs first
        old_keys = set()
        for d in old_docs:
            name = getattr(d, "name", None)
            key = f"name:{name}" if name else f"text:{hash(getattr(d, 'text', ''))}"
            old_keys.add(key)

        delta = []
        for d in new_docs:
            name = getattr(d, "name", None)
            key = f"name:{name}" if name else f"text:{hash(getattr(d, 'text', ''))}"
            if key not in old_keys:
                delta.append(d)

        merged = _dedupe_operators(old_docs + new_docs)
        delta = _dedupe_operators(delta)
        return merged, delta

    # tasks
    old_keys = set()
    for d in old_docs:
        src = getattr(d, "source", None)
        text = getattr(d, "text", "") or ""
        old_keys.add(f"{src}:{hash(text)}")

    delta = []
    for d in new_docs:
        src = getattr(d, "source", None)
        text = getattr(d, "text", "") or ""
        key = f"{src}:{hash(text)}"
        if key not in old_keys:
            delta.append(d)

    merged = _dedupe_tasks(old_docs + new_docs)
    delta = _dedupe_tasks(delta)
    return merged, delta



# ======================
# Main entry point
# ======================
def run(pls: PipelineState,
        *,
        llm: ChatLLMClient,
        embedder: EmbeddingClient,
        vector_store: MilvusVectorStore,
        cfg: dict,
        scope: str = "both",                 # "operators" | "tasks" | "both"
        query_hint: Optional[str] = None,    # Used for retrieval in the supplementary stage
        top_k: Optional[int] = None,         # Override the default top_k from config
        enable_step_op_search: bool = False,  # Recommended as True only for the initial flow and False in the repair stage
) -> PipelineState:
    """
    Knowledge retrieval module. Supports supplementary retrieval during the ReAct repair stage.

    Input parameters:
    - scope:
        "operators" -> retrieve operator knowledge only
        "tasks"     -> retrieve task knowledge only
        "both"      -> retrieve both types of knowledge by default
    - query_hint:
        Supplementary retrieval hint, such as a missing operator, a function name in an error,
        or a Chinese concept. If empty, falls back to pls.user_query.
    - top_k:
        Override cfg.retrieval.top_k. If empty, use the default value from config.
    - enable_step_op_search:
        True  -> decompose the task into steps first, then retrieve operators step by step and merge with deduplication
        False -> retrieve operators with a single query, using query_hint or user_query

    Outputs written to state:
    - pls.knowledge_docs: merged by appending by default; old content is not overwritten
    - pls.trace["react"]["last_delta"]: records only the newly added docs in this run for the delta section in codegen repair
    - pls.trace["retrieval_knowledge"]: records retrieval information for this run
    """
    pls.trace.setdefault("retrieval_knowledge", {})
    pls.trace.setdefault("react", {})

    milvus_cfg = cfg.get("milvus", {})
    retrieval_cfg = cfg.get("retrieval", {})
    op_collection_name = milvus_cfg.get("collection_operators")
    task_collection_name = milvus_cfg.get("collection_tasks")

    cfg_top_k = int(retrieval_cfg.get("top_k", 5))
    use_top_k = cfg_top_k
    if top_k is not None:
        try:
            use_top_k = max(1, int(top_k))
        except Exception:
            use_top_k = cfg_top_k

    # Retrieval query. During experiments, using the original user question is the most stable option.
    query_text = (query_hint or "").strip() or pls.user_query
    # Normalize scope
    scope = (scope or "both").strip().lower()
    if scope not in ("operators", "tasks", "both"):
        scope = "both"

    # Read the existing full working set with merge semantics
    if not isinstance(pls.knowledge_docs, dict):
        pls.knowledge_docs = {}
    old_ops = pls.knowledge_docs.get("operators") or []
    old_tasks = pls.knowledge_docs.get("tasks") or []

    # Delta added in this run; records only the newly added batch
    delta_ops: List[KnowledgeDoc] = []
    delta_tasks: List[KnowledgeDoc] = []

    # Task knowledge retrieval
    task_docs_new: List[KnowledgeDoc] = []
    if scope in ("tasks", "both"):
        # Task knowledge is usually smaller: use half of use_top_k by default, with a minimum of 1
        task_k = max(1, int(use_top_k / 2))
        task_hits = knowledge_search(
            embedder=embedder,
            store=vector_store,
            collection_name=task_collection_name,
            query=query_text,
            top_k=task_k,
        )
        task_docs_new = _hits_to_docs(task_hits, source=f"milvus:{task_collection_name}")

        merged_tasks, delta_tasks = _merge_docs(old_tasks, task_docs_new, kind="tasks")
        pls.knowledge_docs["tasks"] = merged_tasks

    # Operator knowledge retrieval
    operator_docs_new: List[KnowledgeDoc] = []
    steps = []
    if scope in ("operators", "both"):
        if enable_step_op_search:
            # Enable only for broad initial descriptions: decompose into steps first, then retrieve by step
            task_docs_for_hint = task_docs_new or (pls.knowledge_docs.get("tasks") or [])
            task_knowledge_hint = docs_to_text(task_docs_for_hint, max_chars=80000)
            steps = _decompose_steps_by_llm(llm, pls.user_query,task_knowledge_hint=task_knowledge_hint)
            # If decomposition fails, fall back to a single query
            step_queries = steps if steps else [query_text]
            pls.task_steps = step_queries

            collected: List[KnowledgeDoc] = []
            per_step_k = max(2, min(4, int(use_top_k / 2) if use_top_k > 1 else 1))
            for step_q in step_queries:
                op_hits = knowledge_search(
                    embedder=embedder,
                    store=vector_store,
                    collection_name=op_collection_name,
                    query=step_q,
                    top_k=per_step_k,
                )
                collected.extend(_hits_to_docs(op_hits, source=f"milvus:{op_collection_name}"))

            # Merge and deduplicate to obtain the new retrieval results for this run
            operator_docs_new = _dedupe_operators(collected)
        else:
            op_hits = knowledge_search(
                embedder=embedder,
                store=vector_store,
                collection_name=op_collection_name,
                query=query_text,
                top_k=use_top_k,
            )
            operator_docs_new = _hits_to_docs(op_hits, source=f"milvus:{op_collection_name}")

        merged_ops, delta_ops = _merge_docs(old_ops, operator_docs_new, kind="operators")
        pls.knowledge_docs["operators"] = merged_ops


    # last_delta records the newly added docs in this run
    pls.trace["react"]["last_delta"] = {
        "scope": scope,
        "hint": query_text,
        "enable_stepwise_operator_search": bool(enable_step_op_search),
        "steps": steps,  # Kept for traceability; not used by codegen and optional to retain
        "operators": delta_ops if scope in ("operators", "both") else [],
        "tasks": delta_tasks if scope in ("tasks", "both") else [],
    }

    # Trace records for this retrieval run
    pls.trace["retrieval_knowledge"] = {
        "query_used": "query_hint" if (query_hint and query_hint.strip()) else "user_query",
        "query_text": query_text,
        "scope": scope,
        "top_k": use_top_k,
        "enable_stepwise_operator_search": bool(enable_step_op_search),
        "steps_count": len(steps),
        "steps_preview": steps,
        "operator_hits_new": len(operator_docs_new),
        "task_hits_new": len(task_docs_new),
        "operator_delta_added": len(delta_ops),
        "task_delta_added": len(delta_tasks),
        "operator_total": len(pls.knowledge_docs.get("operators") or []),
        "task_total": len(pls.knowledge_docs.get("tasks") or []),
    }
    return pls