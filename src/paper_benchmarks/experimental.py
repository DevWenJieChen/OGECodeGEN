import json
import ast
import os
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, is_dataclass, dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Callable
from datetime import datetime


from src.tools.config import load_config
from main_auto_oge_coder import run_oge_coder
from main_io_prompting import run_iop


# =========================
# Configuration section argparse Note: see the related implementation logic.
# =========================
CASES_JSON_PATH = "data_json/paper_experiment/benchmark_with_dag.json"   #  json Note: see the related implementation logic.
OUT_DIR = "benchmarks_results"                       # Output root directory
MAX_WORKERS = 8                                 # Number of concurrent threads (batch parameter)
LIMIT = 0                                            # 0 = run all; >0 = run only the first N cases

RUN_ID: str | None = None  # None means create a new run; specifying a value such as "20260203_145500" resumes that directory
FORCE_RERUN = False        # True means ignore existing results and rerun everything

# =========================
# Experiment convention: one experiment = one runner + one cfg
# =========================
RunnerFn = Callable[..., Any]

@dataclass
class Experiment:
    name: str                  # Folder name, such as "OURS" / "IOP" / "woIU"
    cfg_path: str              # Config file path
    runner: RunnerFn           # Execution function: run_oge_coder / run_iop, etc.


# =========================
# Utility: convert PipelineState to a JSON-serializable structure
# =========================
def to_jsonable(x: Any) -> Any:
    if x is None:
        return None

    if isinstance(x, (str, int, float, bool)):
        return x
    if isinstance(x, (list, tuple)):
        return [to_jsonable(v) for v in x]
    if isinstance(x, dict):
        return {str(k): to_jsonable(v) for k, v in x.items()}
    if is_dataclass(x):
        return {k: to_jsonable(v) for k, v in asdict(x).items()}
    # Compatible with pydantic v1/v2
    if hasattr(x, "model_dump"):
        return to_jsonable(x.model_dump())
    if hasattr(x, "dict"):
        return to_jsonable(x.dict())
    # Fallback: KnowledgeDoc/custom objects, etc.
    return {"__repr__": repr(x), "__type__": type(x).__name__}


def read_cases(path: str) -> List[Dict[str, Any]]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("cases json must be a list[object]")
    return data


def ensure_dir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)


def make_run_id() -> str:
    return time.strftime("%Y%m%d_%H%M%S")

def get_model_tag(cfg_path: str) -> str:
    cfg = load_config(cfg_path)
    model = ((cfg.get("llm") or {}).get("model")) or "unknown_model"
    return str(model).replace("/", "_").replace(":", "_")

def is_case_done(exp_dir: Path, case_id: str) -> bool:
    p = exp_dir / f"{case_id}.json"
    if not p.is_file():
        return False
    return True

def atomic_write_json(path: Path, obj: Any) -> None:
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)

def _now() -> str:
    return datetime.now().strftime("%H:%M:%S")

