from __future__ import annotations

import os
import re
from src.tools.config import load_config
from src.tools import prompt_loader
from src.core.pipeline_state import PipelineState
from src.tools.model_clients import ChatLLMClient, EmbeddingClient
from src.modules import code_verify
from src.tools.milvus_store import MilvusVectorStore
from src.react_modules.react_controller import ReActController

def _normalize_generated_code(raw: str) -> str:
    """
    Normalize the code text returned by the LLM
    """
    if not isinstance(raw, str):
        return raw

    s = raw.strip()
    if not s:
        return s

    # 1) Prefer extracting fenced code block content
    m = re.search(r"```(?:python|py)?\s*\n(.*?)\n```", s, flags=re.DOTALL | re.IGNORECASE)
    if m:
        return m.group(1).strip()

    # 2) When there is no complete block, try to remove leading/trailing fences
    s = re.sub(r"^\s*```[a-zA-Z0-9_+-]*\s*\n", "", s)
    s = re.sub(r"\n\s*```\s*$", "", s)

    return s.strip()



def run_iop(*, user_query: str, query_lang: str, cfg:dict, data_info: str) -> PipelineState:

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

    # 3) Build input context
    pls = PipelineState(user_query=user_query, user_query_en=user_query, lang=query_lang)

    # Get syntax context content (reusing codegen)
    syntax_rules = prompt_loader.load("codegen/oge_syntax.md")
    system_prompt = prompt_loader.render(
        "paper_benchmarks/IOP_prompts/iop_system_prompt.md",
        syntax_rules=syntax_rules,
        language="Python"
    )
    user_prompt = prompt_loader.render(
        "paper_benchmarks/IOP_prompts/iop_user_prompt.md",
        user_query=user_query,
        data_info=data_info,
        user_lang=query_lang
    )
    # print("\n=== CODE GEN ===")
    # code = llm.invoke(user_prompt=user_prompt, system_prompt=system_prompt)
    raw_code = llm.invoke(user_prompt=user_prompt, system_prompt=system_prompt)
    code = _normalize_generated_code(raw_code)
    pls.code = code
    pls = code_verify.run(pls)
    if not pls.verify_ok:
        # print("Repair!")
        debug_num = 0
        max_fix_num = int(cfg.get("max_fix_num", 5))
        while not pls.verify_ok:
            if debug_num >= max_fix_num:
                # print("Repair failed")
                break
            previous_code = pls.code
            verify_report = pls.verify_report

            repair_system_prompt = prompt_loader.render(
                "paper_benchmarks/IOP_prompts/iop_system_prompt_repair.md",
                syntax_rules=syntax_rules
            )
            repair_user_prompt = prompt_loader.render(
                "paper_benchmarks/IOP_prompts/iop_user_prompt_repair.md",
                user_query=pls.user_query,
                previous_code=previous_code,
                data_info=data_info,
                verify_report=verify_report,
                user_lang = query_lang
            )
            pls.code = llm.invoke(user_prompt=repair_user_prompt, system_prompt=repair_system_prompt)
            pls = code_verify.run(pls)
            debug_num+=1
        pls.max_fix_num = debug_num

    return pls


if __name__ == "__main__":
    print(os.getcwd())
    user_query = "请基于指定的时间范围和空间范围获取一批DEM影像，把它们合成一张连续影像并生成地形阴影效果进行展示。"
    query_lang = "zh"
    data_info = "ASTER_GDEM_DEM30 影像集合（时间：2000-01-01 00:00:00；范围：[108.5, 18.1, 111, 20.1]）"
    iop_cfg = load_config("src/paper_benchmarks/configs/config_iop.yaml")
    pls = run_iop(user_query=user_query, query_lang=query_lang,cfg=iop_cfg, data_info=data_info)
    print(pls)
