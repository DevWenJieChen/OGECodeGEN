"""
Startup
Run from the project root, ensuring python -m src.xxx works.
uvicorn src.service:app --host 0.0.0.0 --port 8000 --reload

Health check:
GET http://localhost:8000/health

Chat endpoints:
POST http://localhost:8000/chat/stream

Body example:
{
  "query": "Use Landsat data to calculate NDVI for the Wuhan region.",
  "modules": {
    "code_verify": true
  }
}

The response body contains:
code
verify_ok
verify_report
and debug information: intent_json / data_docs / knowledge_docs / trace.

"""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from typing import Any, Dict, Optional, Callable

import threading
from fastapi import FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool
from starlette.responses import StreamingResponse


from src.tools.config import load_config
from src.core.pipeline_state import PipelineState
from src.tools.model_clients import ChatLLMClient, EmbeddingClient
from src.modules import intent, retrieval_data, retrieval_knowledge, codegen, code_verify
from src.react_modules.react_controller import ReActController
from src.tools.milvus_store import MilvusVectorStore


# -----------------------------
# Global (initialized once)
# -----------------------------
_CFG: Optional[Dict[str, Any]] = None
_LLM: Optional[ChatLLMClient] = None
_EMBEDDER: Optional[EmbeddingClient] = None
_VECTOR_STORE: Optional[MilvusVectorStore] = None


def _init_once() -> None:
    """
    Initialize configuration and clients once for the service process.
    Called during the lifespan startup stage; also supports fallback invocation on the first request in case some deployment modes do not trigger lifespan.
    """
    global _CFG, _LLM, _EMBEDDER, _VECTOR_STORE
    if _CFG is not None:
        return

    cfg = load_config("config.yaml")

    llm_cfg = cfg.get("llm", {})
    llm = ChatLLMClient(
        provider=llm_cfg.get("provider", "aliyun"),
        model=llm_cfg.get("model", "qwen-plus"),
        temperature=float(llm_cfg.get("temperature", 0.2)),
        timeout_s=int(llm_cfg.get("timeout_s", 60)),
        api_key=llm_cfg.get("api_key", ""),
        base_url=llm_cfg.get("base_url"),
    )

    emb_cfg = cfg.get("embedding", {})
    embedder = EmbeddingClient(
        provider=emb_cfg.get("provider"),
        model=emb_cfg.get("model"),
        dimensions=cfg.get("retrieval").get("dim"),
        api_key=emb_cfg.get("api_key", ""),
        base_url=emb_cfg.get("base_url"),
        timeout_s=int(emb_cfg.get("timeout_s", 60)),
    )

    vector_store = MilvusVectorStore(cfg)

    _CFG = cfg
    _LLM = llm
    _EMBEDDER = embedder
    _VECTOR_STORE = vector_store


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ===== Startup stage =====
    _init_once()
    yield
    # ===== Shutdown stage; clean up resources here if needed. =====
    # If connections/resources need to be closed in the future, do it here.


app = FastAPI(
    title="RAG_OGE Service",
    version="0.1.0",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],          # Development: allow all origins.
    allow_credentials=False,      # Must be False when allow_origins="*".
    allow_methods=["*"],
    allow_headers=["*"],
)

# -----------------------------
# Request / Response Models
# -----------------------------
class ChatRequest(BaseModel):
    query: str = Field(..., description="User natural language request"),
    lang: str = Field(default="zh", description="User natural language mode")
    modules: Optional[Dict[str, bool]] = Field(
        default=None,
        description="Optional module switches, e.g. {'intent': true, 'code_verify': false}",
    )


class ChatResponse(BaseModel):
    ok: bool
    user_query: str

    code: Optional[str] = None
    verify_ok: Optional[bool] = None
    verify_report: Optional[str] = None

    intent_json: Optional[str] = None
    data_docs: Optional[Any] = None
    knowledge_docs: Optional[Any] = None
    trace: Optional[Dict[str, Any]] = None

    error: Optional[str] = None