def _fmt_secs(sec: float) -> str:
    if sec < 60:
        return f"{sec:.1f}s"
    m = int(sec // 60)
    s = sec - m * 60
    return f"{m}m{s:.0f}s"

def parse_maybe_json_or_pyobj_local(x: Any) -> Any:
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


def get_dag_json_state(verify_report: Any) -> str:
    """
    Return:
      - "missing": verify_report is not a dict or does not contain dag_json
      - "empty": dag_json exists explicitly but is an empty []
      - "nonempty": dag_json exists and is non-empty

    """
    vr = parse_maybe_json_or_pyobj_local(verify_report)
    if not isinstance(vr, dict) or "dag_json" not in vr:
        return "missing"

    dag_json = vr.get("dag_json")
    if isinstance(dag_json, list):
        return "nonempty" if len(dag_json) > 0 else "empty"
    if dag_json is None:
        return "missing"
    return "nonempty"


def is_effective_executable(verify_ok: Any, verify_report: Any) -> bool:
    return (verify_ok is True) and (get_dag_json_state(verify_report) == "nonempty")


def run_one_case(case: Dict[str, Any], cfg: dict, runner: RunnerFn, out_dir: Path) -> Dict[str, Any]:
    """
    Run one task, save the result as one JSON file, and return a task summary.
    """
    case_id = case.get("case_id")
    if not case_id:
        raise ValueError("case_id missing")

    user_query = (case.get("description") or "").strip()
    query_lang = (case.get("lang") or "zh").strip()

    # run_oge_coder returns PipelineState
    # pls = run_oge_coder(user_query=user_query, query_lang=query_lang, cfg=cfg)  # type: ignore
    pls = runner(user_query=user_query, query_lang=query_lang, cfg=cfg,data_info=case.get("data_ref"))
    if pls is None:
        raise TypeError(f"runner {runner} returned None; please return PipelineState for evaluation.")

    verify_ok_raw = getattr(pls, "verify_ok", None)
    verify_report_raw = getattr(pls, "verify_report", None)
    dag_json_state = get_dag_json_state(verify_report_raw)
    executability_ok = is_effective_executable(verify_ok_raw, verify_report_raw)

    record = {
        "case": {
            "case_id": case.get("case_id"),
            "task_type": case.get("task_type"),
            "lang": case.get("lang"),
            "description": case.get("description"),
            "data_ref": case.get("data_ref"),
            "notes": case.get("notes"),
            # Store gold information together, so evaluation does not need to read the original cases.json again
            "target_code": case.get("code"),
            "target_dag": case.get("dag"),
        },
        "result": {
            "code":getattr(pls, "code", None),
            "verify_ok": verify_ok_raw,
            "verify_ok_raw": verify_ok_raw,
            "verify_report": verify_report_raw,
            "dag_json_state": dag_json_state,
            "executability_ok": executability_ok,
            "has_modules": getattr(pls, "has_modules", None),
            "max_fix_num": getattr(pls, "max_fix_num", None),
        },
        "pipeline_state": to_jsonable(pls),
    }

    out_path = out_dir / f"{case_id}.json"
    # out_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    atomic_write_json(out_path, record)

    return {
        "case_id": case_id,
        "task_type": case.get("task_type"),
        "lang": query_lang,
        "verify_ok": record["result"]["verify_ok"],
        "executability_ok": executability_ok,
        "dag_json_state": dag_json_state,
        "path": str(out_path),
    }



def run_experiment(
    *,
    exp: Experiment,
    cases: List[Dict[str, Any]],
    run_dir: Path,
    max_workers: int,
) -> Dict[str, Any]:
    """
    Run one experiment (one cfg + one runner), store results under run_dir/<exp.name>/,
    and return the experiment index dict.

    """
    cfg = load_config(exp.cfg_path)

    exp_dir = run_dir / exp.name
    ensure_dir(exp_dir)

    index_path = exp_dir / "index.json"

    # 1) Load old index first (if it exists)
    old_index: Dict[str, Any] = {}
    if index_path.is_file():
        try:
            old_index = json.loads(index_path.read_text(encoding="utf-8"))
            if not isinstance(old_index, dict):
                old_index = {}
        except Exception:
            old_index = {}

    # 2) Convert old items / failures into maps for incremental updates by case_id
    old_items = old_index.get("items", []) or []
    old_failures = old_index.get("failures", []) or []

    item_map: Dict[str, Dict[str, Any]] = {}
    for it in old_items:
        if isinstance(it, dict) and it.get("case_id"):
            item_map[it["case_id"]] = it

    failure_map: Dict[str, Dict[str, Any]] = {}
    for it in old_failures:
        if isinstance(it, dict) and it.get("case_id"):
            failure_map[it["case_id"]] = it

    # Clean historical conflicts: if the same case_id exists in both items and failures, keep items first
    for cid in list(failure_map.keys()):
        if cid in item_map:
            failure_map.pop(cid, None)

    # 3) Keep latest metadata in the new index, but reuse historical items/failures first
    exp_index = {
        "experiment": exp.name,
        "cfg_path": exp.cfg_path,
        "runner": getattr(exp.runner, "__name__", str(exp.runner)),
        "count": len(cases),
        "max_workers": max_workers,
        "items": list(item_map.values()),
        "failures": list(failure_map.values()),
    }

    def flush_index() -> None:
        exp_index["items"] = list(item_map.values())
        exp_index["failures"] = list(failure_map.values())
        atomic_write_json(index_path, exp_index)
    todo = []
    skipped = 0
    for c in cases:
        cid = c.get("case_id")
        if not cid:
            continue
        if (not FORCE_RERUN) and is_case_done(exp_dir, cid):
            skipped += 1
            continue
        todo.append(c)

    exp_index["skipped"] = skipped
    exp_index["todo"] = len(todo)
    t0 = time.time()
    print(
        f"\n[{_now()}][{exp.name}] START  total={len(cases)}  todo={len(todo)}  skipped={skipped}  workers={max_workers}")
    print(f"[{_now()}][{exp.name}] cfg={exp.cfg_path}  runner={getattr(exp.runner, '__name__', str(exp.runner))}")

    # atomic_write_json(exp_dir / "index.json", exp_index)
    flush_index()

    done = 0
    ok_cnt = 0
    fail_cnt = 0
    total = len(todo)

    # Use ThreadPoolExecutor to create a pool with up to MAX_WORKERS worker threads
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        # Submit concurrent tasks in batch and create a mapping from Future objects to case_id values.
        futs = {
            ex.submit(run_one_case, case=c, cfg=cfg, runner=exp.runner, out_dir=exp_dir): c.get("case_id", "UNKNOWN")
            for c in todo
        }
        # as_completed is an iterator that returns Future objects in completion order
        # Process whichever task finishes first, rather than the submission order
        for fut in as_completed(futs):
            # Use the Future object to get the corresponding case_id from the mapping dictionary
            cid = futs[fut]
            try:
                summary = fut.result()
                # Success: write/update items and remove from failures
                item_map[cid] = summary
                failure_map.pop(cid, None)

                # If an old error file exists, delete it as well
                fail_path = exp_dir / f"{cid}.error.json"
                if fail_path.is_file():
                    try:
                        fail_path.unlink()
                    except Exception:
                        pass

                flush_index()

                done += 1
                # if summary.get("verify_ok") is True:
                if summary.get("executability_ok") is True:
                    ok_cnt += 1
                else:
                    # verify_ok=False still means the run completed successfully (it is just non-executable)
                    pass
                elapsed = time.time() - t0
                rate = done / elapsed if elapsed > 0 else 0.0
                pct = (done / total * 100) if total > 0 else 100.0
                print(
                    f"[{_now()}][{exp.name}] {done}/{total} ({pct:.1f}%) ok_exec={ok_cnt} fail={fail_cnt} rate={rate:.2f} case/s elapsed={_fmt_secs(elapsed)} | last={cid}")
            except Exception as e:
                tb = traceback.format_exc()
                failure_info = {
                    "case_id": cid,
                    "error": type(e).__name__,
                    "message": str(e),
                    "traceback": tb,
                }

                # Failure: write/update failures and remove from items
                failure_map[cid] = failure_info
                item_map.pop(cid, None)

                fail_path = exp_dir / f"{cid}.error.json"
                atomic_write_json(fail_path, failure_info)

                flush_index()
                done += 1
                fail_cnt += 1
                elapsed = time.time() - t0
                rate = done / elapsed if elapsed > 0 else 0.0
                pct = (done / total * 100) if total > 0 else 100.0
                print(f"[{_now()}][{exp.name}] {done}/{total} ({pct:.1f}%) ok_exec={ok_cnt} fail={fail_cnt} rate={rate:.2f} case/s elapsed={_fmt_secs(elapsed)} | last=FAIL:{cid}")

    flush_index()
    elapsed = time.time() - t0
    print(
        f"[{_now()}][{exp.name}] DONE  todo={len(todo)} skipped={skipped} failures={len(exp_index['failures'])} elapsed={_fmt_secs(elapsed)}")
    return exp_index


def run_all(
    *,
    experiments: List[Experiment],
    cases_json_path: str,
    out_root: str,
    max_workers: int,
    limit: int = 0,
) -> Path:
    cases = read_cases(cases_json_path)
    if limit and limit > 0:
        cases = cases[:limit]

    model_tag = get_model_tag(experiments[0].cfg_path)
    run_id = RUN_ID or f"{make_run_id()}_{model_tag}"
    # run_id = RUN_ID or make_run_id()
    run_dir = Path(out_root) / run_id
    ensure_dir(run_dir)
    print(f"\n[{_now()}][RUN] run_id={run_id} cases={len(cases)} experiments={len(experiments)} workers={max_workers}")
    print(f"[{_now()}][RUN] out_dir={run_dir}")
    for exp in experiments:
        print(f"[{_now()}][RUN] - {exp.name}: cfg={exp.cfg_path} runner={getattr(exp.runner,'__name__',str(exp.runner))}")


    run_index = {
        "run_id": run_id,
        "cwd": os.getcwd(),
        "cases_path": cases_json_path,
        "count": len(cases),
        "experiments": [],
    }

    for exp in experiments:
        exp_index = run_experiment(exp=exp, cases=cases, run_dir=run_dir, max_workers=max_workers)
        run_index["experiments"].append({
            "name": exp.name,
            "dir": str(run_dir / exp.name),
            "index_path": str(run_dir / exp.name / "index.json"),
            "failures": len(exp_index.get("failures", [])),
        })
        print(f"[{_now()}][RUN] finished {exp.name} failures={len(exp_index.get('failures', []))} skipped={exp_index.get('skipped')} todo={exp_index.get('todo')}")


    # (run_dir / "index.json").write_text(json.dumps(run_index, ensure_ascii=False, indent=2), encoding="utf-8")
    atomic_write_json(run_dir / "index.json", run_index)
    print(f"\nAll experiments saved to: {run_dir}")
    print(f"Run index: {run_dir / 'index.json'}")
    return run_dir

"""
benchmarks_results/
  20260203_145500/                # run_id (one batch run)
    index.json                     # master index containing summaries for all experiments
    OURS/                          # exp_name (one comparison experiment)
      index.json                   # index for this experiment
      T0001.json
      T0002.json
      ...
    IOP/                           # exp_name (another comparison experiment)
      index.json
      T0001.json
      ...
First run (RUN_ID=None)
Generates benchmarks_results/20260203_145500/...
Resume after interruption
Set RUN_ID = "20260203_145500" explicitly (or read it from an environment variable)
Run the script again
→ Completed cases are skipped automatically; only missing cases are run
To rerun one experiment
Simplest option: delete the corresponding experiment directory run_dir/EXP_NAME/
Or set FORCE_RERUN=True
"""
if __name__ == "__main__":
    # Freely combine comparison experiments here:
    # - name: output subdirectory name
    # - cfg_path: controls module switches/model/retrieval and related strategies
    # - runner: execution function (must consistently return PipelineState)
    experiments = [
        Experiment(name="OURS", cfg_path="config.yaml", runner=run_oge_coder),
        Experiment(name="woIU", cfg_path="src/paper_benchmarks/configs/config_woIU.yaml", runner=run_oge_coder),
        Experiment(name="woKR", cfg_path="src/paper_benchmarks/configs/config_woKR.yaml", runner=run_oge_coder),
        Experiment(name="IOP", cfg_path="src/paper_benchmarks/configs/config_iop.yaml", runner=run_iop),
    ]

    run_all(
        experiments=experiments,
        cases_json_path=CASES_JSON_PATH,
        out_root=OUT_DIR,
        max_workers=MAX_WORKERS,
        limit=LIMIT,
    )
