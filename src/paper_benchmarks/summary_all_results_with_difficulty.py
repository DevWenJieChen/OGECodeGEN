import csv
import json
from pathlib import Path
from collections import defaultdict


def safe_float(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def mean_or_none(values):
    vals = [v for v in values if v is not None]
    if not vals:
        return None
    return sum(vals) / len(vals)

def safe_int(x):
    try:
        if x is None or x == "":
            return None
        return int(float(x))
    except (TypeError, ValueError):
        return None


def is_true_value(x):
    """
    Support True/False, true/false, and 1/0 values in CSV files.

    """
    if isinstance(x, bool):
        return x
    if x is None:
        return False

    s = str(x).strip().lower()
    return s in {"true", "1", "yes", "y"}

def compute_debugging_at_k(rows, ks=(0, 1, 2)):
    """
    debugging@k =
        count(executability_ok=True and max_fix_num <= k) / total
    """
    total = len(rows)
    if total == 0:
        return {f"debugging@{k}": None for k in ks}

    counts = {k: 0 for k in ks}

    for row in rows:
        executable = is_true_value(row.get("executability_ok"))
        if not executable:
            continue

        fix_num = safe_int(row.get("max_fix_num"))
        if fix_num is None:
            raise ValueError(
                f"max_fix_num is missing or cannot be parsed: "
                f"experiment={row.get('experiment')}, case_id={row.get('case_id')}, "
                f"max_fix_num={row.get('max_fix_num')}"
            )

        for k in ks:
            if fix_num <= k:
                counts[k] += 1

    return {
        f"debugging@{k}": counts[k] / total
        for k in ks
    }

def load_correctness_from_summary(summary_by_case_path: str):
    """
    Read correctness results from summary_by_case.jsonl.

    Return:
    - case_score_map: {(experiment, case_id): raw_score_overall}
    - exp_scores_map: {experiment: [raw_score_overall, ...]}

    """
    summary_path = Path(summary_by_case_path)
    if not summary_path.exists():
        raise FileNotFoundError(f"File not found: {summary_path}")

    case_score_map = {}
    exp_scores_map = defaultdict(list)

    total_lines = 0
    skipped_lines = 0

    with summary_path.open("r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue

            total_lines += 1

            try:
                obj = json.loads(line)
            except json.JSONDecodeError as e:
                skipped_lines += 1
                print(f"[WARN] Line {line_no} JSON parse failed; skipped: {e}")
                continue

            experiment = obj.get("experiment")
            case_id = obj.get("case_id")
            aggregate = obj.get("aggregate") or {}
            score_overall = aggregate.get("score_overall")

            if experiment is None or case_id is None or score_overall is None:
                skipped_lines += 1
                print(f"[WARN] Line {line_no} is missing experiment/case_id/aggregate.score_overall; skipped")
                continue

            try:
                score_overall = float(score_overall)
            except (TypeError, ValueError):
                skipped_lines += 1
                print(f"[WARN] Line {line_no} has non-numeric score_overall; skipped: {score_overall}")
                continue

            case_score_map[(experiment, case_id)] = score_overall
            exp_scores_map[experiment].append(score_overall)

    if not case_score_map:
        raise ValueError("No valid correctness scores were read from summary_by_case.jsonl")

    print(f"[INFO] summary_by_case.jsonl total lines: {total_lines}")
    print(f"[INFO] skipped lines: {skipped_lines}")
    print(f"[INFO] valid cases: {len(case_score_map)}")
    print(f"[INFO] experiment groups: {len(exp_scores_map)}")

    return case_score_map, exp_scores_map



def load_case_difficulty_map(benchmark_meta_path: str):
    """
    Read case_id -> difficulty and other metadata from the benchmark metadata.

    """
    meta_path = Path(benchmark_meta_path)
    if not meta_path.exists():
        raise FileNotFoundError(f"File not found: {meta_path}")

    arr = json.loads(meta_path.read_text(encoding="utf-8"))
    difficulty_map = {}
    meta_map = {}

    for item in arr:
        case_id = item.get("case_id")
        if not case_id:
            continue

        difficulty = item.get("difficulty", "未知") or "未知"
        difficulty_map[case_id] = difficulty
        meta_map[case_id] = {
            "difficulty": difficulty,
            "task_type": item.get("task_type"),
            "lang": item.get("lang"),
        }

    if not difficulty_map:
        raise ValueError("No valid case_id/difficulty entries were read from the benchmark metadata")

    print(f"[INFO] benchmark metadata case count: {len(difficulty_map)}")
    return difficulty_map, meta_map

def update_metrics_summary(
    metrics_summary_path: str,
    exp_scores_map: dict,
    output_path: str | None = None,
    normalize_to_01: bool = False,
):
    """
    Update experiments[*].Correctness in metrics_summary.json.

    """
    metrics_path = Path(metrics_summary_path)
    if not metrics_path.exists():
        raise FileNotFoundError(f"File not found: {metrics_path}")

    with metrics_path.open("r", encoding="utf-8") as f:
        metrics_data = json.load(f)

    experiments = metrics_data.get("experiments")
    if not isinstance(experiments, list):
        raise ValueError("metrics_summary.json is missing the experiments list")

    correctness_mean_by_experiment = {}
    for exp, scores in exp_scores_map.items():
        mean_score = sum(scores) / len(scores)
        if normalize_to_01:
            mean_score /= 10.0
        correctness_mean_by_experiment[exp] = mean_score

    matched = 0
    unmatched = []

    for item in experiments:
        exp_name = item.get("experiment")
        if exp_name in correctness_mean_by_experiment:
            item["Correctness"] = correctness_mean_by_experiment[exp_name]
            matched += 1
        else:
            unmatched.append(exp_name)

    notes = metrics_data.setdefault("notes", {})
    if isinstance(notes, dict):
        notes["correctness"] = (
            "Filled from summary_by_case.jsonl by averaging aggregate.score_overall"
            + (" and normalized from 0-10 to 0-1." if normalize_to_01 else ".")
        )

    save_path = Path(output_path) if output_path else metrics_path
    with save_path.open("w", encoding="utf-8") as f:
        json.dump(metrics_data, f, ensure_ascii=False, indent=2)

    print("\n=== metrics_summary.json update result ===")
    for exp, value in correctness_mean_by_experiment.items():
        print(f"{exp}: {value:.6f}")

    print(f"[INFO] matched experiments: {matched}")
    if unmatched:
        print(f"[WARN] unmatched experiments: {unmatched}")

    print(f"[INFO] written to: {save_path}")


def update_metrics_by_case_csv(
    metrics_by_case_csv_path: str,
    case_score_map: dict,
    case_meta_map: dict,
    output_path: str | None = None,
    score_column: str = "CorrectnessScore",
    normalize_score: bool = False,
):
    """
    Update CorrectnessScore and difficulty for each case in metrics_by_case.csv.

    """
    csv_path = Path(metrics_by_case_csv_path)
    if not csv_path.exists():
        raise FileNotFoundError(f"File not found: {csv_path}")

    with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames) if reader.fieldnames else []

        if score_column not in fieldnames:
            fieldnames.append(score_column)
        if "difficulty" not in fieldnames:
            fieldnames.append("difficulty")

        rows = list(reader)

    matched_score = 0
    unmatched_score = 0
    matched_difficulty = 0
    unmatched_difficulty = 0

    for row in rows:
        experiment = row.get("experiment")
        case_id = row.get("case_id")
        key = (experiment, case_id)

        if key in case_score_map:
            score = case_score_map[key]
            if normalize_score:
                score /= 10.0
            row[score_column] = f"{score:.6f}"
            matched_score += 1
        else:
            row[score_column] = row.get(score_column, "")
            unmatched_score += 1

        if case_id in case_meta_map:
            row["difficulty"] = case_meta_map[case_id].get("difficulty", "未知") or "未知"
            matched_difficulty += 1
        else:
            row["difficulty"] = row.get("difficulty", "") or "未知"
            unmatched_difficulty += 1

    save_path = Path(output_path) if output_path else csv_path
    with save_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print("\n=== metrics_by_case.csv update result ===")
    print(f"[INFO] Correctness matched cases: {matched_score}")
    print(f"[INFO] Correctness unmatched cases: {unmatched_score}")
    print(f"[INFO] difficulty matched cases: {matched_difficulty}")
    print(f"[INFO] difficulty unmatched cases: {unmatched_difficulty}")
    print(f"[INFO] written to: {save_path}")

    return save_path


def load_metrics_by_case_rows(metrics_by_case_csv_path: str):
    csv_path = Path(metrics_by_case_csv_path)
    if not csv_path.exists():
        raise FileNotFoundError(f"File not found: {csv_path}")

    with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        return list(reader)


def aggregate_rows_to_summary(rows, difficulty_name=None):
    grouped = defaultdict(list)
    for row in rows:
        exp = row.get("experiment")
        if exp:
            grouped[exp].append(row)

    experiments_summary = []
    ordered_experiments = ["OURS", "woIU", "woKR", "IOP"]

    for exp in ordered_experiments:
        exp_rows = grouped.get(exp, [])
        if not exp_rows:
            continue

        exec_vals = [safe_float(r.get("Executability")) for r in exp_rows]
        nmr_vals = [safe_float(r.get("NMR")) for r in exp_rows]
        emr_vals = [safe_float(r.get("EMR")) for r in exp_rows]
        ts_vals = [safe_float(r.get("TS")) for r in exp_rows]
        corr_vals = [safe_float(r.get("CorrectnessScore")) for r in exp_rows]

        debugging_rates = compute_debugging_at_k(exp_rows, ks=(0, 1, 2))

        nmr_vals = [v for v in nmr_vals if v is not None and v != -1.0]
        emr_vals = [v for v in emr_vals if v is not None and v != -1.0]
        ts_vals = [v for v in ts_vals if v is not None and v != -1.0]

        experiments_summary.append({
            "experiment": exp,
            "count": len(exp_rows),
            "Executability": mean_or_none([v for v in exec_vals if v is not None]),
            "NMR_mean": mean_or_none(nmr_vals),
            "EMR_mean": mean_or_none(emr_vals),
            "TS_mean": mean_or_none(ts_vals),
            "Correctness": mean_or_none([v for v in corr_vals if v is not None]),
            "debugging@0": debugging_rates["debugging@0"],
            "debugging@1": debugging_rates["debugging@1"],
            "debugging@2": debugging_rates["debugging@2"],
        })

    return {
        "difficulty": difficulty_name,
        "experiments": experiments_summary
    }


def build_summary_by_difficulty(
    metrics_by_case_csv_path: str,
    output_path: str,
    run_dir: str,
):
    rows = load_metrics_by_case_rows(metrics_by_case_csv_path)

    overall_summary = aggregate_rows_to_summary(rows, difficulty_name="overall")

    by_difficulty_rows = defaultdict(list)
    for row in rows:
        difficulty = row.get("difficulty", "") or "未知"
        by_difficulty_rows[difficulty].append(row)

    result = {
        "run_dir": run_dir,
        "overall": overall_summary,
        "by_difficulty": {}
    }

    for difficulty in ["简单", "中等", "困难", "未知"]:
        subset = by_difficulty_rows.get(difficulty, [])
        result["by_difficulty"][difficulty] = aggregate_rows_to_summary(
            subset,
            difficulty_name=difficulty
        )

    out_path = Path(output_path)
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print("\n=== metrics_summary_by_difficulty.json generated ===")
    print(f"[INFO] written to: {out_path}")


def main():
    # ====== Change these to your actual paths ======
    dir_base ="20260428_130834_llama-3.1-8b-Instruct"
    summary_by_case_path = r"benchmarks_results/"+dir_base+"/correctness_judgements/summary_by_case_gpt55.jsonl"
    metrics_summary_path = r"benchmarks_results/"+dir_base+"/metrics_summary.json"
    metrics_by_case_csv_path = r"benchmarks_results/"+dir_base+"/metrics_by_case.csv"
    benchmark_meta_path = "data_json/benchmark_with_dag_rebalanced_v4.json"

    # If None, overwrite the original file; you can also set a new filename
    metrics_summary_output = None
    metrics_by_case_output = None
    summary_by_difficulty_output = r"benchmarks_results/"+dir_base+"/metrics_summary_by_difficulty.json"

    # Read summary_by_case.jsonl
    case_score_map, exp_scores_map = load_correctness_from_summary(summary_by_case_path)

    # Read difficulty metadata
    _, case_meta_map = load_case_difficulty_map(benchmark_meta_path)

    # Update metrics_summary.json
    update_metrics_summary(
        metrics_summary_path=metrics_summary_path,
        exp_scores_map=exp_scores_map,
        output_path=metrics_summary_output,
        normalize_to_01=False,  # Keep False so the summary also uses a 10-point scale
    )
    # Update metrics_by_case.csv: fill both correctness and difficulty
    updated_csv_path = update_metrics_by_case_csv(
        metrics_by_case_csv_path=metrics_by_case_csv_path,
        case_score_map=case_score_map,
        case_meta_map=case_meta_map,
        output_path=metrics_by_case_output,
        score_column="CorrectnessScore",
        normalize_score=False,
    )

    # Generate summary stratified by difficulty
    build_summary_by_difficulty(
        metrics_by_case_csv_path=str(updated_csv_path),
        output_path=summary_by_difficulty_output,
        run_dir=r"benchmarks_results/"+dir_base,
    )
    print("\nAll updates completed.")

if __name__ == "__main__":
    main()