def _get_modules_switch(modules_override: Optional[Dict[str, bool]]) -> Dict[str, bool]:
    """
       Merge module switch configuration.
       """
    if _CFG is None:
        _init_once()
    assert _CFG is not None
    m = dict(_CFG.get("modules", {}))
    if modules_override:
        m.update(modules_override)
    return m

def _sse_event(event: str, data: Any) -> str:
    """
    Construct one SSE message frame in text/event-stream format.
    Parameters:
        event: SSE event name corresponding to the frontend event type, for example:
            "start" / "intent" / "retrieval_data" / "codegen" / "error" / "done"
        data: payload; can be any Python object, including dataclasses, pydantic models, or custom classes.

    Returns:
        s: one SSE-compliant message string ending with "\\n\\n".
    """
    if isinstance(data, str):
        payload = data
    else:
        # Key point: use FastAPI jsonable_encoder to convert complex objects into JSON-compatible structures.
        payload = json.dumps(jsonable_encoder(data), ensure_ascii=False)
    return f"event: {event}\ndata: {payload}\n\n"

async def _run_stage_in_threadpool(
    fn: Callable[[], PipelineState],
) -> PipelineState:
    """
       Execute a synchronous stage in the thread pool to avoid blocking the FastAPI event loop; same rationale as the non-SSE path above.
       """
    return await run_in_threadpool(fn)

def summarize_knowledge_docs(
    docs: list,
    *,
    max_text_len: int = 150
) -> list[dict]:
    """
    Compress a KnowledgeDoc list into a summary format suitable for SSE transmission.
    """
    summaries = []
    for d in docs:
        summaries.append({
            "source": d.source,
            "score": d.score,
            "name": d.name,
            "text_preview": (
                d.text[:max_text_len] + "..."
                if d.text and len(d.text) > max_text_len
                else d.text
            )
        })
    return summaries

