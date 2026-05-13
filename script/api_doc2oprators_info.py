from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional
from decimal import Decimal


from src.tools.config import load_config
# ====== Existing wrappers ======
# Adjust to your project path
from src.tools.model_clients import ChatLLMClient

# Adjust to your project path: prompt loading function you provided
from src.tools.prompt_loader import load, render


# =========================
# Runtime configuration, editable directly in the IDE
# =========================

INPUT_JSON_PATH = "data_json/paper_experiment/models_prefix_filtered.json"
OUTPUT_JSON_PATH = "data_json/paper_experiment/operator_info_all.json"

# Resumable execution: generates a progress file in the same directory to record processed item count
PROGRESS_PATH = OUTPUT_JSON_PATH + ".progress"
RESUME = True

# Prompt file paths relative to PROMPTS_DIR
SYSTEM_PROMPT_PATH = "pre_data/apidoc2opratorsInfo_system_prompt.md"
USER_PROMPT_PATH = "pre_data/apidoc2opratorsInfo_user_prompt.md"

# Batch size: number of items passed to the model at once; this is prompt-level batching, not SDK batching
BATCH_SIZE = 5

# Retry failed calls
MAX_RETRIES = 4
BACKOFF_BASE_S = 1.6

# Output format
OUTPUT_INDENT: Optional[int] = None  # For example, 2; None means compact output
OUTPUT_ENSURE_ASCII = False

# Whether to enable JSON repair by making an additional repair call when the model output is not strict JSON
ENABLE_JSON_REPAIR = True
JSON_REPAIR_PROMPT_PATH = "pre_data/apidoc2opratorsInfo_repair_prompt.md"

# LLM configuration
llm_cfg = load_config("config.yaml").get("llm_predata", {})

LLM_PROVIDER = llm_cfg.get("provider")
LLM_MODEL = llm_cfg.get("model", "gpt-5.2")
LLM_TEMPERATURE = float(llm_cfg.get("temperature", 0.3))
LLM_TIMEOUT_S = int(llm_cfg.get("timeout_s", 60))
LLM_API_KEY = llm_cfg.get("api_key", "")
LLM_BASE_URL = llm_cfg.get("base_url")


# =========================
# Utility: extract JSON from LLM output
# =========================

_JSON_START_RE = re.compile(r"[\[{]")


def _extract_first_json_block(text: str) -> str:
    """
    Extract the first block from the model output that looks like JSON.
    Strategy:
    - Find the first '[' or '{'
    - Try json.loads directly
    - If parsing fails, gradually truncate the tail until it can be parsed, preventing trailing explanatory text from breaking parsing
    """
    m = _JSON_START_RE.search(text)
    if not m:
        raise ValueError("No JSON start symbol found in model output '[' or '{'。")

    candidate = text[m.start():].strip()

    # Try directly first
    try:
        json.loads(candidate)
        return candidate
    except Exception:
        pass

    # Then gradually truncate the tail, trying within at most the last 20,000 characters
    min_end = max(len(candidate) - 20000, 0)
    for end in range(len(candidate), min_end, -1):
        snippet = candidate[:end].rstrip()
        try:
            json.loads(snippet)
            return snippet
        except Exception:
            continue

    raise ValueError("Unable to extract parseable JSON from model output.")


def _ensure_list_of_objects(x: Any) -> List[Dict[str, Any]]:
    """Ensure the output is list[dict]."""
    if not isinstance(x, list):
        raise ValueError(f"Expected output to be a JSON array (list), got: {type(x)}")
    for i, it in enumerate(x):
        if not isinstance(it, dict):
            raise ValueError(f"Expected output array item {i} to be an object (dict), got: {type(it)}")
    return x  # type: ignore


def normalize_for_json(x: Any) -> Any:
    if isinstance(x, dict):
        return {k: normalize_for_json(v) for k, v in x.items()}
    if isinstance(x, list):
        return [normalize_for_json(v) for v in x]
    if isinstance(x, Decimal):
        # Choose either float or str here
        return float(x)  # Or return str(x)
    return x


# =========================
# Utility: input iteration with support for very large files
# =========================

