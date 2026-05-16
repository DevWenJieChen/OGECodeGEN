# main_react.py
from __future__ import annotations

import os
import time

from src.tools.config import load_config
from src.core.pipeline_state import PipelineState
from src.tools.model_clients import ChatLLMClient, EmbeddingClient
from src.modules import intent, retrieval_data, retrieval_knowledge, codegen, code_verify
# from src.modules import intent_debug as intent, retrieval_data, retrieval_knowledge, codegen, code_verify
from src.tools.milvus_store import MilvusVectorStore
from src.react_modules.react_controller import ReActController


def _fmt_secs(sec: float) -> str:
    if sec < 60:
        return f"{sec:.2f}s"
    m = int(sec // 60)
    s = sec - m * 60
    return f"{m}m{s:.0f}s"


def _log_step(case_name: str, step_name: str, t0: float) -> None:
    dt = time.time() - t0
    print(f"[TIMING][{case_name}] {step_name:<22} {_fmt_secs(dt)}")

def run_oge_coder(*, user_query: str, query_lang: str, cfg: dict, data_info: str="") -> PipelineState:
    # Read config
    m = cfg.get("modules", {})

    # Build input context
    pls = PipelineState(user_query=user_query, user_query_en=user_query, lang=query_lang, data_info=data_info)

    # Initialize LLM / Embedding / VectorStore (once)
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


    # =========================
    # Phase 1: linear first pass (aligned with the old main)
    # =========================
    if m.get("intent", True):
        pls.has_modules["intent"]= True
        pls = intent.run(pls, llm)
    else:
        pls.has_modules["intent"]= False
    # print("\n=== INTENT ===")
    # print(pls.intent_json)

    if m.get("retrieval_data", True):
        # If intent exists, retrieve by its data_constraints; otherwise retrieve by user_query
        pls.has_modules["retrieval_data"] = True
        pls = retrieval_data.run(pls, llm, cfg)
    else:
        pls.has_modules["retrieval_data"]= False
    # print("\n=== RETRIEVAL_DATA ===")
    # print(pls.data_docs)

    if m.get("retrieval_knowledge", True):
        # First pass: enable_step_op_search=True (more robust for broad questions)
        pls.has_modules["retrieval_knowledge"] = True
        pls = retrieval_knowledge.run(
            pls,
            llm=llm,
            embedder=embedder,
            vector_store=vector_store,
            cfg=cfg,
            scope="both",
            enable_step_op_search=True,
        )
    else:
        pls.has_modules["retrieval_knowledge"]= False
    # print("\n=== RETRIEVAL_KNOWLEDGE ===")
    # print(pls.knowledge_docs)

    # Although configurable, codegen is logically required; otherwise the pipeline has no meaning
    if m.get("codegen", True):
        pls.has_modules["codegen"] = True
        pls = codegen.run(pls, llm=llm, mode="fresh")
    else:
        pls.has_modules["codegen"]= False
    # print("\n=== CODE ===")
    # print(pls.code or "(empty)")

    if m.get("code_verify", True):
        pls.has_modules["code_verify"] = True
        pls = code_verify.run(pls)
    else:
        pls.has_modules["code_verify"]= False
    # print("\n=== VERIFY (PHASE 1) ===")
    # print(pls.verify_ok, pls.verify_report)

    # =========================
    # Phase 2: ReAct repair loop (entered only when the first pass fails)
    # =========================
    if not pls.verify_ok:
        controller = ReActController(
            llm=llm,
            embedder=embedder,
            vector_store=vector_store,
            cfg=cfg,
        )
        pls = controller.run(pls)
    return pls


def run_oge_coder_2(*, user_query: str, query_lang: str, cfg: dict, data_info: str="") -> PipelineState:
    run_t0 = time.time()
    case_name = (user_query[:40] + "...") if len(user_query) > 40 else user_query

    print(f"\n[TIMING][{case_name}] START")

    # Read config
    t0 = time.time()
    m = cfg.get("modules", {})
    _log_step(case_name, "load_modules_cfg", t0)

    # Build input context
    t0 = time.time()
    pls = PipelineState(user_query=user_query, user_query_en=user_query, lang=query_lang)
    _log_step(case_name, "build_pipeline_state", t0)

    # Initialize LLM / Embedding / VectorStore (once)
    t0 = time.time()
    llm_cfg = cfg.get("llm", {})
    llm = ChatLLMClient(
        provider=llm_cfg.get("provider", "aliyun"),
        model=llm_cfg.get("model", "qwen-plus"),
        temperature=float(llm_cfg.get("temperature", 0.2)),
        timeout_s=int(llm_cfg.get("timeout_s", 60)),
        api_key=llm_cfg.get("api_key", ""),
        base_url=llm_cfg.get("base_url"),
    )
    _log_step(case_name, "init_llm", t0)

    t0 = time.time()
    emb_cfg = cfg.get("embedding", {})
    embedder = EmbeddingClient(
        provider=emb_cfg.get("provider"),
        model=emb_cfg.get("model"),
        dimensions=cfg.get("retrieval").get("dim"),
        api_key=emb_cfg.get("api_key", ""),
        base_url=emb_cfg.get("base_url"),
        timeout_s=int(emb_cfg.get("timeout_s", 60)),
    )
    _log_step(case_name, "init_embedding", t0)

    t0 = time.time()
    vector_store = MilvusVectorStore(cfg)
    _log_step(case_name, "init_vector_store", t0)

    # =========================
    # Phase 1: Note: see the related implementation logic.
    # =========================
    if m.get("intent", True):
        t0 = time.time()
        pls.has_modules["intent"] = True
        pls = intent.run(pls, llm)
        _log_step(case_name, "intent.run", t0)
    else:
        pls.has_modules["intent"] = False
        print(f"[TIMING][{case_name}] intent.run skipped")

    if m.get("retrieval_data", True):
        t0 = time.time()
        pls.has_modules["retrieval_data"] = True
        pls = retrieval_data.run(pls, llm, cfg)
        _log_step(case_name, "retrieval_data.run", t0)
    else:
        pls.has_modules["retrieval_data"] = False
        print(f"[TIMING][{case_name}] retrieval_data.run skipped")

    if m.get("retrieval_knowledge", True):
        t0 = time.time()
        pls.has_modules["retrieval_knowledge"] = True
        pls = retrieval_knowledge.run(
            pls,
            llm=llm,
            embedder=embedder,
            vector_store=vector_store,
            cfg=cfg,
            scope="both",
            enable_step_op_search=True,
        )
        _log_step(case_name, "retrieval_knowledge.run", t0)
    else:
        pls.has_modules["retrieval_knowledge"] = False
        print(f"[TIMING][{case_name}] retrieval_knowledge.run skipped")

    if m.get("codegen", True):
        t0 = time.time()
        pls.has_modules["codegen"] = True
        pls = codegen.run(pls, llm=llm, mode="fresh")
        _log_step(case_name, "codegen.run(fresh)", t0)
    else:
        pls.has_modules["codegen"] = False
        print(f"[TIMING][{case_name}] codegen.run(fresh) skipped")

    if m.get("code_verify", True):
        t0 = time.time()
        pls.has_modules["code_verify"] = True
        pls = code_verify.run(pls)
        _log_step(case_name, "code_verify.run", t0)
    else:
        pls.has_modules["code_verify"] = False
        print(f"[TIMING][{case_name}] code_verify.run skipped")

    print(
        f"[TIMING][{case_name}] phase1_result         "
        f"verify_ok={getattr(pls, 'verify_ok', None)} "
        f"max_fix_num={getattr(pls, 'max_fix_num', None)}"
    )

    # =========================
    # Phase 2: ReAct Note: see the related implementation logic.
    # =========================
    if not pls.verify_ok:
        t0 = time.time()
        controller = ReActController(
            llm=llm,
            embedder=embedder,
            vector_store=vector_store,
            cfg=cfg,
        )
        _log_step(case_name, "init_react_controller", t0)

        t0 = time.time()
        pls = controller.run(pls)
        _log_step(case_name, "ReActController.run", t0)

        print(
            f"[TIMING][{case_name}] react_result          "
            f"verify_ok={getattr(pls, 'verify_ok', None)} "
            f"max_fix_num={getattr(pls, 'max_fix_num', None)}"
        )
    else:
        print(f"[TIMING][{case_name}] ReActController.run skipped")

    print(f"[TIMING][{case_name}] TOTAL                 {_fmt_secs(time.time() - run_t0)}")
    return pls

if __name__ == "__main__":
    # print(os.getcwd())
    user_query = "Use Landsat data to calculate NDWI for the Wuhan area."
    query_lang = "en"
    ours_cfg = load_config("config.yaml")
    woIU_cfg = load_config("src/paper_benchmarks/configs/config_woIU.yaml")
    woKR_cfg = load_config("src/paper_benchmarks/configs/config_woKR.yaml")
    run_oge_coder(user_query=user_query, query_lang=query_lang,cfg=ours_cfg)