async def _stream_pipeline(req: ChatRequest, request: Request):
    """
    Core SSE generator.
    Overall behavior:
      - Phase 1: synchronously execute the linear pipeline order, running synchronous functions in the thread pool:
          RUN_INTENT -> RUN_RETRIEVAL_DATA -> RUN_RETRIEVAL_KNOWLEDGE -> RUN_CODEGEN -> RUN_VERIFY
        After each stage completes, immediately yield one SSE event so the frontend can display intermediate outputs in real time.

      - Phase 2: enter the ReAct repair loop when RUN_VERIFY in Phase 1 fails (verify_ok == False):
          controller.run(...) executes multiple repair attempts in a background thread (Think -> Act -> (auto)Verify),
          and pushes each step_item through the on_step callback.
        This function uses asyncio.Queue as a cross-thread buffer to convert controller step_items into SSE events in real time.
    Parameters:
        req: ChatRequest request body containing:
            - query: user question.
            - modules: optional module switch overrides.
        request: FastAPI Request used to detect whether the client has disconnected.

    SSE events, recommended frontend event names:
      Phase 1:
        - start
        - intent
        - retrieval_data
        - retrieval_knowledge
        - codegen
        - code_verify
      Phase 2 (ReAct):
        - react_start
        - react_step        (callback once for each controller action).
        - react_verify      (callback from controller auto-verify).
        - react_stop        (controller decides STOP).
        - react_done        (verify_ok becomes True).
      Final:
        - done
        - error
    Returns:
      - Yield SSE text frames one by one, constructed by _sse_event(event, data), ending with "\\n\\n".
    """
    if _CFG is None or _LLM is None or _EMBEDDER is None or _VECTOR_STORE is None:
        _init_once()
    assert _CFG is not None and _LLM is not None and _EMBEDDER is not None and _VECTOR_STORE is not None

    query = (req.query or "").strip()
    query_lang = (req.lang or "zh").strip()
    if not query:
        # Provide explicit errors in SSE as well.
        yield _sse_event("error", {"type": "ValidationError", "message": "query must not be empty"})
        return

    m = _get_modules_switch(req.modules)
    pls = PipelineState(user_query=query, user_query_en=query, lang=query_lang)

    # Opening event: let the frontend enter the processing state immediately.
    yield _sse_event("start", {"user_query": query, "modules": m})

    try:
        # -------------------------
        # Phase 1
        # -------------------------
        # intent
        if m.get("intent", True):
            pls.has_modules["intent"] = True
            pls = await _run_stage_in_threadpool(lambda: intent.run(pls, _LLM))
            if await request.is_disconnected():
                return
            if pls.lang == "en":
                yield _sse_event("intent", {"intent_json": pls.en_info["intent_json"]})
            else:
                yield _sse_event("intent", {"intent_json": pls.intent_json})
        else:
            pls.has_modules["intent"] = False

        # retrieval_data
        if m.get("retrieval_data", True):
            pls.has_modules["retrieval_data"] = True
            pls = await _run_stage_in_threadpool(lambda: retrieval_data.run(pls, _LLM, _CFG))
            if await request.is_disconnected():
                return
            if pls.lang == "en":
                yield _sse_event("retrieval_data", {"data_docs": summarize_knowledge_docs(pls.en_info["data_docs"])})
            else:
                yield _sse_event("retrieval_data", {"data_docs": summarize_knowledge_docs(pls.data_docs)})
        else:
            pls.has_modules["retrieval_data"] = False

        # retrieval_knowledge
        if m.get("retrieval_knowledge", True):
            pls.has_modules["retrieval_knowledge"] = True
            pls = await _run_stage_in_threadpool(
                lambda: retrieval_knowledge.run(
                    pls,
                    llm=_LLM,
                    embedder=_EMBEDDER,
                    vector_store=_VECTOR_STORE,
                    cfg=_CFG,
                    scope="both",
                    enable_step_op_search=True,  # Enable on the first pass.
                )
            )
            if await request.is_disconnected():
                return
            yield _sse_event(
            "retrieval_knowledge",
            {
                    "tasks": {
                        "count": len(pls.knowledge_docs.get("tasks", [])),
                        "docs": summarize_knowledge_docs(pls.knowledge_docs.get("tasks", []))
                    },
                    "operators": {
                        "count": len(pls.knowledge_docs.get("operators", [])),
                        "docs": summarize_knowledge_docs(pls.knowledge_docs.get("operators", []))
                    }
                }
            )
        else:
            pls.has_modules["retrieval_knowledge"] = False

        # codegen
        if m.get("codegen", True):
            pls.has_modules["codegen"] = True
            pls = await _run_stage_in_threadpool(lambda: codegen.run(pls, llm=_LLM, mode="fresh"))
            if await request.is_disconnected():
                return
            yield _sse_event("codegen", {"code": pls.code})
        else:
            pls.has_modules["codegen"] = False

        # code_verify
        if m.get("code_verify", True):
            pls.has_modules["code_verify"] = True
            pls = await _run_stage_in_threadpool(lambda: code_verify.run(pls))
            if await request.is_disconnected():
                return
            yield _sse_event("code_verify", {"verify_ok": pls.verify_ok, "verify_report": pls.verify_report})
        else:
            pls.has_modules["code_verify"] = False

        # -------------------------
        # Phase 2: ReAct; enter when Phase 1 fails. Use asyncio for async operations; controller callbacks return each step to the frontend.
        # -------------------------
        if m.get("code_verify", True) and not pls.verify_ok:
            yield _sse_event("react_start", {"message": "enter ReAct repair/recovery loop"})
            cancel_event = threading.Event()
            controller = ReActController(
                llm=_LLM,
                embedder=_EMBEDDER,
                vector_store=_VECTOR_STORE,
                cfg=_CFG,
            )

            # Create an async queue as a message buffer between the controller and SSE, similar to cross-thread communication.
            q: asyncio.Queue = asyncio.Queue(maxsize=100)

            loop = asyncio.get_running_loop() # Captured by the on_step closure.
            def on_step(payload: Dict[str, Any]) -> None:
                # Note: the controller calls this function from a background thread.
                # loop = asyncio.get_event_loop()
                def _put():
                    # drop-oldest: if the queue is full, discard the oldest item first.
                    if q.full():
                        try:
                            q.get_nowait()
                        except asyncio.QueueEmpty:
                            pass
                    try:
                        q.put_nowait(payload)
                    except asyncio.QueueFull:
                        # In theory, it should not be full after the drop above; this is a fallback.
                        pass

                # Safely delegate the enqueue operation to the event loop to avoid thread-safety issues.
                loop.call_soon_threadsafe(_put)

            async def run_controller_in_thread():
                return await _run_stage_in_threadpool(lambda: controller.run(pls, on_step=on_step, cancel_event=cancel_event))

            # Start the background thread.
            controller_task = asyncio.create_task(run_controller_in_thread())

            # Continuously consume queue messages and push them.
            while True:
                if await request.is_disconnected():
                    cancel_event.set()
                    controller_task.cancel()
                    print("request is disconnected!")
                    return

                # If the controller has finished and the queue is empty, exit the loop.
                if controller_task.done() and q.empty():
                    break

                try:
                    item = await asyncio.wait_for(q.get(), timeout=0.5)
                except asyncio.TimeoutError:
                    continue

                # Event types can be further refined here.
                if item.get("type") == "react_step":
                    yield _sse_event("react_step", item)
                elif item.get("type") == "react_auto_verify":
                    yield _sse_event("react_verify", item)
                elif item.get("type") == "react_done":
                    yield _sse_event("react_done", item)
                elif item.get("type") == "react_stop":
                    yield _sse_event("react_stop", item)
                else:
                    yield _sse_event("react", item)

            # Wait for final completion; use try to handle exceptions inside the controller thread more controllably and avoid losing react_step events.
            try:
                pls = await controller_task
            except Exception as e:
                yield _sse_event("error", {"error":"await controller_task error"})
                return

            # After ReAct finishes, emit one final verify event as an optional supplement.
            yield _sse_event("code_verify", {"verify_ok": pls.verify_ok, "verify_report": pls.verify_report})

        # done: send the final full package at once so the frontend can store/render it directly.
        yield _sse_event(
            "done",
            ChatResponse(
                ok=True,
                user_query=query,
                code=pls.code,
                verify_ok=pls.verify_ok,
                verify_report=pls.verify_report,
                intent_json=pls.intent_json,
                # data_docs=pls.data_docs,
                # knowledge_docs=pls.knowledge_docs,
                # trace=pls.trace,
            ).model_dump(),
        )

    except Exception as e:
        yield _sse_event("error", {"type": type(e).__name__, "message": str(e)})

