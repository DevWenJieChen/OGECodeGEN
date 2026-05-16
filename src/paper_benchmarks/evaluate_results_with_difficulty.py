import ast
import csv
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Set
from collections import Counter
import itertools


# =========================
# Configuration section argparse Note: see the related implementation logic.
# =========================
RUN_DIR = Path("benchmarks_results/20260428_022501_llama-3.3-70b-instruct")  # TODO:  run_id Note: see the related implementation logic.
EXPERIMENTS = ["OURS", "woIU", "woKR", "IOP"]          # TODO: Adjust as needed
OUT_SUMMARY_JSON = "metrics_summary.json"
OUT_CASE_CSV = "metrics_by_case.csv"
BENCHMARK_META_PATH = Path("data_json/benchmark_with_dag_rebalanced.json")

EXPORT_DOT = True
DOT_DIRNAME = "graphs_dot"
EXPORT_DOT_ONLY_FAILED = False

EDGE_WEIGHT = 0.7                 # Sim = w*F1_E + (1-w)*F1_V
BRUTE_FORCE_MATCH_LIMIT = 7       # Use exhaustive optimal matching when min(m,n) <= 7; otherwise use greedy matching
# Missing-value placeholder for Step3 (generated code cannot form a DAG)
MISSING_METRIC = -1.0
# Depth-binning bucket width (A1)
DEPTH_BIN_SIZE = 3

# =========================
# Summary utilities: exclude missing values
# =========================
def is_valid_metric(x: Any, missing: float = MISSING_METRIC) -> bool:
    return isinstance(x, (int, float)) and x != missing

def mean_excluding_missing(values: List[Any], missing: float = MISSING_METRIC) -> Optional[float]:
    xs = [float(v) for v in values if is_valid_metric(v, missing=missing)]
    return (sum(xs) / len(xs)) if xs else None


def load_case_meta(meta_path: Path) -> Dict[str, Dict[str, Any]]:
    """
    Read benchmark metadata and build a case_id -> metadata map.
    Currently, difficulty is used at minimum; task_type/lang are also retained as fallbacks.

    """
    if not meta_path.exists():
        print(f"[WARN] benchmark meta not found: {meta_path}")
        return {}

    arr = json.loads(meta_path.read_text(encoding="utf-8"))
    case_meta = {}
    for item in arr:
        cid = item.get("case_id")
        if not cid:
            continue
        case_meta[cid] = {
            "difficulty": item.get("difficulty") or "未知",
            "task_type": item.get("task_type"),
            "lang": item.get("lang"),
        }
    return case_meta

# =========================
# Utility: support JSON / Python dict strings
# =========================
def parse_maybe_json_or_pyobj(x: Any) -> Any:
    """
    Support:
    - Already parsed dict/list objects
    - JSON strings
    - Python literal strings (single-quoted dicts), commonly seen in verify_report/logs
    Return None when parsing fails.

    """
    if x is None:
        return None
    if isinstance(x, (dict, list)):
        return x
    if not isinstance(x, str):
        return None

    s = x.strip()
    if not s:
        return None

    # 1) JSON
    try:
        return json.loads(s)
    except Exception:
        pass

    # 2) Python literal Note: see the related implementation logic.
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
    vr = parse_maybe_json_or_pyobj(verify_report)
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

# ==========================================================
# Step 2: DAG JSON -> Graph (build nodes/edges)
# ==========================================================
@dataclass
class Graph:
    """
    Minimal graph structure:
    - node_label: node_id -> functionName
    - edges: (src_id, dst_id), representing a data dependency where src output is consumed by dst (src -> dst)

    """
    node_label: Dict[str, str]
    edges: List[Tuple[str, str]]


def extract_dag_dict_from_any(dag_any: Any) -> Optional[Dict[str, Any]]:
    """
    Normalize the input into a dag_dict: {"0": {...}, "1": {...}, ...}.
    The dag may be:
    - wrapper: {"dag":"{...}", "layerName":..., ...}
    - direct dag_dict: {"0":..., "1":...}
    - string form (JSON/Python literal)

    Therefore, this function does two steps:
    1) Parse dag_any into an object first (supporting JSON / Python literals).
    2) If it is a wrapper, parse wrapper["dag"] again to obtain the real dag_dict.

    Return:
    - Success: dict whose keys are numeric strings ("0", "1", ...).
    - Failure: None.

    """
    obj = parse_maybe_json_or_pyobj(dag_any)
    if obj is None:
        return None

    # A) Already a dag_dict (keys are "0", "1", ...)
    if isinstance(obj, dict) and obj and all(isinstance(k, str) and k.isdigit() for k in obj.keys()):
        return obj

    # B) wrapper，contains a dag field
    if isinstance(obj, dict) and "dag" in obj:
        inner = parse_maybe_json_or_pyobj(obj.get("dag"))
        if isinstance(inner, dict) and inner and all(isinstance(k, str) and k.isdigit() for k in inner.keys()):
            return inner

    return None


