from __future__ import annotations

import ast
import json
import math
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple, Literal

from pydantic import BaseModel, Field

from src.tools.prompt_loader import render
from src.tools.config import load_config
from src.tools.model_batch_clients import BatchLLMClient


# =========================
# Configuration section
# =========================
RUN_DIR = Path("benchmarks_results/20260428_022501_llama-3.3-70b-instruct")
EXPERIMENTS = ["OURS", "woIU", "woKR", "IOP"]
OUT_DIRNAME = "correctness_judgements"

PROMPT_SYSTEM = "paper_benchmarks/judge_prompts/system_prompt.md"
PROMPT_USER_SEMANTIC = "paper_benchmarks/judge_prompts/user_prompt_semantic.md"
PROMPT_USER_FULFILLMENT = "paper_benchmarks/judge_prompts/user_prompt_fulfillment.md"
PROMPT_USER_PARAM = "paper_benchmarks/judge_prompts/user_prompt_param.md"

# Real Batch parameters
REQUESTS_PER_BATCH_FILE = 600
BATCH_ENDPOINT = "/v1/chat/completions"
BATCH_COMPLETION_WINDOW = "24h"
BATCH_ENABLE_THINKING = False
BATCH_REQUEST_EXTRA_BODY: Dict[str, Any] = {}

# Structured output control:
# - json_schema: Try to constrain returned structure through response_format + json_schema (closest to the original invoke_structured_batch)
# - json_object: Only require a JSON object, without a specific schema
# - off: Do not attach API-level structured parameters; rely only on prompt constraints and compatible backfill during harvest
BATCH_STRUCTURED_OUTPUT_MODE = "json_schema"  # json_schema / json_object / off
BATCH_JSON_SCHEMA_STRICT = True

# Compatible backfill during harvest: avoid failing the whole item when the model only misses fields such as confidence / judge_type
HARVEST_ENABLE_FIELD_BACKFILL = True
HARVEST_DEFAULT_CONFIDENCE = 0.5
HARVEST_DEFAULT_MAJOR_ISSUES: List[str] = []
HARVEST_DEFAULT_RATIONALE_BRIEF = ""

# Test mode:
# - True: Use only the first N cases of each experiment and write to a separate test directory to avoid contaminating official results
# - False: Run the full set and write outputs to OUT_DIRNAME
TEST_MODE = False
TEST_CASES_PER_EXPERIMENT = 3

# Stage control:
# - inspect: Only inspect which cases, how many requests, and how many batches will be processed; no cost incurred
# - submit: Generate JSONL and submit batch jobs
# - harvest: Check whether all batches are complete; stop if not all are complete; download and backfill all after completion
# - summary: Rebuild summary only from existing judge jsonl files
STAGE = "harvest"


class SemanticJudgeResult(BaseModel):
    judge_type: Literal["semantic"] = "semantic"
    data_adherence: float = Field(ge=0, le=10)
    semantic_faithfulness: float = Field(ge=0, le=10)
    confidence: float = Field(ge=0, le=1)
    major_issues: List[str]
    rationale_brief: str


class FulfillmentJudgeResult(BaseModel):
    judge_type: Literal["fulfillment"] = "fulfillment"
    task_fulfillment: float = Field(ge=0, le=10)
    output_quality: float = Field(ge=0, le=10)
    confidence: float = Field(ge=0, le=1)
    major_issues: List[str]
    rationale_brief: str


class ParamJudgeResult(BaseModel):
    judge_type: Literal["param"] = "param"
    parameter_validity: float = Field(ge=0, le=10)
    result_plausibility: float = Field(ge=0, le=10)
    confidence: float = Field(ge=0, le=1)
    major_issues: List[str]
    rationale_brief: str


@dataclass
class JudgeConfig:
    judge_id: str
    judge_style: str
    judge_type: str
    schema: Any
    prompt_user: str
    strict: bool = True


# =========================
# Utility functions
# =========================
def _now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _log(msg: str) -> None:
    print(f"[{_now_str()}] {msg}", flush=True)


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_run_label() -> str:
    if TEST_MODE:
        return f"test_first{TEST_CASES_PER_EXPERIMENT}"
    return "full"