def iter_input_items(path: str, skip: int = 0) -> Iterable[Dict[str, Any]]:
    """
    Iteratively read the input JSON array file.
    - If ijson is installed, read in streaming mode, suitable for very large files
    - Otherwise, load all at once, suitable for medium-sized files
    """
    try:
        import ijson  # type: ignore
        with open(path, "rb") as f:
            it = ijson.items(f, "item")
            for idx, obj in enumerate(it):
                if idx < skip:
                    continue
                if not isinstance(obj, dict):
                    raise ValueError(f"Input item {idx} is not an object (dict).")
                yield normalize_for_json(obj)
        return
    except ImportError:
        pass

    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, list):
        raise ValueError("The top-level input JSON must be an array ([{}, {}]).")

    for idx, obj in enumerate(data):
        if idx < skip:
            continue
        if not isinstance(obj, dict):
            raise ValueError(f"Input item {idx} is not an object (dict).")
        yield normalize_for_json(obj)


# =========================
# Utility: stream-write a JSON array
# =========================

@dataclass
class JsonArrayWriter:
    """Stream output as a JSON array and support resumable append writes."""
    path: str
    ensure_ascii: bool = False
    indent: Optional[int] = None
    resume: bool = False
    already_done: int = 0  # Passed from progress; indicates the number of written items and is used to decide comma placement

    def __post_init__(self) -> None:
        # Resume: append to the existing file; first remove the trailing ']'
        if self.resume and os.path.exists(self.path) and os.path.getsize(self.path) > 0:
            self._fp = open(self.path, "rb+")
            self._prepare_for_append()
            # If items have already been written, write a comma before subsequent items; handled in write_item
            self._first = (self.already_done == 0)
        else:
            # Fresh run: overwrite the file
            self._fp = open(self.path, "wb")
            self._first = True
            self._fp.write(b"[")

    def _prepare_for_append(self) -> None:
        """
        In binary mode:
        - Search backward from the end for the last b']'
        - Truncate to this position, removing the closing bracket
        """
        self._fp.seek(0, os.SEEK_END)
        size = self._fp.tell()
        if size == 0:
            self._fp.write(b"[")
            return

        max_back = min(size, 64 * 1024)
        self._fp.seek(size - max_back)
        tail = self._fp.read()  # bytes

        idx = tail.rfind(b"]")
        if idx == -1:
            raise ValueError(f"Did not find ']'，at the end of the output file; cannot resume append: {self.path}")

        cut_pos = (size - max_back) + idx
        self._fp.seek(cut_pos)
        self._fp.truncate()

    def write_item(self, obj: Dict[str, Any]) -> None:
        if self._first:
            self._fp.write(b"\n")
            self._first = False
        else:
            # Important: the comma immediately follows the previous item '}'，so no blank line is produced
            self._fp.write(b",\n")

        s = json.dumps(obj, ensure_ascii=self.ensure_ascii, indent=self.indent)
        self._fp.write(s.encode("utf-8"))

    def close(self) -> None:
        if self._first:
            self._fp.write(b"]\n")
        else:
            self._fp.write(b"\n]\n")
        self._fp.close()

    def flush(self) -> None:
        self._fp.flush()
        # Optional, safer
        # os.fsync(self._fp.fileno())



# =========================
# Resumable execution
# =========================

def load_progress(progress_path: str) -> int:
    """Read the processed item count."""
    if not os.path.exists(progress_path):
        return 0
    try:
        with open(progress_path, "r", encoding="utf-8") as f:
            s = f.read().strip()
        return int(s) if s else 0
    except Exception:
        return 0


def save_progress(progress_path: str, n: int) -> None:
    """Atomically write progress to avoid file corruption if the process crashes."""
    tmp = progress_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(str(n))
    os.replace(tmp, progress_path)

def _contains_decimal(x: Any) -> bool:
    if isinstance(x, Decimal):
        return True
    if isinstance(x, dict):
        return any(_contains_decimal(v) for v in x.values())
    if isinstance(x, list):
        return any(_contains_decimal(v) for v in x)
    return False

def find_decimal_path(x: Any, path: str = "root") -> Optional[str]:
    if isinstance(x, Decimal):
        return path
    if isinstance(x, dict):
        for k, v in x.items():
            p = find_decimal_path(v, f"{path}.{k}")
            if p:
                return p
    if isinstance(x, list):
        for i, v in enumerate(x):
            p = find_decimal_path(v, f"{path}[{i}]")
            if p:
                return p
    return None



# =========================
# LLM invocation and retry
# =========================

def llm_invoke_with_retry(
    client: ChatLLMClient,
    user_prompt: str,
    system_prompt: str,
    *,
    max_retries: int = MAX_RETRIES,
    backoff_base_s: float = BACKOFF_BASE_S,
) -> str:
    last_err: Optional[Exception] = None
    for attempt in range(max_retries + 1):
        try:
            return client.invoke(user_prompt, system_prompt=system_prompt)
        except Exception as e:
            last_err = e
            if attempt >= max_retries:
                break
            time.sleep(backoff_base_s ** attempt)
    raise RuntimeError(f"LLM call failed after retries; last error: {last_err}") from last_err