# =========================
# Extensible code sabotage strategies for testing the ReAct correction flow.
# =========================
SabotageFn = Callable[[str, "PipelineState"], str]

def _sabotage_bad_param(code: str, pls: "PipelineState") -> str:
    """
    Parameter-name sabotage: replace productId with productID as an example.
    Used to verify whether ReAct can correct common parameter errors during repair.
    """
    if not code:
        return code
    # Replace according to real common OGE field names.
    if "productID" in code:
        return code.replace("productID", "productId")
    # Fallback: create a bad parameter that is guaranteed to fail.
    return code + "\n\n# SABOTAGE: force bad param\nraise RuntimeError('forced failure: bad_param fallback')\n"

def _sabotage_syntax_error(code: str, pls: "PipelineState") -> str:
    """
    Syntax sabotage: insert an invalid Python snippet.
    Used to verify whether repair can fix syntax with minimal changes.
    """
    return (code or "") + "\n\n# SABOTAGE: syntax error\nif True print('boom')\n"

def _sabotage_runtime_error(code: str, pls: "PipelineState") -> str:
    """
    Runtime sabotage: explicitly raise an exception.
    Used to verify the verify/runtime error path and whether repair can remove it.
    """
    return (code or "") + "\n\n# SABOTAGE: runtime error\nraise RuntimeError('forced failure for react test')\n"

def _sabotage_import_error(code: str, pls: "PipelineState") -> str:
    """
    Dependency sabotage: import a nonexistent package.
    """
    return "import definitely_not_exist_pkg\n\n" + (code or "")

# Extension point: add/modify various error cases here.
SABOTAGE_CASES: Dict[str, SabotageFn] = {
    "bad_param": _sabotage_bad_param,
    "syntax_error": _sabotage_syntax_error,
    "runtime_error": _sabotage_runtime_error,
    "import_error": _sabotage_import_error,
}