def get_out_dir() -> Path:
    if TEST_MODE:
        return RUN_DIR / f"{OUT_DIRNAME}_{get_run_label()}"
    return RUN_DIR / OUT_DIRNAME


def _strip_code_fences(text: str) -> str:
    s = text.strip()
    if not s.startswith("```"):
        return s
    lines = s.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _extract_json_object_candidate(text: str) -> Optional[str]:
    s = text.strip()
    start = s.find("{")
    end = s.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    return s[start:end + 1].strip()


def parse_maybe_json_or_pyobj(x: Any) -> Any:
    if x is None:
        return None
    if isinstance(x, (dict, list)):
        return x
    if not isinstance(x, str):
        return None

    raw = x.strip()
    if not raw:
        return None

    candidates: List[str] = [raw]
    stripped = _strip_code_fences(raw)
    if stripped and stripped not in candidates:
        candidates.append(stripped)
    extracted = _extract_json_object_candidate(stripped)
    if extracted and extracted not in candidates:
        candidates.append(extracted)

    for s in candidates:
        try:
            return json.loads(s)
        except Exception:
            pass
        try:
            return ast.literal_eval(s)
        except Exception:
            pass
    return None


def iter_case_records(run_dir: Path, exp_names: Sequence[str]) -> Iterable[Tuple[str, Path, Dict[str, Any]]]:
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


def select_records_for_run(
    all_records: List[Tuple[str, str, Dict[str, Any]]]
) -> List[Tuple[str, str, Dict[str, Any]]]:
    if not TEST_MODE:
        return all_records

    if TEST_CASES_PER_EXPERIMENT <= 0:
        raise ValueError("TEST_CASES_PER_EXPERIMENT must be > 0 when TEST_MODE=True")

    selected: List[Tuple[str, str, Dict[str, Any]]] = []
    per_exp_count: Dict[str, int] = {exp: 0 for exp in EXPERIMENTS}

    for exp_name, case_id, rec in all_records:
        if per_exp_count.get(exp_name, 0) >= TEST_CASES_PER_EXPERIMENT:
            continue
        selected.append((exp_name, case_id, rec))
        per_exp_count[exp_name] = per_exp_count.get(exp_name, 0) + 1

    return selected


def load_done_keys(jsonl_path: Path) -> Set[str]:
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


def load_json(path: Path, default: Any) -> Any:
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def chunked(seq: Sequence[Any], n: int) -> Iterable[Sequence[Any]]:
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


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
    return (
        0.20 * subscores["task_fulfillment"] +
        0.15 * subscores["data_adherence"] +
        0.20 * subscores["semantic_faithfulness"] +
        0.20 * subscores["parameter_validity"] +
        0.15 * subscores["output_quality"] +
        0.10 * subscores["result_plausibility"]
    )


def build_judges() -> List[JudgeConfig]:
    return [
        JudgeConfig(
            judge_id="j1_semantic",
            judge_style="重视任务语义与数据角色一致性：重点审查任务是否被改成另一个问题，数据角色、因子角色、输出角色是否正确。",
            judge_type="semantic",
            schema=SemanticJudgeResult,
            prompt_user=PROMPT_USER_SEMANTIC,
            strict=True,
        ),
        JudgeConfig(
            judge_id="j2_fulfillment",
            judge_style="重视任务完成度与输出闭环：重点审查是否真正完成用户要求，是否产生用户可用的最终结果。",
            judge_type="fulfillment",
            schema=FulfillmentJudgeResult,
            prompt_user=PROMPT_USER_FULFILLMENT,
            strict=True,
        ),
        JudgeConfig(
            judge_id="j3_param_exec",
            judge_style="重视关键参数合理性与真实可执行性：重点审查 band、公式、产品层级、scale/offset、阈值、窗口、时相，以及伪验证问题。",
            judge_type="param",
            schema=ParamJudgeResult,
            prompt_user=PROMPT_USER_PARAM,
            strict=True,
        ),
    ]


