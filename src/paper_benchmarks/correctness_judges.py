from __future__ import annotations

import ast
import json
import time
from dataclasses import dataclass
from typing import Literal
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple
from datetime import datetime
import math

from pydantic import BaseModel, Field

from src.tools.prompt_loader import render  # Your prompts/render function
from src.tools.config import load_config
# from src.clients.llm_client import ChatLLMClient
from src.tools.model_clients import ChatLLMClient


# =========================
# Configuration section
# =========================
RUN_DIR = Path("benchmarks_results/20260414_182231_gemma-4-31b-it")  # TODO
EXPERIMENTS = ["OURS", "woIU", "woKR", "IOP"]         # TODO
OUT_DIRNAME = "correctness_judgements"

# Prompt paths (place them under prompts/judge/ yourself)
PROMPT_SYSTEM = "paper_benchmarks/judge_prompts/system_prompt.md"
PROMPT_USER_SEMANTIC = "paper_benchmarks/judge_prompts/user_prompt_semantic.md"
PROMPT_USER_FULFILLMENT = "paper_benchmarks/judge_prompts/user_prompt_fulfillment.md"
PROMPT_USER_PARAM = "paper_benchmarks/judge_prompts/user_prompt_param.md"

# Batch parameters (cost-saving + faster)
BATCH_SIZE = 10
MAX_CONCURRENCY = 5
# TIMEOUT_S = 60

# Failure retry policy (avoid unbounded cost)
RETRY_ON_FAIL = 1
SLEEP_BETWEEN_RETRY_S = 1.0


class SemanticJudgeResult(BaseModel):
    judge_type: Literal["semantic"] = "semantic"
    data_adherence: float = Field(ge=0, le=10)
    semantic_faithfulness: float = Field(ge=0, le=10)
    confidence: float = Field(ge=0, le=1)
    major_issues: List[str] = Field(max_length=5)
    rationale_brief: str = Field(max_length=120)


class FulfillmentJudgeResult(BaseModel):
    judge_type: Literal["fulfillment"] = "fulfillment"
    task_fulfillment: float = Field(ge=0, le=10)
    output_quality: float = Field(ge=0, le=10)
    confidence: float = Field(ge=0, le=1)
    major_issues: List[str] = Field(max_length=5)
    rationale_brief: str = Field(max_length=120)


class ParamJudgeResult(BaseModel):
    judge_type: Literal["param"] = "param"
    parameter_validity: float = Field(ge=0, le=10)
    result_plausibility: float = Field(ge=0, le=10)
    confidence: float = Field(ge=0, le=1)
    major_issues: List[str] = Field(max_length=5)
    rationale_brief: str = Field(max_length=120)


@dataclass
class JudgeConfig:
    judge_id: str
    client: ChatLLMClient
    judge_style: str
    judge_type: str
    schema: Any
    prompt_user: str
    strict: bool = True

# =========================
# Utility functions
# =========================
def parse_maybe_json_or_pyobj(x: Any) -> Any:
    if x is None:
        return None
    if isinstance(x, (dict, list)):
        return x
    if not isinstance(x, str):
        return None
    s = x.strip()
    if not s:
        return None
    try:
        return json.loads(s)
    except Exception:
        pass
    try:
        return ast.literal_eval(s)
    except Exception:
        return None


def iter_case_records(run_dir: Path, exp_names: Sequence[str]) -> Iterable[Tuple[str, Path, Dict[str, Any]]]:
    """
    yield: (exp_name, rec_path, rec_dict)
    """
    for exp_name in exp_names:
        exp_dir = run_dir / exp_name
        if not exp_dir.is_dir():
            continue
        idx_path = exp_dir / "index.json"
        if not idx_path.is_file():
            continue

        idx = json.loads(idx_path.read_text(encoding="utf-8"))
        items = idx.get("items", []) or []
        for it in items:
            case_id = it.get("case_id")
            if not case_id:
                continue
            rec_path = exp_dir / f"{case_id}.json"
            if not rec_path.is_file():
                continue
            rec = json.loads(rec_path.read_text(encoding="utf-8"))
            yield exp_name, rec_path, rec


