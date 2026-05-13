# src/modules/verify.py
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from typing import Dict, Any, Optional, List

from src.core.pipeline_state import PipelineState


# Subprocess execution template (concise):
# - Purpose: run generated code in an isolated process (assuming the oge package is installed) to avoid polluting or crashing the parent process.
# - Success: a normal exit (0) outputs the dag string to the console, allowing the generated code's stdout to pass through unchanged (the parent process directly reads stdout).
# - Failure: output one JSON line (structured error information)
_WRAPPER = r"""
import sys
import json
import traceback

def emit_err(payload):
    # Convention: on error, output one JSON line (used by the parent process for structured storage)
    sys.stdout.write(json.dumps(payload, ensure_ascii=False))
    sys.stdout.write("\n")
    sys.stdout.flush()

try:
    code_path = sys.argv[1]
    with open(code_path, "r", encoding="utf-8") as f:
        src = f.read()

    # 1) Compile: explicitly catches SyntaxError
    compiled = compile(src, code_path, "exec")

    # 2) Execute: generated code may print content such as print(dag=<<...>>).
    #    Note: stdout is not intercepted or redirected here; keep it simple and let the generated code print as-is.
    g = {"__name__": "__main__"}
    exec(compiled, g, g)

    # Success: exit with 0 directly (the generated code's stdout has already been emitted)
    sys.exit(0)

except SyntaxError as e:
    emit_err({
        "ok": False,
        "stage": "syntax",
        "error": {
            "type": "SyntaxError",
            "message": e.msg,
            "lineno": e.lineno,
            "offset": e.offset,
            "text": (e.text or "").strip(),
            "traceback": traceback.format_exc(),
        }
    })
    sys.exit(10)

except Exception as e:
    emit_err({
        "ok": False,
        "stage": "runtime",
        "error": {
            "type": type(e).__name__,
            "message": str(e),
            "traceback": traceback.format_exc(),
        }
    })
    sys.exit(20)
"""


def _format_error_json(
    stage: str,
    exit_code: Optional[int],
    stderr: Optional[str] = None,
    wrapper_payload: Optional[Dict[str, Any]] = None,
    note: Optional[str] = None,
) -> str:
    """
    Return a JSON string for pls.verify_report when verification fails.
    """
    obj: Dict[str, Any] = {
        "ok": False,
        "stage": stage,
        "exit_code": exit_code,
        # "note": note,
        # "stderr": stderr,
        # "stdout": stdout,
    }
    # Prefer wrapper_payload because it is the most complete and avoids duplication.
    if isinstance(wrapper_payload, dict):
        err = (wrapper_payload.get("error") or {}) if wrapper_payload.get("ok") is False else {}
        if err:
            obj["err_type"] = err.get("type")
            obj["err_message"] = err.get("message")
            obj["err_traceback"] = err.get("traceback")

    # If wrapper_error is unavailable (for example, the wrapper itself crashed), fall back to stderr.
    if "err_traceback" not in obj and stderr:
        obj["stderr"] = stderr
    return json.dumps(obj, ensure_ascii=False, indent=2)


def _format_verify_json(
    ok: bool,
    stage: str,
    exit_code: Optional[int],
    stdout: Optional[str] = None,
    stderr: Optional[str] = None,
    wrapper_payload: Optional[Dict[str, Any]] = None,
    note: Optional[str] = None,
) -> str:
    if ok:
        # [MOD] stdout may contain multiple dag=<<...>> entries, so parse them uniformly as list[str].
        dags: List[str] = []
        space_params_payload = None
        # Non-greedy match for << >>
        # m = re.search(r"dag=<<(.+?)>>", stdout, flags=re.DOTALL)
        # if m:
        #     dag_payload = m.group(1)
        if stdout:
            dags = re.findall(r"dag=<<(.+?)>>", stdout, flags=re.DOTALL) or []

        m = re.search(r"spaceParams=<<(.+?)>>", stdout, flags=re.DOTALL)
        if m:
            space_params_payload = m.group(1)
        obj = {
            "ok": True,
            "stage": "ok",
            "exit_code": 0,
            # "oge_code": stdout or "",
            "dag_json": dags,
            "spaceparams_json": space_params_payload,
        }
        return json.dumps(obj, ensure_ascii=False, indent=2)

    # Failure: reuse the existing _format_error_json.
    return _format_error_json(
        stage=stage,
        exit_code=exit_code,
        stderr=stderr,
        wrapper_payload=wrapper_payload,
        note=note,
    )


def run(pls: PipelineState, timeout_s: int = 30) -> PipelineState:
    code = (pls.code or "").strip()
    pls.trace.setdefault("verify", {})
    if not code:
        pls.verify_ok = False
        pls.trace["verify"].update({
            "stage": "input",
            "exit_code": None,
            "stdout_len": 0,
            "stderr_len": 0,
        })
        pls.verify_report = _format_error_json(
            stage="input",
            exit_code=None,
            # stdout="",
            # stderr="",
            note="pls.code is empty",
        )
        return pls

    try:
        with tempfile.TemporaryDirectory() as td:
            code_path = os.path.join(td, "generated_code.py")
            wrapper_path = os.path.join(td, "verify_wrapper.py")

            with open(code_path, "w", encoding="utf-8") as f:
                f.write(code)

            with open(wrapper_path, "w", encoding="utf-8") as f:
                f.write(_WRAPPER)

            # sys.executable is the Python interpreter path. This is similar to python script.py arg1 arg2 arg3, where code_path is arg1.
            proc = subprocess.run(
                [sys.executable, wrapper_path, code_path],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_s,
            )

            stdout = (proc.stdout or "").strip()
            stderr = (proc.stderr or "").strip()

            pls.trace["verify"].update({
                "exit_code": proc.returncode,
                "stdout_len": len(stdout),
                "stderr_len": len(stderr),
            })

            # Success: assign stdout directly to verify_report.
            if proc.returncode == 0:
                pls.verify_ok = True
                pls.verify_report = _format_verify_json(ok=True, stage="ok", exit_code=0, stdout=stdout) # stdout
                # trace: supplementary information for the success state (do not parse content; only check whether the expected format exists).
                pls.trace["verify"].update({
                    "stage": "ok",
                    "has_dag_line": "dag=<<" in stdout,
                    "has_spaceparams_line": "spaceParams=<<" in stdout,
                })
                return pls

            # Failure: try to parse the last stdout line as the JSON error emitted by the wrapper.
            wrapper_payload = None
            if stdout:
                last_line = stdout.splitlines()[-1]
                try:
                    wrapper_payload = json.loads(last_line)
                except json.JSONDecodeError:
                    wrapper_payload = None

            stage = "unknown"
            if isinstance(wrapper_payload, dict) and wrapper_payload.get("ok") is False:
                stage = wrapper_payload.get("stage", "unknown")

            pls.verify_ok = False
            # trace: supplementary information for the failure state.
            pls.trace["verify"].update({
                "stage": stage,
            })
            pls.verify_report = _format_error_json(
                stage=stage,
                exit_code=proc.returncode,
                # stdout=stdout,
                stderr=stderr,
                wrapper_payload=wrapper_payload,
            )
            return pls

    except subprocess.TimeoutExpired:
        pls.verify_ok = False
        # trace: timeout state.
        pls.trace["verify"].update({
            "stage": "timeout",
            "exit_code": None,
            "stdout_len": 0,
            "stderr_len": 0,
        })
        pls.verify_report = _format_error_json(
            stage="timeout",
            exit_code=None,
            # stdout="",
            # stderr="",
            note=f"code verify timeout after {timeout_s}s",
        )
        return pls