def build_batch_client() -> Tuple[BatchLLMClient, str, str]:
    cfg = load_config("src/paper_benchmarks/configs/config_judges.yaml")
    llm_cfg = cfg.get("llm", {})
    provider = llm_cfg.get("provider")
    model = llm_cfg.get("model")
    timeout_s = int(llm_cfg.get("timeout_s"))
    api_key = llm_cfg.get("api_key")
    base_url = llm_cfg.get("base_url")

    client = BatchLLMClient(
        provider=provider,
        model=model,
        api_key=api_key,
        base_url=base_url,
        completion_window=BATCH_COMPLETION_WINDOW,
        timeout_s=timeout_s,
        enable_thinking=BATCH_ENABLE_THINKING,
    )
    return client, provider, model


def get_paths(out_dir: Path) -> Dict[str, Path]:
    return {
        "registry": out_dir / "batch_jobs_registry.json",
        "batch_inputs": ensure_dir(out_dir / "batch_inputs"),
        "batch_downloads": ensure_dir(out_dir / "batch_downloads"),
        "summary": out_dir / "summary_by_case.jsonl",
        "selection_manifest": out_dir / "selected_cases_manifest.json",
    }


def collect_all_records() -> List[Tuple[str, str, Dict[str, Any]]]:
    all_records: List[Tuple[str, str, Dict[str, Any]]] = []
    for exp_name, rec_path, rec in iter_case_records(RUN_DIR, EXPERIMENTS):
        case = rec.get("case", {}) or {}
        case_id = (case.get("case_id") or rec_path.stem).strip()
        all_records.append((exp_name, case_id, rec))
    return select_records_for_run(all_records)


def make_local_job_id(judge_id: str, part_idx: int) -> str:
    return f"{get_run_label()}__{judge_id}__part{part_idx:03d}"


def make_custom_id(exp_name: str, case_id: str, judge_id: str) -> str:
    return f"{exp_name}::{case_id}::{judge_id}"


def parse_custom_id(custom_id: str) -> Tuple[str, str, str]:
    parts = custom_id.split("::", 2)
    if len(parts) != 3:
        raise ValueError(f"invalid custom_id: {custom_id}")
    return parts[0], parts[1], parts[2]


def extract_text_from_batch_success_item(item: Dict[str, Any]) -> str:
    response = item.get("response") or {}
    body = response.get("body") or {}
    choices = body.get("choices") or []
    if not choices:
        return ""
    message = choices[0].get("message") or {}
    content = message.get("content")

    if isinstance(content, str):
        return content.strip()

    if isinstance(content, list):
        parts: List[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
        return "".join(parts).strip()

    return ""


def build_request_for_case(
    exp_name: str,
    case_id: str,
    rec: Dict[str, Any],
    judge: JudgeConfig,
    system_prompt: str,
    batch_client: BatchLLMClient,
) -> Dict[str, Any]:
    user_prompt = build_user_prompt(rec, judge) + build_output_contract_hint(judge)
    messages: List[Dict[str, Any]] = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": user_prompt})

    return batch_client.build_chat_request(
        custom_id=make_custom_id(exp_name, case_id, judge.judge_id),
        messages=messages,
        endpoint=BATCH_ENDPOINT,
        extra_body=build_structured_extra_body(judge),
    )


def build_output_contract_hint(judge: JudgeConfig) -> str:
    schema = judge.schema.model_json_schema()
    schema_text = json.dumps(schema, ensure_ascii=False, indent=2)
    return (
        "\n\n【输出格式要求】\n"
        "你必须只输出一个 JSON 对象，不要输出解释、前缀、后缀、Markdown 代码围栏。"
        "\n返回内容必须满足下面的 JSON Schema；若某字段无明显问题，也必须按 schema 返回。"
        f"\nJSON Schema:\n{schema_text}"
    )


def build_structured_extra_body(judge: JudgeConfig) -> Optional[Dict[str, Any]]:
    extra: Dict[str, Any] = dict(BATCH_REQUEST_EXTRA_BODY)

    if BATCH_STRUCTURED_OUTPUT_MODE == "off":
        return extra or None
    if BATCH_STRUCTURED_OUTPUT_MODE == "json_object":
        extra["response_format"] = {"type": "json_object"}
        return extra
    if BATCH_STRUCTURED_OUTPUT_MODE == "json_schema":
        extra["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": judge.judge_id,
                "strict": BATCH_JSON_SCHEMA_STRICT,
                "schema": judge.schema.model_json_schema(),
            },
        }
        return extra

    raise ValueError(f"Unknown BATCH_STRUCTURED_OUTPUT_MODE: {BATCH_STRUCTURED_OUTPUT_MODE}")