def dag_dict_to_graph(dag_dict: Dict[str, Any], root_key: str = "0") -> Graph:
    """
    Convert dag_dict to Graph.
    Use depth-first traversal.

    Key semantics relied on by the downstream structure metrics:
    - root_key="0" is the main expression (main output).
    - Top-level keys "1", "2", ... are extracted common subexpressions used to avoid duplicate computation.
    - Inside the main body, {"valueReference":"k"} means "reuse the result of dag_dict[k]".
      -> During parsing, jump to dag_dict[k] and connect its produced invocation node to the current parent node.
    - Each {"functionInvocationValue": {...}} is treated as an operator invocation node.
    - Edge direction: child invocation (producer) -> parent invocation (consumer).

    Goal of this version:
    - Build the graph structure first and count nodes/edges, making parser validation easier.
    - Step3 then computes NMR/EMR/GED/TS on this basis.

    """
    node_label: Dict[str, str] = {}
    edges: Set[Tuple[str, str]] = set()
    # Use the key itself as node_id (for example, "3") to ensure reference reuse.
    ref_root_id: Dict[str, Optional[str]] = {}

    built_ref: Set[str] = set()
    used_ids = set()

    def alloc_id(path: str) -> str:
        """
        Allocate a stable and readable node_id for an invocation node.
        For example, a node from a path such as 0.coverage.arguments.coverage is easy to debug.

        Design notes:
        - node_id is not used for matching; it only uniquely identifies nodes and builds edges.
        - Using the traversal path makes it easy to locate where the node came from in the DAG.
        - If a conflict occurs, append suffix #i to ensure uniqueness.

        """
        if path not in used_ids:
            used_ids.add(path)
            return path
        i = 1
        while f"{path}#{i}" in used_ids:
            i += 1
        nid = f"{path}#{i}"
        used_ids.add(nid)
        return nid

    # top-level key  node_id Note: see the related implementation logic.
    def build_top_key(key: str) -> Optional[str]:
        """
        Build the invocation subgraph for one top-level key exactly once.
        Return the root invocation node id for that key (fixed as the key); return None if it is not an invocation.

        """
        if key not in dag_dict:
            return None
        # [MOD1] Avoid duplication (including during recursion)
        if key in ref_root_id:
            return ref_root_id[key]
        if key in built_ref:
            return ref_root_id.get(key)  # During recursion, return the current record first (possibly None)
        built_ref.add(key)

        obj = dag_dict.get(key)

        # [ADD] If the top-level key is not an invocation (constant/array/argRef/etc.), do not include it in the graph
        if not (isinstance(obj, dict) and "functionInvocationValue" in obj):
            ref_root_id[key] = None
            return None

        # Only an invocation is represented as a node (node_id=key)
        ref_root_id[key] = key
        walk(obj, parent=None, path=key, force_id=key)  # Keep your existing parameters/implementation unchanged
        return key

    def walk(val: Any, parent: Optional[str], path: str,  force_id: Optional[str] = None) -> None:
        """
        Recursively traverse any DAG substructure. When an invocation is encountered:
        1) Create a node (label=functionName).
        2) If parent exists, add a dependency edge: child -> parent.
        3) Continue traversing arguments to find deeper invocations/valueReference entries.

        During recursive traversal:
        - invocation -> create node + add edge + recursively traverse arguments.
        - valueReference -> ensure k's subgraph is built (build_top_key), and add only one edge: k_root -> parent.
        - arrayValue -> traverse values.
        - constant/argumentReference and similar leaf values -> return.

        Parameters:
        - val: current DAG substructure (dict/list/constant wrapper/etc.).
        - parent: node_id of the parent invocation (if current val produces an invocation, it is consumed by parent).
        - path: current traversal path used to build a readable node_id.

        """
        if val is None:
            return

        # functionDefinitionValue
        if isinstance(val, dict) and "functionDefinitionValue" in val:
            fdv = val.get("functionDefinitionValue")
            if isinstance(fdv, dict):
                body = fdv.get("body")
                # Expand only when body is a top-level key (such as "2"); otherwise ignore it (no more complex parsing)
                if isinstance(body, str) and body in dag_dict:
                    rid = build_top_key(body)
                    # walk(dag_dict.get(body), parent, f"{path}.fnDef({body})")
                    if rid is not None and parent is not None:
                        edges.add((rid, parent))
            return


        # -------------------------
        # 1) valueReference：Reference a top-level key (common subexpression)
        # -------------------------
        if isinstance(val, dict) and "valueReference" in val:
            ref_key = str(val["valueReference"])

            # it should not create a graph node or an edge; otherwise DOT will contain ghost nodes such as "1".
            ref_obj = dag_dict.get(ref_key)
            if not (isinstance(ref_obj, dict) and "functionInvocationValue" in ref_obj):
                return  # Treat it as a leaf value (constant-pool reference) and return directly
            rid = build_top_key(ref_key)
            if rid is not None and parent is not None:
                edges.add((rid, parent))
            return

        # functionInvocationValue：create node + traverse arguments
        if isinstance(val, dict) and "functionInvocationValue" in val:
            fiv = val.get("functionInvocationValue")
            if not isinstance(fiv, dict):
                return
            fn = fiv.get("functionName")
            if not isinstance(fn, str) or not fn:
                return

            # Create the node, actually using force_id (fixed as values such as "3" for top-level keys)
            nid = force_id if force_id else alloc_id(path)
            node_label[nid] = fn

            # Add dependency edge: child -> parent (the child computation result is consumed by the parent)
            if parent is not None:
                edges.add((nid, parent))

            # Continue traversing arguments; nested invocations or valueReference entries are found here
            args = fiv.get("arguments", {})
            if isinstance(args, dict):
                for k, v in args.items():
                    walk(v, nid, f"{path}.{k}")
            return

        # -------------------------
        # 3) arrayValue：array parameter wrapper
        # -------------------------
        if isinstance(val, dict) and "arrayValue" in val:
            av = val.get("arrayValue")
            if isinstance(av, dict) and isinstance(av.get("values"), list):
                for i, it in enumerate(av["values"]):
                    walk(it, parent, f"{path}.arr[{i}]")
            return

        # -------------------------
        # 4) constantValue：constant wrapper
        # -------------------------
        if isinstance(val, dict) and "constantValue" in val:
            return

        # 4.1) argumentReference：variable/argument reference (exists in Java; treated as a leaf during graph building)
        if isinstance(val, dict) and "argumentReference" in val:
            return

        # -------------------------
        # 5) ordinary dict/list: tolerant traversal
        # -------------------------
        if isinstance(val, dict):
            for k, v in val.items():
                walk(v, parent, f"{path}.{k}")
            return

        # list: recurse
        if isinstance(val, list):
            for i, it in enumerate(val):
                walk(it, parent, f"{path}[{i}]")
            return

    build_top_key(root_key)
    return Graph(node_label=node_label, edges=list(edges))