def apply_sabotage(code: str, pls: "PipelineState", case: str) -> str:
    fn = SABOTAGE_CASES.get(case)
    if not fn:
        # Unknown cases default to bad_param to avoid a caller typo causing ReAct not to run.
        fn = _sabotage_bad_param
    return fn(code, pls)

# =========================
# Dedicated SSE test endpoint: inject sabotage to force an error in Phase 1.
# =========================

async def _stream_pipeline_test_react(req: "ChatRequest", request: Request, sabotage_case: str):
    """
    Similar to /chat/stream, but:
    - After Phase 1 codegen and before verify, sabotage the code to force Phase 2.
    - sabotage_case is passed through a query parameter to quickly switch among error types.
    """
    if _CFG is None or _LLM is None or _EMBEDDER is None or _VECTOR_STORE is None:
        _init_once()
    assert _CFG is not None and _LLM is not None and _EMBEDDER is not None and _VECTOR_STORE is not None

    query = (req.query or "").strip()
    if not query:
        yield _sse_event("error", {"type": "ValidationError", "message": "query must not be empty"})
        return

    m = _get_modules_switch(req.modules)
    pls = PipelineState(user_query=query)

    yield _sse_event("start", {"user_query": query, "modules": m, "test_mode": True, "sabotage_case": sabotage_case})

    try:
        # -------------------------
        # Phase 1, same as the production endpoint.
        # -------------------------
        if m.get("intent", True):
            pls = await _run_stage_in_threadpool(lambda: intent.run(pls, _LLM))
            if await request.is_disconnected():
                return
            yield _sse_event("intent", {"intent_json": pls.intent_json})

        if m.get("retrieval_data", True):
            pls = await _run_stage_in_threadpool(lambda: retrieval_data.run(pls, _LLM, _CFG))
            if await request.is_disconnected():
                return
            yield _sse_event("retrieval_data", {"data_docs": summarize_knowledge_docs(pls.data_docs)})

        if m.get("retrieval_knowledge", True):
            pls = await _run_stage_in_threadpool(
                lambda: retrieval_knowledge.run(
                    pls,
                    llm=_LLM,
                    embedder=_EMBEDDER,
                    vector_store=_VECTOR_STORE,
                    cfg=_CFG,
                    scope="both",
                    enable_step_op_search=True,
                )
            )
            if await request.is_disconnected():
                return
            yield _sse_event(
                "retrieval_knowledge",
                {
                    "tasks": {
                        "count": len(pls.knowledge_docs.get("tasks", [])),
                        "docs": summarize_knowledge_docs(pls.knowledge_docs.get("tasks", [])),
                    },
                    "operators": {
                        "count": len(pls.knowledge_docs.get("operators", [])),
                        "docs": summarize_knowledge_docs(pls.knowledge_docs.get("operators", [])),
                    },
                },
            )

        if m.get("codegen", True):
            pls = await _run_stage_in_threadpool(lambda: codegen.run(pls, llm=_LLM, mode="fresh"))
            if await request.is_disconnected():
                return
            yield _sse_event("codegen", {"code": pls.code})

        # -------------------------
        # Key step: deliberate sabotage for the test endpoint only.
        # -------------------------
        if m.get("codegen", True):
            original = pls.code or ""
            sabotaged = apply_sabotage(original, pls, sabotage_case)
            pls.code = sabotaged
            yield _sse_event(
                "test_sabotage",
                {
                    "case": sabotage_case,
                    "message": "Inject a controlled failure before verification to force entry into the ReAct stage",
                    "sabotage_code": pls.code
                },
            )

        # verify; this should most likely fail here.
        if m.get("code_verify", True):
            pls = await _run_stage_in_threadpool(lambda: code_verify.run(pls))
            if await request.is_disconnected():
                return
            yield _sse_event("code_verify", {"verify_ok": pls.verify_ok, "verify_report": pls.verify_report})

        # -------------------------
        # Phase 2: ReAct, same as the production endpoint.
        # -------------------------
        if m.get("code_verify", True) and not pls.verify_ok:
            yield _sse_event("react_start", {"message": "enter auto-repair loop"})
            cancel_event = threading.Event()
            controller = ReActController(
                llm=_LLM,
                embedder=_EMBEDDER,
                vector_store=_VECTOR_STORE,
                cfg=_CFG,
            )

            q: asyncio.Queue = asyncio.Queue(maxsize=100)
            loop = asyncio.get_running_loop()

            def on_step(payload: Dict[str, Any]) -> None:
                def _put():
                    if q.full():
                        try:
                            q.get_nowait()
                        except asyncio.QueueEmpty:
                            pass
                    try:
                        q.put_nowait(payload)
                    except asyncio.QueueFull:
                        pass

                loop.call_soon_threadsafe(_put)

            async def run_controller_in_thread():
                return await _run_stage_in_threadpool(
                    lambda: controller.run(pls, on_step=on_step, cancel_event=cancel_event)
                )

            controller_task = asyncio.create_task(run_controller_in_thread())

            while True:
                if await request.is_disconnected():
                    cancel_event.set()
                    controller_task.cancel()
                    return

                if controller_task.done() and q.empty():
                    break

                try:
                    item = await asyncio.wait_for(q.get(), timeout=0.5)
                except asyncio.TimeoutError:
                    continue

                if item.get("type") == "react_step":
                    yield _sse_event("react_step", item)
                elif item.get("type") == "react_auto_verify":
                    yield _sse_event("react_verify", item)
                elif item.get("type") == "react_done":
                    yield _sse_event("react_done", item)
                elif item.get("type") == "react_stop":
                    yield _sse_event("react_stop", item)
                else:
                    yield _sse_event("react", item)

            try:
                pls = await controller_task
            except Exception:
                yield _sse_event("error", {"type": "ControllerError", "message": "await controller_task error"})
                return

            yield _sse_event("code_verify", {"verify_ok": pls.verify_ok, "verify_report": pls.verify_report})

        yield _sse_event(
            "done",
            ChatResponse(
                ok=True,
                user_query=query,
                code=pls.code,
                verify_ok=pls.verify_ok,
                verify_report=pls.verify_report,
                intent_json=pls.intent_json if pls.lang=="zh" else pls.en_info["intent_json"],
            ).model_dump(),
        )

    except Exception as e:
        yield _sse_event("error", {"type": type(e).__name__, "message": str(e)})