def _coerce_float_if_possible(value: Any) -> Any:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return value
        try:
            return float(s)
        except Exception:
            return value
    return value


def normalize_parsed_judge_result(parsed: Any, judge: JudgeConfig) -> Any:
    if not HARVEST_ENABLE_FIELD_BACKFILL or not isinstance(parsed, dict):
        return parsed

    obj = dict(parsed)

    # v2.1 patch 1：judge_type force-overwrite with the current judge fixed value to avoid incorrect literals from the model
    obj["judge_type"] = judge.judge_type

    if obj.get("confidence") in (None, ""):
        obj["confidence"] = HARVEST_DEFAULT_CONFIDENCE
    if obj.get("major_issues") is None:
        obj["major_issues"] = list(HARVEST_DEFAULT_MAJOR_ISSUES)
    if obj.get("rationale_brief") is None:
        obj["rationale_brief"] = HARVEST_DEFAULT_RATIONALE_BRIEF

    if isinstance(obj.get("major_issues"), str):
        txt = obj["major_issues"].strip()
        obj["major_issues"] = [txt] if txt else []
    elif isinstance(obj.get("major_issues"), list):
        obj["major_issues"] = [str(x).strip() for x in obj["major_issues"] if str(x).strip()]

    if isinstance(obj.get("rationale_brief"), str):
        obj["rationale_brief"] = obj["rationale_brief"].strip()


    for field_name in judge.schema.model_fields.keys():
        if field_name in obj:
            obj[field_name] = _coerce_float_if_possible(obj[field_name])

    return obj


def build_selection_manifest(all_records: List[Tuple[str, str, Dict[str, Any]]]) -> Dict[str, Any]:
    counts: Dict[str, int] = {exp: 0 for exp in EXPERIMENTS}
    case_keys_by_exp: Dict[str, List[str]] = {exp: [] for exp in EXPERIMENTS}
    for exp_name, case_id, _rec in all_records:
        counts[exp_name] = counts.get(exp_name, 0) + 1
        case_keys_by_exp.setdefault(exp_name, []).append(case_id)

    return {
        "run_label": get_run_label(),
        "test_mode": TEST_MODE,
        "test_cases_per_experiment": TEST_CASES_PER_EXPERIMENT if TEST_MODE else None,
        "run_dir": str(RUN_DIR),
        "out_dir": str(get_out_dir()),
        "experiments": EXPERIMENTS,
        "selected_total_records": len(all_records),
        "selected_counts_by_experiment": counts,
        "selected_case_ids_by_experiment": case_keys_by_exp,
    }


def inspect_run_plan() -> None:
    out_dir = ensure_dir(get_out_dir())
    paths = get_paths(out_dir)
    judges = build_judges()
    all_records = collect_all_records()
    manifest = build_selection_manifest(all_records)
    save_json(paths["selection_manifest"], manifest)

    _log(f"[INSPECT] mode={get_run_label()} out_dir={out_dir}")
    _log(f"[INSPECT] selected_total_records={len(all_records)}")
    for exp in EXPERIMENTS:
        _log(
            f"[INSPECT] experiment={exp} selected_cases={manifest['selected_counts_by_experiment'].get(exp, 0)}"
        )

    judge_done: Dict[str, Set[str]] = {
        j.judge_id: load_done_keys(out_dir / f"{j.judge_id}.jsonl")
        for j in judges
    }

    for judge in judges:
        done = judge_done[judge.judge_id]
        pending_count = 0
        no_pred_count = 0
        for exp_name, case_id, rec in all_records:
            key = f"{exp_name}::{case_id}"
            if key in done:
                continue
            pred_code = ((rec.get("result", {}) or {}).get("code") or "").strip()
            if not pred_code:
                no_pred_count += 1
                continue
            pending_count += 1

        batch_count = math.ceil(pending_count / REQUESTS_PER_BATCH_FILE) if pending_count > 0 else 0
        _log(
            f"[INSPECT] judge={judge.judge_id} done={len(done)} pending={pending_count} "
            f"no_pred_code={no_pred_count} estimated_batches={batch_count}"
        )

    _log(f"[INSPECT] selection manifest saved to {paths['selection_manifest']}")
    _log("[INSPECT] no API call has been made.")