def transform_batch(
    client: ChatLLMClient,
    batch: List[Dict[str, Any]],
    system_prompt: str,
) -> List[Dict[str, Any]]:
    """
    Process one batch:
    - Inject the batch into prompts/json_transform/transform_batch.md
    - Require the model to output a strict JSON array with the same length as the input
    - Optionally make one repair call if parsing fails
    """
    if _contains_decimal(batch):
        print("[WARN] batch contains Decimal (will normalize/convert).")

    p = find_decimal_path(batch)
    if p:
        print(f"[WARN] Decimal found at {p}")

    input_items_json = json.dumps(batch, ensure_ascii=False, indent=2)

    user_prompt = render(
        USER_PROMPT_PATH,
        input_items_json=input_items_json,
    )

    raw = llm_invoke_with_retry(client, user_prompt, system_prompt)

    try:
        json_text = _extract_first_json_block(raw)
        parsed = json.loads(json_text)
        out_list = _ensure_list_of_objects(parsed)
    except Exception:
        if not ENABLE_JSON_REPAIR:
            raise
        # Use the same system prompt, then run a repair prompt
        repair_user_prompt = render(
            JSON_REPAIR_PROMPT_PATH,
            bad_text=raw,
        )
        repaired = llm_invoke_with_retry(client, repair_user_prompt, system_prompt)
        json_text = _extract_first_json_block(repaired)
        parsed = json.loads(json_text)
        out_list = _ensure_list_of_objects(parsed)

    if len(out_list) != len(batch):
        raise ValueError(f"Batch length mismatch: input {len(batch)} items, output {len(out_list)} items")

    return out_list


# =========================
# Main workflow
# =========================

def run() -> None:
    if not LLM_API_KEY:
        raise RuntimeError("Missing LLM_API_KEY from the environment or script configuration.")

    # Read the system prompt
    system_prompt = load(SYSTEM_PROMPT_PATH)

    # Initialize clients
    client = ChatLLMClient(
        provider=LLM_PROVIDER,
        model=LLM_MODEL,
        temperature=LLM_TEMPERATURE,
        timeout_s=LLM_TIMEOUT_S,
        api_key=LLM_API_KEY,
        base_url=LLM_BASE_URL,
    )

    already_done = load_progress(PROGRESS_PATH) if RESUME else 0
    if already_done > 0:
        print(f"[RESUME] Skipping the first {already_done} items：{PROGRESS_PATH}")

    if RESUME and already_done > 0:
        if (not os.path.exists(OUTPUT_JSON_PATH)) or os.path.getsize(OUTPUT_JSON_PATH) == 0:
            raise RuntimeError(
                f"Detected progress={already_done}，but the output file does not exist or is empty: {OUTPUT_JSON_PATH}。"
                f"This would skip input items while the output is missing. Delete the progress file and rerun, or restore the output file."
            )
    writer = JsonArrayWriter(
        OUTPUT_JSON_PATH,
        ensure_ascii=OUTPUT_ENSURE_ASCII,
        indent=OUTPUT_INDENT,
        resume=RESUME and already_done > 0,
        already_done=already_done,
    )
    processed = already_done
    batch: List[Dict[str, Any]] = []
    t0 = time.time()

    try:
        for item in iter_input_items(INPUT_JSON_PATH, skip=already_done):
            batch.append(item)

            if len(batch) >= BATCH_SIZE:
                out_items = transform_batch(client, batch, system_prompt)
                for o in out_items:
                    writer.write_item(o)

                writer.flush()  # Flush to disk immediately after writing
                processed += len(batch)
                batch.clear()

                # Periodically save progress
                save_progress(PROGRESS_PATH, processed)
                if processed % (BATCH_SIZE * 2) == 0:
                    print(f"[PROGRESS] processed={processed} elapsed={time.time()-t0:.1f}s")

        # Process the final partial batch
        if batch:
            out_items = transform_batch(client, batch, system_prompt)
            for o in out_items:
                writer.write_item(o)
            writer.flush()  # Flush to disk immediately after writing
            processed += len(batch)
            batch.clear()
            save_progress(PROGRESS_PATH, processed)

        print(f"[DONE] processed={processed} output={OUTPUT_JSON_PATH} total_elapsed={time.time()-t0:.1f}s")

    finally:
        writer.close()


if __name__ == "__main__":
    run()