# =========================
# Reserved: infer graph from code on failure (LLM)
# =========================
def infer_graph_from_code_ast(code: Optional[str], rec: Dict[str, Any]) -> Tuple[List[Graph], str]:
    """
    Use AST to statically extract multiple DAGs from OGE code: List[Graph].
    """
    def strip_code_fence(s: str) -> str:
        if not isinstance(s, str):
            return s
        s = s.strip()
        s = re.sub(r"^\s*```[a-zA-Z0-9_+-]*\s*\n", "", s)
        s = re.sub(r"\n\s*```\s*$", "", s)
        return s.strip()

    def dedup_output_groups(groups: List[Set[str]]) -> List[Set[str]]:
        seen = set()
        out = []
        for g in groups:
            key = tuple(sorted(g))
            if key not in seen:
                seen.add(key)
                out.append(g)
        return out

    def infer_obj_type_from_label_local(label: Optional[str]) -> str:
        if not label:
            return "Unknown"
        if label == "Service.getCoverage":
            return "Coverage"
        if label == "Service.getCoverageCollection":
            return "CoverageCollection"
        if label == "Service.getFeature":
            return "Feature"
        if label.startswith("CoverageCollection."):
            return "CoverageCollection"
        if label.startswith("Coverage."):
            return "Coverage"
        if label.startswith("FeatureCollection."):
            return "FeatureCollection"
        if label.startswith("Feature."):
            return "Feature"
        if label.startswith("Geometry."):
            return "Geometry"
        return "Unknown"

    def styles_label_for_type_local(obj_type: Optional[str]) -> str:
        if obj_type in {"Coverage", "CoverageCollection", "Feature", "FeatureCollection"}:
            return f"{obj_type}.addStyles"
        return "Unknown.addStyles"

    def export_label_for_type_local(obj_type: Optional[str]) -> str:
        if obj_type in {"Coverage", "CoverageCollection", "Feature", "FeatureCollection"}:
            return f"{obj_type}.export"
        return "Coverage.export"

    def regex_fallback_extract_graphs(raw_code: str) -> Tuple[List[Graph], str]:
        lines = [ln.strip() for ln in raw_code.splitlines() if ln.strip()]
        node_label_fb: Dict[str, str] = {}
        edges_fb: List[Tuple[str, str]] = []
        env_fb: Dict[str, str] = {}
        output_roots_fb: List[Set[str]] = []
        seq = 0

        def new_node(label: str, upstreams: Optional[List[str]] = None) -> str:
            nonlocal seq
            seq += 1
            nid = f"fb_{seq}"
            node_label_fb[nid] = label
            if upstreams:
                for u in upstreams:
                    if u in node_label_fb:
                        edges_fb.append((u, nid))
            return nid

        def add_output_root(nid: Optional[str]) -> None:
            if nid and nid in node_label_fb:
                output_roots_fb.append({nid})

        def bind(lhs: Optional[str], nid: Optional[str]) -> None:
            if lhs and nid:
                env_fb[lhs] = nid

        for ln in lines:
            # Remove comments
            if "#" in ln:
                ln = ln.split("#", 1)[0].strip()
            if not ln:
                continue

            lhs = None
            rhs = ln
            m_assign = re.match(r"^([A-Za-z_]\w*)\s*=\s*(.+)$", ln)
            if m_assign:
                lhs = m_assign.group(1)
                rhs = m_assign.group(2).strip()

            # service.getProcess("X").execute(...)
            m = re.search(r'service\.getProcess\(\s*["\']([^"\']+)["\']\s*\)\.execute\(', rhs)
            if m:
                label = m.group(1)
                upstreams = [env_fb[v] for v in re.findall(r"\b([A-Za-z_]\w*)\b", rhs) if v in env_fb]
                nid = new_node(label, upstreams)
                bind(lhs, nid)
                continue

            # proc.execute(...)
            m = re.search(r'([A-Za-z_]\w*)\.execute\(', rhs)
            if m:
                proc_var = m.group(1)
                # Do not resolve proc_env strictly here; fallback simply skips it
                # Let it continue matching other rules below
                pass

            # service.getCoverage / getCoverageCollection / getFeature
            if re.search(r"\bservice\.getCoverage\s*\(", rhs):
                nid = new_node("Service.getCoverage")
                bind(lhs, nid)
                continue

            if re.search(r"\bservice\.getCoverageCollection\s*\(", rhs):
                nid = new_node("Service.getCoverageCollection")
                bind(lhs, nid)
                continue

            if re.search(r"\bservice\.getFeature\s*\(", rhs):
                nid = new_node("Service.getFeature")
                bind(lhs, nid)
                continue

            # styles / style
            # m = re.search(r'([A-Za-z_]\w*)\.(styles|style)\s*\(', rhs)
            m = re.search(r'([A-Za-z_]\w*)\.(styles)\s*\(', rhs)
            if m:
                base_var = m.group(1)
                base_node = env_fb.get(base_var)
                base_label = node_label_fb.get(base_node) if base_node else None
                obj_type = infer_obj_type_from_label_local(base_label)
                nid = new_node(styles_label_for_type_local(obj_type), [base_node] if base_node else None)
                bind(lhs, nid)

                # The same line also contains getMap/export/log
                if ".getMap(" in rhs:
                    add_output_root(nid)
                elif ".export(" in rhs:
                    enid = new_node(export_label_for_type_local(obj_type), [nid])
                    add_output_root(enid)
                elif ".log(" in rhs:
                    lnid = new_node("Service.printString", [nid])
                    add_output_root(lnid)
                continue

            # Variable directly calls getMap / export / log
            m = re.search(r'([A-Za-z_]\w*)\.getMap\(', rhs)
            if m:
                add_output_root(env_fb.get(m.group(1)))
                continue

            m = re.search(r'([A-Za-z_]\w*)\.export\(', rhs)
            if m:
                base_var = m.group(1)
                base_node = env_fb.get(base_var)
                base_label = node_label_fb.get(base_node) if base_node else None
                obj_type = infer_obj_type_from_label_local(base_label)
                enid = new_node(export_label_for_type_local(obj_type), [base_node] if base_node else None)
                add_output_root(enid)
                continue

            m = re.search(r'([A-Za-z_]\w*)\.log\(', rhs)
            if m:
                base_var = m.group(1)
                base_node = env_fb.get(base_var)
                lnid = new_node("Service.printString", [base_node] if base_node else None)
                add_output_root(lnid)
                continue

        if not node_label_fb:
            return [], "code_regex_no_nodes_manual_review"

        if not output_roots_fb:
            return [Graph(node_label=node_label_fb, edges=edges_fb)], "code_regex_partial_no_output_manual_review"

        output_roots_fb = dedup_output_groups(output_roots_fb)

        pred_fb: Dict[str, Set[str]] = {}
        for src, dst in edges_fb:
            pred_fb.setdefault(dst, set()).add(src)

        def build_subgraph(roots: Set[str]) -> Optional[Graph]:
            keep: Set[str] = set()
            stack = list(roots)
            while stack:
                cur = stack.pop()
                if cur in keep:
                    continue
                keep.add(cur)
                stack.extend(pred_fb.get(cur, set()))
            sub_nodes = {nid: lbl for nid, lbl in node_label_fb.items() if nid in keep}
            sub_edges = [(s, d) for (s, d) in edges_fb if s in keep and d in keep]
            if not sub_nodes:
                return None
            return Graph(node_label=sub_nodes, edges=sub_edges)

        graphs_fb: List[Graph] = []
        for roots in output_roots_fb:
            g = build_subgraph(roots)
            if g is not None:
                graphs_fb.append(g)

        if not graphs_fb:
            return [], "code_regex_failed_manual_review"

        return graphs_fb, f"code_regex_partial_multi(n={len(graphs_fb)})"

    # -------------------------
    # 0) Input preprocessing
    # -------------------------
    if not code or not isinstance(code, str) or not code.strip():
        return [], "code_ast_no_code"

    code = strip_code_fence(code)
    if not code:
        return [], "code_ast_empty_after_strip"

    # -------------------------
    # 1) Primary AST path
    # -------------------------
    try:
        tree = ast.parse(code)
    except SyntaxError:
        # AST failure -> lightweight fallback
        return regex_fallback_extract_graphs(code)

    node_label: Dict[str, str] = {}
    edges: Set[Tuple[str, str]] = set()

    env: Dict[str, Set[str]] = {}
    type_env: Dict[str, str] = {}
    proc_env: Dict[str, str] = {}
    output_groups: List[Set[str]] = []

    node_seq = 0

    def mk_info(
        producers: Optional[Set[str]] = None,
        obj_type: Optional[str] = None,
        proc_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        return {
            "producers": set() if producers is None else set(producers),
            "obj_type": obj_type,
            "proc_id": proc_id,
        }

    def alloc_node_id() -> str:
        nonlocal node_seq
        node_seq += 1
        return f"ast_{node_seq}"

    def add_node(label: str, upstreams: Optional[Set[str]] = None) -> str:
        nid = alloc_node_id()
        node_label[nid] = label
        if upstreams:
            for src in upstreams:
                if src in node_label:
                    edges.add((src, nid))
        return nid

    def add_output_group(roots: Set[str]) -> None:
        roots2 = {r for r in roots if r in node_label}
        if roots2:
            output_groups.append(roots2)

    def const_str(node: ast.AST) -> Optional[str]:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        return None

    def get_call_first_string_arg(call: ast.Call) -> Optional[str]:
        if call.args:
            s = const_str(call.args[0])
            if s is not None:
                return s
        for kw in call.keywords:
            if kw.arg in {"process_id", "processID", "featureId", "featureID"}:
                s = const_str(kw.value)
                if s is not None:
                    return s
        return None

    def is_name(node: ast.AST, name: str) -> bool:
        return isinstance(node, ast.Name) and node.id == name

    def is_service_method_call(node: ast.Call, method_name: str) -> bool:
        return (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == method_name
            and is_name(node.func.value, "service")
        )

    def merge_infos(infos: List[Dict[str, Any]]) -> Dict[str, Any]:
        producers: Set[str] = set()
        obj_type: Optional[str] = None
        proc_id: Optional[str] = None
        for info in infos:
            producers |= info["producers"]
            if obj_type is None and info.get("obj_type"):
                obj_type = info["obj_type"]
            if proc_id is None and info.get("proc_id"):
                proc_id = info["proc_id"]
        return mk_info(producers, obj_type, proc_id)

    def analyze_expr(node: Optional[ast.AST]) -> Dict[str, Any]:
        if node is None:
            return mk_info()

        if isinstance(node, ast.Name):
            if node.id in proc_env:
                return mk_info(proc_id=proc_env[node.id])
            return mk_info(
                producers=env.get(node.id, set()),
                obj_type=type_env.get(node.id, "Unknown"),
            )

        if isinstance(node, ast.Constant):
            return mk_info()

        if isinstance(node, ast.List):
            return merge_infos([analyze_expr(e) for e in node.elts])

        if isinstance(node, ast.Tuple):
            return merge_infos([analyze_expr(e) for e in node.elts])

        if isinstance(node, ast.Set):
            return merge_infos([analyze_expr(e) for e in node.elts])

        if isinstance(node, ast.Dict):
            infos = []
            for k in node.keys:
                infos.append(analyze_expr(k))
            for v in node.values:
                infos.append(analyze_expr(v))
            return merge_infos(infos)

        if isinstance(node, ast.Subscript):
            return analyze_expr(node.value)

        if isinstance(node, ast.UnaryOp):
            return analyze_expr(node.operand)

        if isinstance(node, ast.BinOp):
            return merge_infos([analyze_expr(node.left), analyze_expr(node.right)])

        if isinstance(node, ast.BoolOp):
            return merge_infos([analyze_expr(v) for v in node.values])

        if isinstance(node, ast.Compare):
            infos = [analyze_expr(node.left)]
            infos.extend(analyze_expr(c) for c in node.comparators)
            return merge_infos(infos)

        if isinstance(node, ast.Call):
            if is_service_method_call(node, "getProcess"):
                process_id = get_call_first_string_arg(node)
                if process_id:
                    return mk_info(proc_id=process_id)
                return mk_info()

            if is_service_method_call(node, "getCoverage"):
                nid = add_node("Service.getCoverage")
                return mk_info({nid}, "Coverage")

            if is_service_method_call(node, "getCoverageCollection"):
                nid = add_node("Service.getCoverageCollection")
                return mk_info({nid}, "CoverageCollection")

            if is_service_method_call(node, "getFeature"):
                nid = add_node("Service.getFeature")
                return mk_info({nid}, "Feature")

            if isinstance(node.func, ast.Attribute):
                method = node.func.attr
                recv = node.func.value

                if method == "execute":
                    process_id: Optional[str] = None

                    if isinstance(recv, ast.Call) and is_service_method_call(recv, "getProcess"):
                        process_id = get_call_first_string_arg(recv)
                    elif isinstance(recv, ast.Name) and recv.id in proc_env:
                        process_id = proc_env[recv.id]

                    if process_id:
                        arg_infos = [analyze_expr(a) for a in node.args]
                        arg_infos.extend(analyze_expr(kw.value) for kw in node.keywords)
                        upstreams: Set[str] = set()
                        for info in arg_infos:
                            upstreams |= info["producers"]

                        nid = add_node(process_id, upstreams)
                        return mk_info({nid}, infer_obj_type_from_label_local(process_id))

                # Support style / styles
                if method == "styles": #method in {"styles", "style"}:
                    recv_info = analyze_expr(recv)
                    label = styles_label_for_type_local(recv_info.get("obj_type"))
                    nid = add_node(label, recv_info["producers"])
                    return mk_info({nid}, recv_info.get("obj_type", "Unknown"))

                if method == "getMap":
                    recv_info = analyze_expr(recv)
                    add_output_group(recv_info["producers"])
                    return recv_info

                if method == "export":
                    recv_info = analyze_expr(recv)
                    label = export_label_for_type_local(recv_info.get("obj_type"))
                    nid = add_node(label, recv_info["producers"])
                    add_output_group({nid})
                    return mk_info({nid}, recv_info.get("obj_type", "Unknown"))

                if method == "log":
                    recv_info = analyze_expr(recv)
                    nid = add_node("Service.printString", recv_info["producers"])
                    add_output_group({nid})
                    return mk_info({nid}, "Unknown")

                infos = [analyze_expr(recv)]
                infos.extend(analyze_expr(a) for a in node.args)
                infos.extend(analyze_expr(kw.value) for kw in node.keywords)
                return merge_infos(infos)

            infos = []
            if isinstance(node.func, ast.Name) and node.func.id in env:
                infos.append(mk_info(env[node.func.id], type_env.get(node.func.id, "Unknown")))
            elif isinstance(node.func, ast.Attribute):
                infos.append(analyze_expr(node.func.value))
            infos.extend(analyze_expr(a) for a in node.args)
            infos.extend(analyze_expr(kw.value) for kw in node.keywords)
            return merge_infos(infos)

        if isinstance(node, ast.Attribute):
            return analyze_expr(node.value)

        return mk_info()

    def bind_target(target: ast.AST, info: Dict[str, Any]) -> None:
        if not isinstance(target, ast.Name):
            return

        name = target.id
        env.pop(name, None)
        type_env.pop(name, None)
        proc_env.pop(name, None)

        if info.get("proc_id") and not info["producers"]:
            proc_env[name] = info["proc_id"]
            return

        env[name] = set(info["producers"])
        if info.get("obj_type"):
            type_env[name] = info["obj_type"]

    def analyze_stmt(stmt: ast.stmt) -> None:
        if isinstance(stmt, ast.Assign):
            info = analyze_expr(stmt.value)
            for t in stmt.targets:
                bind_target(t, info)
            return

        if isinstance(stmt, ast.AnnAssign):
            info = analyze_expr(stmt.value)
            bind_target(stmt.target, info)
            return

        if isinstance(stmt, ast.Expr):
            analyze_expr(stmt.value)
            return

        if isinstance(stmt, ast.Return):
            analyze_expr(stmt.value)
            return

        if isinstance(stmt, ast.If):
            env_before = {k: set(v) for k, v in env.items()}
            type_before = dict(type_env)
            proc_before = dict(proc_env)

            for s in stmt.body:
                analyze_stmt(s)
            env_body = {k: set(v) for k, v in env.items()}
            type_body = dict(type_env)
            proc_body = dict(proc_env)

            env.clear()
            env.update({k: set(v) for k, v in env_before.items()})
            type_env.clear()
            type_env.update(type_before)
            proc_env.clear()
            proc_env.update(proc_before)

            for s in stmt.orelse:
                analyze_stmt(s)
            env_else = {k: set(v) for k, v in env.items()}
            type_else = dict(type_env)
            proc_else = dict(proc_env)

            merged_env: Dict[str, Set[str]] = {}
            for k in set(env_body) | set(env_else):
                merged_env[k] = set(env_body.get(k, set())) | set(env_else.get(k, set()))

            merged_type: Dict[str, str] = {}
            for k in set(type_body) | set(type_else):
                t1 = type_body.get(k)
                t2 = type_else.get(k)
                merged_type[k] = t1 if t1 == t2 else (t1 or t2 or "Unknown")

            merged_proc: Dict[str, str] = {}
            for k in set(proc_body) | set(proc_else):
                p1 = proc_body.get(k)
                p2 = proc_else.get(k)
                if p1 and p1 == p2:
                    merged_proc[k] = p1

            env.clear()
            env.update(merged_env)
            type_env.clear()
            type_env.update(merged_type)
            proc_env.clear()
            proc_env.update(merged_proc)
            return

        if isinstance(stmt, ast.For):
            analyze_expr(stmt.iter)
            for s in stmt.body:
                analyze_stmt(s)
            for s in stmt.orelse:
                analyze_stmt(s)
            return

        return

    for stmt in tree.body:
        analyze_stmt(stmt)

    if not node_label:
        # AST succeeded but no nodes were found; mark for manual review
        return [], "code_ast_no_nodes_manual_review"

    output_groups[:] = dedup_output_groups(output_groups)

    if not output_groups:
        return [Graph(node_label=node_label, edges=sorted(edges))], "code_ast_ok_no_output"

    pred: Dict[str, Set[str]] = {}
    for src, dst in edges:
        pred.setdefault(dst, set()).add(src)

    def build_subgraph_from_roots(roots: Set[str]) -> Optional[Graph]:
        keep: Set[str] = set()
        stack = list(roots)
        while stack:
            cur = stack.pop()
            if cur in keep:
                continue
            keep.add(cur)
            stack.extend(pred.get(cur, set()))

        sub_nodes = {nid: lbl for nid, lbl in node_label.items() if nid in keep}
        sub_edges = [(s, d) for (s, d) in edges if s in keep and d in keep]

        if not sub_nodes:
            return None
        return Graph(node_label=sub_nodes, edges=sorted(sub_edges))

    graphs: List[Graph] = []
    for roots in output_groups:
        g = build_subgraph_from_roots(roots)
        if g is not None:
            graphs.append(g)

    if not graphs:
        return [], "code_ast_no_output_subgraph_manual_review"

    return graphs, f"code_ast_ok_multi(n={len(graphs)})"


# =========================
# Reserved: CorrectnessScore is time-consuming and handled by another implemented script; ignore it here
# =========================
def score_correctness_llm(rec: Dict[str, Any]) -> Tuple[Optional[float], str]:
    """
    - Inputs may include generated code / verify_report / data_ref / gold_code / gold_dag, etc.
    - Output a 0-10 score (or normalize before further processing).

    """
    return None, "todo"

# ==========================================================
# [ADD] Step3：DAG similarity (F1 version) + multi-DAG matching
# ==========================================================
# Compute node topological depth (shortest level, used for depth binning)
def compute_depths(g: Graph) -> Dict[str, int]:
    nodes = set(g.node_label.keys())
    indeg = {n: 0 for n in nodes}
    out = {n: [] for n in nodes}

    for src, dst in g.edges:
        if src in nodes and dst in nodes:
            out[src].append(dst)
            indeg[dst] += 1

    roots = [n for n, d in indeg.items() if d == 0]
    if not roots:
        # In abnormal cases (no source nodes), set all depths to 0
        return {n: 0 for n in nodes}

    INF = 10**9
    depth = {n: INF for n in nodes}
    for r in roots:
        depth[r] = 0

    from collections import deque
    q = deque(roots)
    indeg2 = dict(indeg)

    while q:
        u = q.popleft()
        du = depth[u] if depth[u] != INF else 0
        for v in out[u]:
            # Shortest-level relaxation
            if du + 1 < depth[v]:
                depth[v] = du + 1
            indeg2[v] -= 1
            if indeg2[v] == 0:
                q.append(v)

    for n in nodes:
        if depth[n] == INF:
            depth[n] = 0
    return depth

def graph_signature(g: Graph) -> Tuple[Counter, Counter]:
    """
    Convert a Graph into multiset representations:
    - V: Counter[functionName]
    - E: Counter[(src_fn, dst_fn, depth_bin_dst)]
    - E: Counter[(src_fn, dst_fn, depth_bin_dst)]

    """
    V = Counter(g.node_label.values())
    E = Counter()
    # depth binningNodesdepth and bucket it Note: see the related implementation logic.
    depth = compute_depths(g)
    for src, dst in g.edges:
        if src in g.node_label and dst in g.node_label:
            # E[(g.node_label[src], g.node_label[dst])] += 1
            src_fn = g.node_label[src]
            dst_fn = g.node_label[dst]
            d_dst = depth.get(dst, 0)
            b_dst = d_dst // DEPTH_BIN_SIZE  # coarse-grained level bucket
            E[(src_fn, dst_fn, b_dst)] += 1
    return V, E

def multiset_intersection_size(a: Counter, b: Counter) -> int:
    if not a or not b:
        return 0
    keys = set(a.keys()) & set(b.keys())
    return sum(min(a[k], b[k]) for k in keys)

def prf1_multiset(pred: Counter, gold: Counter) -> Tuple[float, float, float]:
    """
    Precision/Recall/F1 for multisets.
    Convention: if pred and gold are both empty, return 1.0.
    """
    inter = multiset_intersection_size(pred, gold)
    pred_sz = sum(pred.values())
    gold_sz = sum(gold.values())

    if pred_sz == 0 and gold_sz == 0:
        return 1.0, 1.0, 1.0
    if pred_sz == 0 or gold_sz == 0:
        return 0.0, 0.0, 0.0

    p = inter / pred_sz
    r = inter / gold_sz
    f1 = (2 * p * r / (p + r)) if (p + r) else 0.0
    return p, r, f1

def dag_pair_scores(p: Graph, g: Graph, w_edge: float = EDGE_WEIGHT) -> Tuple[float, float, float]:
    """
    Return (F1_V, F1_E, Sim).
    Sim = (w*F1_E + (1-w)*F1_V) * size_penalty.
    """
    pV, pE = graph_signature(p)
    gV, gE = graph_signature(g)

    _, _, f1_v = prf1_multiset(pV, gV)
    _, _, f1_e = prf1_multiset(pE, gE)

    sim = w_edge * f1_e + (1.0 - w_edge) * f1_v

    # size penalty: suppress degenerate/very short DAGs from gaining undeserved score
    eps = 1e-9
    size_p = float(sum(pE.values()))  # total count of multiset edges
    size_g = float(sum(gE.values()))
    if size_p == 0.0 and size_g == 0.0:
        pen = 1.0
    else:
        pen = min(size_p, size_g) / (max(size_p, size_g) + eps)

    sim = sim * pen
    return f1_v, f1_e, sim

def best_dag_matching(pred_graphs: List[Graph], gold_graphs: List[Graph],
                      w_edge: float = EDGE_WEIGHT,
                      brute_force_limit: int = BRUTE_FORCE_MATCH_LIMIT
                      ) -> Tuple[Optional[float], Optional[float], Optional[float], List[Tuple[int, int, float, float, float]]]:
    """
    Perform maximum-weight one-to-one matching for multiple DAGs, and normalize by /max(m,n) to penalize missing/redundant graphs.

    Return:
      TS_score  : Σsim / max(m,n)
      NMR_score : ΣF1_V / max(m,n)
      EMR_score : ΣF1_E / max(m,n)
      pairs     : [(i, j, F1_V, F1_E, Sim), ...]
    """
    m, n = len(pred_graphs), len(gold_graphs)
    if m == 0 and n == 0:
        return 1.0, 1.0, 1.0, []
    if m == 0 or n == 0:
        return 0.0, 0.0, 0.0, []

    # Precompute matrix
    M: List[List[Tuple[float, float, float]]] = [
        [dag_pair_scores(pred_graphs[i], gold_graphs[j], w_edge=w_edge) for j in range(n)]
        for i in range(m)
    ]

    k = min(m, n)
    denom = max(m, n)

    best_sum_sim = -1.0
    best_sum_v = 0.0
    best_sum_e = 0.0
    best_pairs: List[Tuple[int, int, float, float, float]] = []

    # Small scale: exhaustive optimum (optimal and interpretable)
    if k <= brute_force_limit:
        if m <= n:
            # Each pred chooses a different gold
            for cols in itertools.permutations(range(n), m):
                s_sim = s_v = s_e = 0.0
                for i in range(m):
                    f1_v, f1_e, sim = M[i][cols[i]]
                    s_sim += sim
                    s_v += f1_v
                    s_e += f1_e
                if s_sim > best_sum_sim:
                    best_sum_sim = s_sim
                    best_sum_v = s_v
                    best_sum_e = s_e
                    best_pairs = [(i, cols[i], *M[i][cols[i]]) for i in range(m)]
        else:
            # Each gold chooses a different pred
            for rows in itertools.permutations(range(m), n):
                s_sim = s_v = s_e = 0.0
                for j in range(n):
                    f1_v, f1_e, sim = M[rows[j]][j]
                    s_sim += sim
                    s_v += f1_v
                    s_e += f1_e
                if s_sim > best_sum_sim:
                    best_sum_sim = s_sim
                    best_sum_v = s_v
                    best_sum_e = s_e
                    best_pairs = [(rows[j], j, *M[rows[j]][j]) for j in range(n)]
    else:
        # Large scale: greedy fallback (simple and reproducible)
        used_i, used_j = set(), set()
        all_pairs = []
        for i in range(m):
            for j in range(n):
                f1_v, f1_e, sim = M[i][j]
                all_pairs.append((sim, i, j, f1_v, f1_e))
        all_pairs.sort(reverse=True, key=lambda x: x[0])

        best_sum_sim = best_sum_v = best_sum_e = 0.0
        for sim, i, j, f1_v, f1_e in all_pairs:
            if i in used_i or j in used_j:
                continue
            used_i.add(i)
            used_j.add(j)
            best_pairs.append((i, j, f1_v, f1_e, sim))
            best_sum_sim += sim
            best_sum_v += f1_v
            best_sum_e += f1_e
            if len(best_pairs) >= k:
                break

    TS_score = max(0.0, best_sum_sim) / denom
    NMR_score = max(0.0, best_sum_v) / denom
    EMR_score = max(0.0, best_sum_e) / denom
    return TS_score, NMR_score, EMR_score, best_pairs



# [ADD] Merge Graph objects for multiple DAGs and avoid node_id conflicts by prefixing each graph node_id/edge (only for DOT/size statistics, not Step3)
def merge_graphs(graphs: List[Graph], prefix_fmt: str = "d{idx}::") -> Graph:
    node_label: Dict[str, str] = {}
    edges: List[Tuple[str, str]] = []

    for i, g in enumerate(graphs):
        pref = prefix_fmt.format(idx=i)
        for nid, fn in g.node_label.items():
            node_label[pref + nid] = fn
        for src, dst in g.edges:
            edges.append((pref + src, pref + dst))

    return Graph(node_label=node_label, edges=edges)

# ==========================================================
# Parse pred: return a graph list (for multi-DAG matching)
# ==========================================================
def extract_pred_graphs(rec: Dict[str, Any]) -> Tuple[List[Graph], str]:
    """
     Priority for pred graph sources (minimal framework):
    1) If verify_report contains dag_json: build graphs directly from dag_json.
    2) Otherwise: infer graphs from code

    Return: (graph_or_none, reason)
    reason:
      - ok / dag_json_missing / verify_report_not_dict / dag_parse_failed
      - code_llm_todo / code_llm_no_code

    """
    result = rec.get("result", {}) or {}
    # 1) verify_report.dag_json Note: see the related implementation logic.
    vr = parse_maybe_json_or_pyobj(result.get("verify_report"))
    if isinstance(vr, dict):
        dag_json = vr.get("dag_json")
        # [MOD] dag_json  str  list[str] Note: see the related implementation logic.
        dag_items: List[Any] = []
        if isinstance(dag_json, list):
            dag_items = dag_json
        elif dag_json is not None:
            dag_items = [dag_json]
        if dag_items:
            graphs: List[Graph] = []
            failed = 0
            for one in dag_items:
                dag_dict = extract_dag_dict_from_any(one)
                if dag_dict is None:
                    failed += 1
                    continue
                graphs.append(dag_dict_to_graph(dag_dict))
            if not graphs:
                return [], f"dag_parse_failed_all(n={len(dag_items)})"
            if failed:
                return graphs, f"ok_multi(n={len(dag_items)},ok={len(graphs)},fail={failed})"
            return graphs, f"ok_multi(n={len(dag_items)})"

    # fallback: code->LLM（placeholder）
    ps = rec.get("pipeline_state", {}) or {}
    code = ps.get("code") or (result.get("code") if isinstance(result, dict) else None)
    graphs, r = infer_graph_from_code_ast(code, rec)
    if not graphs:
        return [], r
    return graphs, r

# ==========================================================
# Parse gold: return a graph list (for multi-DAG matching)
# ==========================================================
def extract_gold_graphs(rec: Dict[str, Any]) -> Tuple[List[Graph], str]:
    """
    The gold dag is stored in rec["case"]["target_dag"]; the dataset may contain placeholders.
    """
    case = rec.get("case", {}) or {}
    gold_raw = case.get("target_dag")
    if gold_raw is None:
        return [], "gold_missing"
    if isinstance(gold_raw, str) and "PLACEHOLDER" in gold_raw:
        return [], "gold_placeholder"

    gold_items: List[Any] = gold_raw if isinstance(gold_raw, list) else [gold_raw]

    graphs: List[Graph] = []
    failed = 0
    for one in gold_items:
        dag_dict = extract_dag_dict_from_any(one)
        if dag_dict is None:
            failed += 1
            continue
        graphs.append(dag_dict_to_graph(dag_dict))

    if not graphs:
        return [], f"gold_parse_failed_all(n={len(gold_items)})"

    if failed:
        return graphs, f"ok_multi(n={len(gold_items)},ok={len(graphs)},fail={failed})"
    return graphs, f"ok_multi(n={len(gold_items)})"


# [ADD] Graph -> DOT（Graphviz）
def graph_to_dot(g: Graph, title: str = "") -> str:
    """
    Export Graph(node_label, edges) as Graphviz DOT text.
    """
    lines = ["digraph G {"]
    lines.append("  rankdir=LR;")
    lines.append("  node [shape=box];")  # Use boxes uniformly (operator nodes are visually clearer)

    if title:
        # The title may contain quotes; replace them simply
        safe_title = title.replace('"', "'")
        lines.append(f'  label="{safe_title}"; labelloc=top; fontsize=20;')

    # Nodes：Display functionName
    for nid, fn in g.node_label.items():
        safe_fn = str(fn).replace('"', "'")
        lines.append(f'  "{nid}" [label="{safe_fn}"];')

    # Edges：src -> dst
    for src, dst in g.edges:
        lines.append(f'  "{src}" -> "{dst}";')

    lines.append("}")
    return "\n".join(lines)


def main():
    summary = {
        "run_dir": str(RUN_DIR),
        "experiments": [],
        "notes": {
            "structure_metrics": "Step3 implemented: per-DAG F1(V)/F1(E), depth-binned edge labels, size-penalized Sim for matching; aggregated by /max(m,n).",
            "correctness": "TODO: Step5 correctness scoring handled in another script.",
            "pred_graph": "pred_graph tries dag_json first (even if verify_ok=False), else fallback to code->AST.",
        }
    }

    csv_rows: List[Dict[str, Any]] = []
    case_meta_map = load_case_meta(BENCHMARK_META_PATH)

    for exp_name in EXPERIMENTS:
        exp_dir = RUN_DIR / exp_name
        if not exp_dir.is_dir():
            continue

        idx_path = exp_dir / "index.json"
        if not idx_path.is_file():
            continue

        idx = json.loads(idx_path.read_text(encoding="utf-8"))
        items = idx.get("items", []) or []

        # Step4: Executability
        total = 0
        ok = 0

        # [ADD] Step3 aggregation statistics
        nmr_vals: List[float] = []
        emr_vals: List[float] = []
        ts_vals: List[float] = []
        difficulty_counter: Counter = Counter()

        for it in items:
            case_id = it.get("case_id")
            if not case_id:
                continue
            rec_path = exp_dir / f"{case_id}.json"
            if not rec_path.is_file():
                continue

            rec = json.loads(rec_path.read_text(encoding="utf-8"))
            total += 1

            result_obj = rec.get("result", {}) or {}
            verify_ok_raw = result_obj.get("verify_ok")
            verify_report_raw = result_obj.get("verify_report")
            max_fix_num = result_obj.get("max_fix_num")

            dag_json_state = get_dag_json_state(verify_report_raw)
            executability_ok = is_effective_executable(verify_ok_raw, verify_report_raw)

            if executability_ok:
                ok += 1

            # Step2：Parse pred/gold graphs (first count nodes/edges to validate parser correctness)
            pred_g, pred_reason = extract_pred_graphs(rec)
            gold_g, gold_reason = extract_gold_graphs(rec)

            pred_dag_count = len(pred_g)
            gold_dag_count = len(gold_g)

            # Step3：Structure metric calculation (F1 version + multi-DAG matching)
            NMR = MISSING_METRIC
            EMR = MISSING_METRIC
            TS = MISSING_METRIC
            ApproxGED = None  # TODO（add proxy-GED later if needed）

            vr_obj = parse_maybe_json_or_pyobj(verify_report_raw)
            dag_json_explicit_empty = (
                    isinstance(vr_obj, dict)
                    and "dag_json" in vr_obj
                    and isinstance(vr_obj.get("dag_json"), list)
                    and len(vr_obj.get("dag_json")) == 0
            )

            if gold_g:
                if pred_g:
                    # If a pred graph exists, compute normally
                    TS, NMR, EMR, _pairs = best_dag_matching(pred_g, gold_g)
                else:
                    # When pred is empty, distinguish two cases:
                    # 1) verify_ok=True  dag_json=[] -> pseudo-pass; score as 0 Note: see the related implementation logic.
                    # 2) Other cases (e.g., verification failed and AST also failed) -> keep -1
                    if verify_ok_raw  is True and dag_json_explicit_empty:
                        TS, NMR, EMR = 0.0, 0.0, 0.0

                nmr_vals.append(NMR)
                emr_vals.append(EMR)
                ts_vals.append(TS)
            else:
                # Only missing/broken gold is truly unevaluable
                nmr_vals.append(NMR)
                emr_vals.append(EMR)
                ts_vals.append(TS)

            def _parse_n_from_reason(reason: str) -> Optional[int]:
                if not isinstance(reason, str):
                    return None
                m = re.search(r"n=(\d+)", reason)
                if m:
                    return int(m.group(1))
                if reason.startswith("ok"):
                    return 1
                return None

            # Step5 Correctness placeholder interface
            corr_score, _corr_reason = score_correctness_llm(rec)

            # Size statistics: to preserve old field semantics, still use the merged overall size
            pred_g_merged = merge_graphs(pred_g) if pred_g else None
            gold_g_merged = merge_graphs(gold_g) if gold_g else None

            case_obj = rec.get("case", {}) or {}
            case_id_final = case_obj.get("case_id", case_id)
            case_meta = case_meta_map.get(case_id_final, {})
            difficulty = case_obj.get("difficulty") or case_meta.get("difficulty") or "未知"
            task_type = case_obj.get("task_type") or case_meta.get("task_type")
            lang = case_obj.get("lang") or case_meta.get("lang")

            difficulty_counter[difficulty] += 1

            csv_rows.append({
                "experiment": exp_name,
                "case_id": case_id_final,
                "task_type": task_type,
                "lang": lang,
                "difficulty": difficulty,
                "verify_ok": verify_ok_raw,
                "Executability": 1 if executability_ok is True else 0,
                "max_fix_num": max_fix_num,
                "dag_json_state": dag_json_state,
                "executability_ok": executability_ok,
                "pred_dag_count": pred_dag_count,
                "gold_dag_count": gold_dag_count,
                "pred_nodes": len(pred_g_merged.node_label) if pred_g_merged  else None,
                "pred_edges": len(pred_g_merged.edges) if pred_g_merged  else None,
                "pred_graph_reason": pred_reason,

                "gold_nodes": len(gold_g_merged.node_label) if gold_g_merged else None,
                "gold_edges": len(gold_g_merged.edges) if gold_g_merged else None,
                "gold_graph_reason": gold_reason,

                # Step3 output
                "NMR": NMR,
                "EMR": EMR,
                "ApproxGED": ApproxGED,
                "TS": TS,

                # TODO Step5: correctness score (0-10)
                "CorrectnessScore": corr_score,
            })
            # [ADD] Export DOT (visualization records)
            if EXPORT_DOT:
                should_export = True
                if EXPORT_DOT_ONLY_FAILED:
                    pred_ok = isinstance(pred_reason, str) and pred_reason.startswith("ok")
                    should_export = (not executability_ok) or (not pred_ok)

                if should_export:
                    dot_dir = RUN_DIR / DOT_DIRNAME / exp_name
                    dot_dir.mkdir(parents=True, exist_ok=True)

                    # pred graph
                    if pred_g_merged  is not None:
                        dot_text = graph_to_dot(pred_g_merged , title=f"{exp_name}:{case_id}:pred")
                        (dot_dir / f"{case_id}.pred.dot").write_text(dot_text, encoding="utf-8")

                    # gold graph (optional; useful for comparison)
                    if gold_g_merged  is not None:
                        dot_text = graph_to_dot(gold_g_merged , title=f"{exp_name}:{case_id}:gold")
                        (dot_dir / f"{case_id}.gold.dot").write_text(dot_text, encoding="utf-8")

        exec_rate = (ok / total) if total else 0.0

        summary["experiments"].append({
            "experiment": exp_name,
            "count": total,
            "Executability": exec_rate,
            "NMR_mean": mean_excluding_missing(nmr_vals),
            "EMR_mean": mean_excluding_missing(emr_vals),
            "TS_mean": mean_excluding_missing(ts_vals),

            # TODO Step5
            "Correctness": None,
            "difficulty_counts": {
                "简单": difficulty_counter.get("简单", 0),
                "中等": difficulty_counter.get("中等", 0),
                "困难": difficulty_counter.get("困难", 0),
                "未知": difficulty_counter.get("未知", 0),
            },
        })

    # Step6: Output summary.json
    (RUN_DIR / OUT_SUMMARY_JSON).write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )

    # Step6: Output by-case.csv
    csv_path = RUN_DIR / OUT_CASE_CSV
    fieldnames = [
        "experiment", "case_id", "task_type", "lang", "difficulty",
        "verify_ok", "Executability", "max_fix_num", "dag_json_state", "executability_ok",
        "pred_dag_count", "gold_dag_count",
        "pred_nodes", "pred_edges", "pred_graph_reason",
        "gold_nodes", "gold_edges", "gold_graph_reason",
        "NMR", "EMR", "ApproxGED", "TS",
        "CorrectnessScore",
    ]
    with csv_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in csv_rows:
            w.writerow({k: r.get(k) for k in fieldnames})

    print(f"Saved: {RUN_DIR / OUT_SUMMARY_JSON}")
    print(f"Saved: {csv_path}")


if __name__ == "__main__":
    main()