def prepare_and_submit_jobs() -> None:
    out_dir = ensure_dir(get_out_dir())
    paths = get_paths(out_dir)
    registry_path = paths["registry"]
    registry: List[Dict[str, Any]] = load_json(registry_path, [])
    registry_by_local_job: Dict[str, Dict[str, Any]] = {x["local_job_id"]: x for x in registry if "local_job_id" in x}

    system_prompt = render(PROMPT_SYSTEM)
    judges = build_judges()
    batch_client, provider, model = build_batch_client()
    all_records = collect_all_records()
    save_json(paths["selection_manifest"], build_selection_manifest(all_records))

    _log(f"[PREPARE] mode={get_run_label()} total records loaded: {len(all_records)} out_dir={out_dir}")

    judge_done: Dict[str, Set[str]] = {
        j.judge_id: load_done_keys(out_dir / f"{j.judge_id}.jsonl")
        for j in judges
    }

    created_jobs = 0
    skipped_jobs = 0

    for judge in judges:
        done = judge_done[judge.judge_id]
        pending: List[Tuple[str, str, Dict[str, Any]]] = []
        for exp_name, case_id, rec in all_records:
            key = f"{exp_name}::{case_id}"
            if key in done:
                continue
            pred_code = ((rec.get("result", {}) or {}).get("code") or "").strip()
            if not pred_code:
                safe_write_jsonl(out_dir / f"{judge.judge_id}.jsonl", {
                    "experiment": exp_name,
                    "case_id": case_id,
                    "ok": False,
                    "judge_id": judge.judge_id,
                    "model": model,
                    "error": "no_pred_code",
                })
                done.add(key)
                continue
            pending.append((exp_name, case_id, rec))

        if not pending:
            _log(f"[PREPARE] judge={judge.judge_id} no pending items")
            continue

        for part_idx, chunk in enumerate(chunked(pending, REQUESTS_PER_BATCH_FILE), start=1):
            local_job_id = make_local_job_id(judge.judge_id, part_idx)
            if local_job_id in registry_by_local_job:
                _log(f"[PREPARE] skip existing local_job_id={local_job_id}")
                skipped_jobs += 1
                continue

            requests: List[Dict[str, Any]] = []
            case_keys: List[str] = []
            for exp_name, case_id, rec in chunk:
                requests.append(build_request_for_case(exp_name, case_id, rec, judge, system_prompt, batch_client))
                case_keys.append(f"{exp_name}::{case_id}")

            jsonl_path = paths["batch_inputs"] / f"{local_job_id}.jsonl"
            batch_client.write_requests_jsonl(requests, jsonl_path)
            input_file_id = batch_client.upload_batch_file(jsonl_path)
            job = batch_client.create_batch_job(
                input_file_id=input_file_id,
                endpoint=BATCH_ENDPOINT,
                completion_window=BATCH_COMPLETION_WINDOW,
                metadata={
                    "ds_name": local_job_id,
                    "ds_description": f"correctness judge batch: {judge.judge_id} ({get_run_label()})",
                },
            )

            record = {
                "local_job_id": local_job_id,
                "judge_id": judge.judge_id,
                "judge_type": judge.judge_type,
                "model": model,
                "provider": provider,
                "jsonl_path": str(jsonl_path),
                "request_count": len(requests),
                "case_keys": case_keys,
                "input_file_id": input_file_id,
                "batch_id": job.batch_id,
                "endpoint": BATCH_ENDPOINT,
                "remote_status": job.status,
                "output_file_id": job.output_file_id,
                "error_file_id": job.error_file_id,
                "submitted_at": _now_str(),
                "harvested": False,
                "run_label": get_run_label(),
                "test_mode": TEST_MODE,
            }
            registry.append(record)
            registry_by_local_job[local_job_id] = record
            save_json(registry_path, registry)

            created_jobs += 1
            _log(
                f"[SUBMIT] local_job_id={local_job_id} batch_id={job.batch_id} "
                f"judge={judge.judge_id} requests={len(requests)} status={job.status}"
            )

    _log(
        f"[DONE] prepare_and_submit_jobs finished. "
        f"created_jobs={created_jobs}, skipped_jobs={skipped_jobs}, registry={registry_path}"
    )