@app.get("/health")
def health() -> Dict[str, Any]:
    return {"ok": True, "cwd": os.getcwd()}



@app.post("/chat/stream")
async def chat_stream(req: ChatRequest, request: Request):
    """
    SSE endpoint: stream pipeline stage results as text/event-stream.

    Request parameters (JSON body):
        - query: string, user question.
        - modules: dict[str,bool], optional, overrides config.yaml module switches.

    Returns:
        StreamingResponse (media_type="text/event-stream").
        Clients should consume it as a stream; fetch + ReadableStream is recommended because native EventSource only supports GET.
    """
    return StreamingResponse(
        _stream_pipeline(req, request),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )


# =========================
# FastAPI route: standalone test endpoint.
# =========================

@app.post("/chat/stream/test-react")
async def chat_stream_test_react(
    req: "ChatRequest",
    request: Request,
    sabotage: str = "bad_param",   # Query parameter, e.g. ?sabotage=syntax_error.
):
    """
    Test-only SSE endpoint that forces the auto-repair stage (Phase 2).

    Usage examples:
      POST /chat/stream/test-react?sabotage=bad_param
      POST /chat/stream/test-react?sabotage=syntax_error

    Available sabotage values are the keys of SABOTAGE_CASES.
    """
    return StreamingResponse(
        _stream_pipeline_test_react(req, request, sabotage_case=sabotage),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )

# Optional development-time config hot reload; remove if not needed.
@app.post("/admin/reload_config")
def reload_config() -> Dict[str, Any]:
    global _CFG, _LLM, _EMBEDDER, _VECTOR_STORE
    _CFG = None
    _LLM = None
    _EMBEDDER = None
    _VECTOR_STORE = None
    _init_once()
    return {"ok": True}