def load_done_keys(jsonl_path: Path) -> Set[str]:
    """
    Resume support: scan the existing jsonl file and collect completed keys.
    Key format: "{experiment}::{case_id}".

    """
    done: Set[str] = set()
    if not jsonl_path.is_file():
        return done
    with jsonl_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            exp = obj.get("experiment")
            cid = obj.get("case_id")
            if exp and cid:
                done.add(f"{exp}::{cid}")
    return done


def safe_write_jsonl(path: Path, obj: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")

def _now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _fmt_seconds(sec: float) -> str:
    sec = int(sec)
    h = sec // 3600
    m = (sec % 3600) // 60
    s = sec % 60
    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def _log(msg: str) -> None:
    print(f"[{_now_str()}] {msg}", flush=True)

def _compact_text(x: Any) -> str:
    if x is None:
        return ""
    if isinstance(x, str):
        return x.strip()
    try:
        return json.dumps(x, ensure_ascii=False, indent=2)
    except Exception:
        return str(x).strip()


def build_user_prompt(rec: Dict[str, Any], judge: JudgeConfig) -> str:
    case = rec.get("case", {}) or {}
    result = rec.get("result", {}) or {}

    desc = (case.get("description") or "").strip()
    data_ref = _compact_text(case.get("data_ref"))
    gold_code = (case.get("target_code") or "").strip()

    pred_code = (result.get("code") or "").strip()
    verify_ok = result.get("verify_ok")
    executability_ok = result.get("executability_ok")
    dag_json_state = result.get("dag_json_state")
    verify_report = _compact_text(result.get("verify_report"))

    return render(
        judge.prompt_user,
        judge_style=judge.judge_style,
        description=desc,
        data_ref=data_ref,
        verify_ok=str(verify_ok),
        executability_ok=str(executability_ok),
        dag_json_state=str(dag_json_state),
        verify_report=verify_report,
        gold_code=gold_code,
        pred_code=pred_code,
    )


def aggregate_judgements(per_judge_results: Dict[str, Any]) -> Dict[str, Any]:
    subs: Dict[str, float] = {}

    semantic = per_judge_results.get("semantic")
    if semantic is not None:
        subs["data_adherence"] = float(semantic.data_adherence)
        subs["semantic_faithfulness"] = float(semantic.semantic_faithfulness)

    fulfillment = per_judge_results.get("fulfillment")
    if fulfillment is not None:
        subs["task_fulfillment"] = float(fulfillment.task_fulfillment)
        subs["output_quality"] = float(fulfillment.output_quality)

    param = per_judge_results.get("param")
    if param is not None:
        subs["parameter_validity"] = float(param.parameter_validity)
        subs["result_plausibility"] = float(param.result_plausibility)

    expected_dims = {
        "task_fulfillment",
        "data_adherence",
        "semantic_faithfulness",
        "parameter_validity",
        "output_quality",
        "result_plausibility",
    }

    if set(subs.keys()) != expected_dims:
        return {
            "error": "incomplete_judgements",
            "subscores_partial": subs,
            "missing_dimensions": sorted(expected_dims - set(subs.keys())),
        }

    overall = compute_overall(subs)

    confidence_values: List[float] = []
    merged_issues: List[str] = []
    seen: Set[str] = set()

    for jr in per_judge_results.values():
        confidence_values.append(float(jr.confidence))
        for it in jr.major_issues:
            if len(merged_issues) >= 8:
                break
            s = (it or "").strip()
            if s and s not in seen:
                seen.add(s)
                merged_issues.append(s)
        if len(merged_issues) >= 8:
            break

    return {
        "score_overall": overall,
        "confidence_mean": sum(confidence_values) / len(confidence_values) if confidence_values else 0.0,
        "subscores": subs,
        "major_issues_merged": merged_issues,
    }

def compute_overall(subscores: Dict[str, float]) -> float:
    """
        Overall weights:
        - task_fulfillment: 0.20
        - data_adherence: 0.15
        - semantic_faithfulness: 0.20
        - parameter_validity: 0.20
        - output_quality: 0.15
        - result_plausibility: 0.10

    """
    return (
        0.20 * subscores["task_fulfillment"] +
        0.15 * subscores["data_adherence"] +
        0.20 * subscores["semantic_faithfulness"] +
        0.20 * subscores["parameter_validity"] +
        0.15 * subscores["output_quality"] +
        0.10 * subscores["result_plausibility"]
    )


# =========================
# Main flow: judge scoring (batching + resume)
# =========================
def judge_one_batch(
    judge: JudgeConfig,
    exp_name: str,
    case_ids: List[str],
    recs: List[Dict[str, Any]],
    system_prompt: str,
) -> Tuple[List[Optional[Any]], List[Optional[str]]]:
    """
    Return:
    - results: one JudgeResult or None per case
    - errors: one error message or None per case

    """
    user_prompts = [build_user_prompt(r, judge) for r in recs]

    batch_start = time.time()

    # Try batch first (cheapest / fastest)
    for attempt in range(RETRY_ON_FAIL + 1):
        try:
            out = judge.client.invoke_structured_batch(
                user_prompts,
                system_prompt=system_prompt,
                schema=judge.schema,
                max_concurrency=MAX_CONCURRENCY,
                strict=judge.strict,
            )

            if len(out) != len(recs):
                raise RuntimeError(f"batch size mismatch: got={len(out)} expect={len(recs)}")

            return out, [None] * len(out)

        except Exception as e:
            _log(
                f"[BATCH-ERROR] judge={judge.judge_id} "
                f"attempt={attempt + 1}/{RETRY_ON_FAIL + 1} "
                f"error={type(e).__name__}: {e}"
            )

            if attempt < RETRY_ON_FAIL:
                time.sleep(SLEEP_BETWEEN_RETRY_S)
                continue

            _log(f"[BATCH-FALLBACK] judge={judge.judge_id} -> invoke_structured one by one")

            results: List[Optional[Any]] = []
            errors: List[Optional[str]] = []
            single_fail = 0

            for idx, up in enumerate(user_prompts, start=1):
                try:
                    jr = judge.client.invoke_structured(
                        up,
                        system_prompt=system_prompt,
                        schema=judge.schema,
                        strict=judge.strict,
                    )
                    results.append(jr)
                    errors.append(None)
                except Exception as ee:
                    results.append(None)
                    err_msg = f"{type(ee).__name__}: {ee}"
                    errors.append(err_msg)
                    single_fail += 1
                    _log(
                        f"[SINGLE-ERROR] judge={judge.judge_id} "
                        f"idx={idx}/{len(user_prompts)} "
                        f"case_id={case_ids[idx - 1]} "
                        f"error={err_msg}"
                    )

            elapsed = time.time() - batch_start
            _log(
                f"[BATCH-FALLBACK-DONE] judge={judge.judge_id} "
                f"size={len(recs)} single_fail={single_fail} "
                f"elapsed={_fmt_seconds(elapsed)}"
            )
            return results, errors

    return [None] * len(recs), ["unknown_error"] * len(recs)


def main() -> None:
    out_dir = RUN_DIR / OUT_DIRNAME
    out_dir.mkdir(parents=True, exist_ok=True)

    system_prompt = render(PROMPT_SYSTEM)

    cfg = load_config("src/paper_benchmarks/configs/config_judges.yaml")
    # 2) Initialize LLM / Embedding / VectorStore (once)
    llm_cfg = cfg.get("llm", {})
    provider=llm_cfg.get("provider")
    model=llm_cfg.get("model")
    temperature=float(llm_cfg.get("temperature"))
    timeout_s=int(llm_cfg.get("timeout_s"))
    api_key=llm_cfg.get("api_key")
    base_url=llm_cfg.get("base_url")

    # -------------------------
    # Configure 3-5 judges (example)
    # -------------------------
    # Set different judge_style values for judges to enable multi-perspective voting
    # TODO: Inject api_key/base_url according to your environment (environment variables or config files both work)
    JUDGES: List[JudgeConfig] = [
        JudgeConfig(
            judge_id="j1_semantic",
            client=ChatLLMClient(provider=provider, model=model, temperature=temperature, timeout_s=timeout_s,
                                 api_key=api_key, base_url=base_url),
            judge_style="重视任务语义与数据角色一致性：重点审查任务是否被改成另一个问题，数据角色、因子角色、输出角色是否正确。",
            judge_type="semantic",
            schema=SemanticJudgeResult,
            prompt_user=PROMPT_USER_SEMANTIC,
            strict=True,
        ),
        JudgeConfig(
            judge_id="j2_fulfillment",
            client=ChatLLMClient(provider=provider, model=model, temperature=temperature, timeout_s=timeout_s,
                                 api_key=api_key, base_url=base_url),
            judge_style="重视任务完成度与输出闭环：重点审查是否真正完成用户要求，是否产生用户可用的最终结果。",
            judge_type="fulfillment",
            schema=FulfillmentJudgeResult,
            prompt_user=PROMPT_USER_FULFILLMENT,
            strict=True,
        ),
        JudgeConfig(
            judge_id="j3_param_exec",
            client=ChatLLMClient(provider=provider, model=model, temperature=temperature, timeout_s=timeout_s,
                                 api_key=api_key, base_url=base_url),
            judge_style="重视关键参数合理性与真实可执行性：重点审查 band、公式、产品层级、scale/offset、阈值、窗口、时相，以及伪验证问题。",
            judge_type="param",
            schema=ParamJudgeResult,
            prompt_user=PROMPT_USER_PARAM,
            strict=True,
        ),
    ]

    # Cache file for each judge
    judge_jsonl: Dict[str, Path] = {j.judge_id: (out_dir / f"{j.judge_id}.jsonl") for j in JUDGES}
    judge_done: Dict[str, Set[str]] = {j.judge_id: load_done_keys(judge_jsonl[j.judge_id]) for j in JUDGES}

    # Aggregated output (one line per case, including summaries from multiple judges)
    summary_path = out_dir / "summary_by_case.jsonl"
    # summary_done = load_done_keys(summary_path)



    # For clarity, this script is implemented in two stages:
    # A) For each judge, scan all cases, collect missing cases -> batch judging
    # B) Scan again to aggregate the summary (only completed items are aligned)
    # --------
    # Stage A: batch-fill missing cases for each judge
    # --------
    all_records: List[Tuple[str, str, Dict[str, Any]]] = []
    for exp_name, rec_path, rec in iter_case_records(RUN_DIR, EXPERIMENTS):
        case = rec.get("case", {}) or {}
        case_id = (case.get("case_id") or rec_path.stem).strip()
        all_records.append((exp_name, case_id, rec))

    # print(f"[INFO] total records loaded: {len(all_records)}")
    _log(f"[STAGE] load all records")
    _log(f"[INFO] total_records={len(all_records)} experiments={EXPERIMENTS}")

    for judge in JUDGES:
        out_path = judge_jsonl[judge.judge_id]
        done = judge_done[judge.judge_id]

        pending: List[Tuple[str, str, Dict[str, Any]]] = []
        for exp_name, case_id, rec in all_records:
            key = f"{exp_name}::{case_id}"
            if key in done:
                continue
            # If pred_code is missing, write an error directly (avoid wasting tokens)
            pred_code = ((rec.get("result", {}) or {}).get("code") or "").strip()
            if not pred_code:
                safe_write_jsonl(out_path, {
                    "experiment": exp_name,
                    "case_id": case_id,
                    "ok": False,
                    "error": "no_pred_code",
                })
                done.add(key)
                continue
            pending.append((exp_name, case_id, rec))

        # print(f"[INFO] judge={judge.judge_id} pending={len(pending)} (done={len(done)})")
        total_cases = len(all_records)
        done_count = len(done)
        pending_count = len(pending)
        total_chunks = math.ceil(pending_count / BATCH_SIZE) if pending_count > 0 else 0

        _log(
            f"[JUDGE-START] judge={judge.judge_id} "
            f"done={done_count}/{total_cases} "
            f"pending={pending_count} "
            f"chunks={total_chunks}"
        )

        # Split into batches
        i = 0
        while i < len(pending):
            # chunk = pending[i:i + BATCH_SIZE]
            # i += BATCH_SIZE
            chunk_start_time = time.time()
            chunk = pending[i:i + BATCH_SIZE]
            chunk_idx = i // BATCH_SIZE + 1
            i += BATCH_SIZE

            exp0 = chunk[0][0]
            # Note: experiments within a chunk may differ; for simplicity, this is allowed here
            # (because experiment is written with each record)
            case_ids = [x[1] for x in chunk]
            recs = [x[2] for x in chunk]
            exps = [x[0] for x in chunk]

            results, errors = judge_one_batch(judge, exp0, case_ids, recs, system_prompt)

            for exp_name, case_id, jr, err in zip(exps, case_ids, results, errors):
                key = f"{exp_name}::{case_id}"
                if jr is not None:
                    safe_write_jsonl(out_path, {
                        "experiment": exp_name,
                        "case_id": case_id,
                        "ok": True,
                        "judge_id": judge.judge_id,
                        "model": judge.client.model,
                        "result": jr.model_dump(),
                    })
                else:
                    safe_write_jsonl(out_path, {
                        "experiment": exp_name,
                        "case_id": case_id,
                        "ok": False,
                        "judge_id": judge.judge_id,
                        "model": judge.client.model,
                        "error": err or "unknown_error",
                    })
                done.add(key)

            # print(f"[PROGRESS] judge={judge.judge_id} wrote batch size={len(chunk)}")
            ok_count = sum(1 for x in results if x is not None)
            fail_count = len(results) - ok_count
            elapsed = time.time() - chunk_start_time
            finished_count = min(chunk_idx * BATCH_SIZE, pending_count)

            if chunk_idx == 1 or chunk_idx % 10 == 0 or chunk_idx == total_chunks or fail_count > 0:
                pct = (finished_count / pending_count * 100) if pending_count > 0 else 100.0
                _log(
                    f"[PROGRESS] judge={judge.judge_id} "
                    f"{finished_count}/{pending_count} "
                    f"({pct:.1f}%) "
                    f"fail={fail_count}"
                )

        _log(
            f"[JUDGE-DONE] judge={judge.judge_id} "
            f"pending_processed={pending_count} "
            f"chunks={total_chunks}"
        )

    # --------
    # Stage B: aggregate summary (merge multiple judge results by case)
    # --------
    # For simplicity and robustness, read each judge jsonl once and build an index
    _log("[STAGE] rebuild summary index")
    if summary_path.exists():
        summary_path.unlink()
    judge_map: Dict[str, Dict[str, Any]] = {j.judge_id: {} for j in JUDGES}
    judge_err: Dict[str, Dict[str, str]] = {j.judge_id: {} for j in JUDGES}

    for j in JUDGES:
        p = judge_jsonl[j.judge_id]
        if not p.is_file():
            continue
        _log(f"[SUMMARY-LOAD] judge={j.judge_id}")
        with p.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                exp = obj.get("experiment")
                cid = obj.get("case_id")
                if not exp or not cid:
                    continue
                key = f"{exp}::{cid}"
                if obj.get("ok") is True and isinstance(obj.get("result"), dict):
                    try:
                        judge_map[j.judge_id][key] = j.schema.model_validate(obj["result"])
                    except Exception as e:
                        judge_err[j.judge_id][key] = f"validate_error: {type(e).__name__}: {e}"
                elif obj.get("ok") is False:
                    judge_err[j.judge_id][key] = obj.get("error") or "error"

    summary_done = load_done_keys(summary_path)
    wrote = 0

    for exp_name, case_id, rec in all_records:
        key = f"{exp_name}::{case_id}"

        # Collect available judge results
        per_judge_results: Dict[str, Any] = {}
        per_judge: Dict[str, Any] = {}

        for j in JUDGES:
            jr = judge_map[j.judge_id].get(key)
            if jr is not None:
                per_judge_results[j.judge_type] = jr
                per_judge[j.judge_id] = {
                    "ok": True,
                    "judge_type": j.judge_type,
                    "confidence": jr.confidence,
                    "result": jr.model_dump(),
                }
            else:
                per_judge[j.judge_id] = {
                    "ok": False,
                    "judge_type": j.judge_type,
                    "error": judge_err[j.judge_id].get(key),
                }

        agg = aggregate_judgements(per_judge_results)
        safe_write_jsonl(summary_path, {
            "experiment": exp_name,
            "case_id": case_id,
            "judge_count_total": len(JUDGES),
            "judge_count_ok": len(per_judge_results),
            "aggregate": agg,
            "per_judge": per_judge,
        })
        # summary_done.add(key)
        wrote += 1
        if wrote % 100 == 0:
            _log(f"[SUMMARY-PROGRESS] {wrote}/{len(all_records)}")

    _log(f"[SUMMARY-DONE] wrote={wrote} summary_path={summary_path}")
    _log(f"[DONE] outputs_dir={out_dir}")


if __name__ == "__main__":
    main()