def harvest_job_outputs(
    *,
    success_items: List[Dict[str, Any]],
    error_items: List[Dict[str, Any]],
    judges_by_id: Dict[str, JudgeConfig],
    out_dir: Path,
    model: str,
    judge_done: Dict[str, Set[str]],
) -> None:
    for item in success_items:
        custom_id = item.get("custom_id")
        if not custom_id:
            continue
        try:
            exp_name, case_id, judge_id = parse_custom_id(str(custom_id))
        except Exception as e:
            _log(f"[HARVEST] invalid custom_id in success item: {custom_id} ({e})")
            continue

        key = f"{exp_name}::{case_id}"
        if key in judge_done[judge_id]:
            continue

        judge = judges_by_id[judge_id]
        text = extract_text_from_batch_success_item(item)
        parsed = parse_maybe_json_or_pyobj(text)
        parsed = normalize_parsed_judge_result(parsed, judge)

        if parsed is None:
            safe_write_jsonl(out_dir / f"{judge_id}.jsonl", {
                "experiment": exp_name,
                "case_id": case_id,
                "ok": False,
                "judge_id": judge_id,
                "model": model,
                "error": f"parse_error: cannot parse model output as JSON object: {text[:300]}",
            })
            judge_done[judge_id].add(key)
            continue

        try:
            jr = judge.schema.model_validate(parsed)
            safe_write_jsonl(out_dir / f"{judge_id}.jsonl", {
                "experiment": exp_name,
                "case_id": case_id,
                "ok": True,
                "judge_id": judge_id,
                "model": model,
                "result": jr.model_dump(),
            })
        except Exception as e:
            safe_write_jsonl(out_dir / f"{judge_id}.jsonl", {
                "experiment": exp_name,
                "case_id": case_id,
                "ok": False,
                "judge_id": judge_id,
                "model": model,
                "error": f"validate_error: {type(e).__name__}: {e}",
            })
        judge_done[judge_id].add(key)

    for item in error_items:
        custom_id = item.get("custom_id")
        if not custom_id:
            continue
        try:
            exp_name, case_id, judge_id = parse_custom_id(str(custom_id))
        except Exception as e:
            _log(f"[HARVEST] invalid custom_id in error item: {custom_id} ({e})")
            continue

        key = f"{exp_name}::{case_id}"
        if key in judge_done[judge_id]:
            continue

        err_obj = item.get("error") or {}
        err_text = _compact_text(err_obj) or _compact_text(item)
        safe_write_jsonl(out_dir / f"{judge_id}.jsonl", {
            "experiment": exp_name,
            "case_id": case_id,
            "ok": False,
            "judge_id": judge_id,
            "model": model,
            "error": err_text,
        })
        judge_done[judge_id].add(key)


def rebuild_summary() -> None:
    out_dir = ensure_dir(get_out_dir())
    summary_path = out_dir / "summary_by_case.jsonl"
    judges = build_judges()
    judge_map: Dict[str, Dict[str, Any]] = {j.judge_id: {} for j in judges}
    judge_err: Dict[str, Dict[str, str]] = {j.judge_id: {} for j in judges}

    for j in judges:
        p = out_dir / f"{j.judge_id}.jsonl"
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
                        # v2.1 patch 3：also run normalize during summary to stay consistent with harvest
                        normalized_result = normalize_parsed_judge_result(obj["result"], j)
                        judge_map[j.judge_id][key] = j.schema.model_validate(normalized_result)
                    except Exception as e:
                        judge_err[j.judge_id][key] = f"validate_error: {type(e).__name__}: {e}"
                elif obj.get("ok") is False:
                    judge_err[j.judge_id][key] = obj.get("error") or "error"

    all_records = collect_all_records()
    if summary_path.exists():
        summary_path.unlink()

    wrote = 0
    with summary_path.open("w", encoding="utf-8") as fout:
        for exp_name, case_id, _rec in all_records:
            key = f"{exp_name}::{case_id}"
            per_judge_results: Dict[str, Any] = {}
            per_judge: Dict[str, Any] = {}

            for j in judges:
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
            fout.write(json.dumps({
                "experiment": exp_name,
                "case_id": case_id,
                "judge_count_total": len(judges),
                "judge_count_ok": len(per_judge_results),
                "aggregate": agg,
                "per_judge": per_judge,
            }, ensure_ascii=False) + "\n")
            wrote += 1
            if wrote % 100 == 0:
                _log(f"[SUMMARY-PROGRESS] {wrote}/{len(all_records)}")

    _log(f"[SUMMARY-DONE] wrote={wrote} summary_path={summary_path}")
    _log(f"[DONE] outputs_dir={out_dir}")


def poll_and_harvest_jobs() -> None:
    out_dir = ensure_dir(get_out_dir())
    paths = get_paths(out_dir)
    registry_path = paths["registry"]
    registry: List[Dict[str, Any]] = load_json(registry_path, [])

    if not registry:
        _log(f"[HARVEST] no registry found or registry empty: {registry_path}")
        return

    judges = build_judges()
    judges_by_id = {j.judge_id: j for j in judges}
    judge_done: Dict[str, Set[str]] = {
        j.judge_id: load_done_keys(out_dir / f"{j.judge_id}.jsonl")
        for j in judges
    }
    batch_client, _provider, model = build_batch_client()

    terminal_statuses = {"completed", "failed", "expired", "cancelled"}
    all_done = True

    for rec in registry:
        if rec.get("harvested") is True:
            continue
        batch_id = rec.get("batch_id")
        local_job_id = rec.get("local_job_id", "<unknown>")
        if not batch_id:
            all_done = False
            continue

        info = batch_client.retrieve_batch_job(batch_id)
        raw = info.raw or {}
        request_counts = raw.get("request_counts") or {}

        rec["remote_status"] = info.status
        rec["output_file_id"] = info.output_file_id
        rec["error_file_id"] = info.error_file_id
        rec["last_checked_at"] = _now_str()

        _log(
            f"[POLL] local_job_id={local_job_id} batch_id={batch_id} status={info.status} "
            f"request_counts={request_counts}"
        )

        if info.status not in terminal_statuses:
            all_done = False

    save_json(registry_path, registry)

    if not all_done:
        _log("[HARVEST] not all batch jobs finished yet. stop.")
        return

    harvested_any = False
    for rec in registry:
        if rec.get("harvested") is True:
            continue

        batch_id = rec.get("batch_id")
        local_job_id = rec.get("local_job_id", "<unknown>")
        info_status = rec.get("remote_status")
        if not batch_id:
            continue
        if info_status not in terminal_statuses:
            continue

        download_dir = ensure_dir(paths["batch_downloads"] / local_job_id)
        success_items: List[Dict[str, Any]] = []
        error_items: List[Dict[str, Any]] = []

        output_file_id = rec.get("output_file_id")
        error_file_id = rec.get("error_file_id")

        if output_file_id:
            success_path = download_dir / "success.jsonl"
            batch_client.download_file_to_path(output_file_id, success_path)
            success_items = batch_client.parse_jsonl_text(success_path.read_text(encoding="utf-8"))
            _log(f"[HARVEST] downloaded success file for {local_job_id}: items={len(success_items)}")

        if error_file_id:
            error_path = download_dir / "error.jsonl"
            batch_client.download_file_to_path(error_file_id, error_path)
            error_items = batch_client.parse_jsonl_text(error_path.read_text(encoding="utf-8"))
            _log(f"[HARVEST] downloaded error file for {local_job_id}: items={len(error_items)}")

        harvest_job_outputs(
            success_items=success_items,
            error_items=error_items,
            judges_by_id=judges_by_id,
            out_dir=out_dir,
            model=model,
            judge_done=judge_done,
        )

        rec["harvested"] = True
        rec["harvested_at"] = _now_str()
        harvested_any = True

    save_json(registry_path, registry)

    if harvested_any:
        rebuild_summary()
    else:
        _log("[HARVEST] all jobs done, but no new jobs harvested this round.")


def main() -> None:
    if STAGE == "inspect":
        inspect_run_plan()
    elif STAGE == "submit":
        prepare_and_submit_jobs()
    elif STAGE == "harvest":
        poll_and_harvest_jobs()
    elif STAGE == "summary":
        rebuild_summary()
    else:
        raise ValueError(f"Unknown STAGE: {STAGE}")


if __name__ == "__main__":
    main()
