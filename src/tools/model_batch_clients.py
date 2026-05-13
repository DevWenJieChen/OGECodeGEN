from __future__ import annotations

import json
import time
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence
from datetime import datetime

from openai import OpenAI
from pydantic import BaseModel


@dataclass
class BatchJobInfo:
    batch_id: str
    input_file_id: str
    status: str
    endpoint: str
    output_file_id: Optional[str] = None
    error_file_id: Optional[str] = None
    raw: Optional[Dict[str, Any]] = None


@dataclass
class BatchRunResult:
    batch_id: str
    status: str
    output_file_id: Optional[str]
    error_file_id: Optional[str]
    success_items: List[Dict[str, Any]]
    error_items: List[Dict[str, Any]]
    raw_batch: Dict[str, Any]


@dataclass
class BatchLLMClient:
    """
    Actual Batch File / Batch API client.
    Suitable for:
    - OpenAI official Batch API
    - Alibaba Cloud Bailian OpenAI-compatible Batch File API

    """
    provider: str
    model: str
    api_key: str
    base_url: str
    completion_window: str = "24h"
    metadata: Optional[Dict[str, str]] = None
    timeout_s: int = 60
    enable_thinking: Optional[bool] = None

    def __post_init__(self) -> None:
        self._client = OpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
            timeout=self.timeout_s,
        )

    # -------------------------
    # JSONL request builders
    # -------------------------
    def build_chat_request(
        self,
        *,
        custom_id: str,
        messages: List[Dict[str, Any]],
        endpoint: str = "/v1/chat/completions",
        extra_body: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
        }

        if self.enable_thinking is not None:
            body["enable_thinking"] = self.enable_thinking

        if extra_body:
            body.update(extra_body)

        return {
            "custom_id": custom_id,
            "method": "POST",
            "url": endpoint,
            "body": body,
        }

    def build_chat_requests_from_prompts(
        self,
        *,
        user_prompts: Sequence[str],
        system_prompt: Optional[str] = None,
        endpoint: str = "/v1/chat/completions",
        custom_id_prefix: str = "req",
        extra_body: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        requests: List[Dict[str, Any]] = []
        for idx, up in enumerate(user_prompts):
            messages: List[Dict[str, Any]] = []
            if system_prompt:
                messages.append({"role": "system", "content": system_prompt})
            messages.append({"role": "user", "content": up})

            requests.append(
                self.build_chat_request(
                    custom_id=f"{custom_id_prefix}-{idx}",
                    messages=messages,
                    endpoint=endpoint,
                    extra_body=extra_body,
                )
            )
        return requests

    # -------------------------
    # File helpers
    # -------------------------
    @staticmethod
    def write_requests_jsonl(
        requests: Sequence[Dict[str, Any]],
        jsonl_path: str | Path,
    ) -> Path:
        path = Path(jsonl_path)
        path.parent.mkdir(parents=True, exist_ok=True)

        with path.open("w", encoding="utf-8") as f:
            for item in requests:
                f.write(json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n")

        return path

    def upload_batch_file(self, jsonl_path: str | Path) -> str:
        file_obj = self._client.files.create(
            file=Path(jsonl_path),
            purpose="batch",
        )
        return file_obj.id

    def create_batch_job(
        self,
        *,
        input_file_id: str,
        endpoint: str = "/v1/chat/completions",
        completion_window: Optional[str] = None,
        metadata: Optional[Dict[str, str]] = None,
    ) -> BatchJobInfo:
        batch = self._client.batches.create(
            input_file_id=input_file_id,
            endpoint=endpoint,
            completion_window=completion_window or self.completion_window,
            metadata=metadata or self.metadata,
        )

        batch_dict = batch.model_dump() if hasattr(batch, "model_dump") else dict(batch)

        return BatchJobInfo(
            batch_id=batch.id,
            input_file_id=batch.input_file_id,
            status=batch.status,
            endpoint=batch.endpoint,
            output_file_id=getattr(batch, "output_file_id", None),
            error_file_id=getattr(batch, "error_file_id", None),
            raw=batch_dict,
        )

    def retrieve_batch_job(self, batch_id: str) -> BatchJobInfo:
        batch = self._client.batches.retrieve(batch_id)
        batch_dict = batch.model_dump() if hasattr(batch, "model_dump") else dict(batch)

        return BatchJobInfo(
            batch_id=batch.id,
            input_file_id=batch.input_file_id,
            status=batch.status,
            endpoint=batch.endpoint,
            output_file_id=getattr(batch, "output_file_id", None),
            error_file_id=getattr(batch, "error_file_id", None),
            raw=batch_dict,
        )


    def wait_for_batch_job(
            self,
            batch_id: str,
            *,
            poll_interval_s: int = 15,
            terminal_statuses: Optional[set[str]] = None,
            verbose: bool = True,
    ) -> BatchJobInfo:
        terminal = terminal_statuses or {"completed", "failed", "expired", "cancelled"}

        last_status = None
        round_idx = 0

        while True:
            round_idx += 1
            info = self.retrieve_batch_job(batch_id)

            raw = info.raw or {}
            request_counts = raw.get("request_counts") or {}
            total = request_counts.get("total")
            completed = request_counts.get("completed")
            failed = request_counts.get("failed")

            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            if verbose:
                # Print prominently when the status changes
                changed = (info.status != last_status)

                msg = (
                    f"[{now_str}] poll={round_idx} "
                    f"batch_id={batch_id} "
                    f"status={info.status}"
                )

                # Include statistics if available
                if total is not None or completed is not None or failed is not None:
                    msg += (
                        f" | request_counts: total={total}, "
                        f"completed={completed}, failed={failed}"
                    )

                # Also show output/error files if present
                if info.output_file_id:
                    msg += f" | output_file_id={info.output_file_id}"
                if info.error_file_id:
                    msg += f" | error_file_id={info.error_file_id}"

                # Make status changes more visible
                if changed:
                    print("=" * 100, flush=True)
                    print(msg, flush=True)
                    print("=" * 100, flush=True)
                else:
                    print(msg, flush=True)

            last_status = info.status

            if info.status in terminal:
                return info

            time.sleep(poll_interval_s)

    def cancel_batch_job(self, batch_id: str) -> BatchJobInfo:
        batch = self._client.batches.cancel(batch_id)
        batch_dict = batch.model_dump() if hasattr(batch, "model_dump") else dict(batch)

        return BatchJobInfo(
            batch_id=batch.id,
            input_file_id=batch.input_file_id,
            status=batch.status,
            endpoint=batch.endpoint,
            output_file_id=getattr(batch, "output_file_id", None),
            error_file_id=getattr(batch, "error_file_id", None),
            raw=batch_dict,
        )

    # -------------------------
    # Output parsing
    # -------------------------
    def download_file_text(self, file_id: str) -> str:
        content = self._client.files.content(file_id)
        return content.text

    def download_file_to_path(self, file_id: str, output_path: str | Path) -> Path:
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)

        content = self._client.files.content(file_id)
        content.write_to_file(path)
        return path

    @staticmethod
    def parse_jsonl_text(text: str) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            items.append(json.loads(line))
        return items

    def download_output_items(self, output_file_id: str) -> List[Dict[str, Any]]:
        text = self.download_file_text(output_file_id)
        return self.parse_jsonl_text(text)

    def download_error_items(self, error_file_id: str) -> List[Dict[str, Any]]:
        text = self.download_file_text(error_file_id)
        return self.parse_jsonl_text(text)

    # -------------------------
    # High-level helpers
    # -------------------------
    def run_batch_requests(
            self,
            *,
            requests: Sequence[Dict[str, Any]],
            endpoint: str = "/v1/chat/completions",
            completion_window: Optional[str] = None,
            metadata: Optional[Dict[str, str]] = None,
            poll_interval_s: int = 15,
            keep_input_file: bool = False,
            input_jsonl_path: Optional[str | Path] = None,
            verbose: bool = True,
    ) -> BatchRunResult:
        """
        Complete workflow:
        1. Write JSONL
        2. Upload the batch file
        3. Create the batch job
        4. Poll until a terminal state
        5. Download successful/failed results
        """
        if input_jsonl_path is None:
            tmp_dir = Path(tempfile.mkdtemp(prefix="batch_llm_"))
            input_jsonl_path = tmp_dir / "batch_input.jsonl"

        input_path = self.write_requests_jsonl(requests, input_jsonl_path)
        if verbose:
            print(f"[BatchLLMClient] Input JSONL written to: {input_path}", flush=True)
            print(f"[BatchLLMClient] Number of requests: {len(requests)}", flush=True)

        input_file_id = self.upload_batch_file(input_path)
        if verbose:
            print(f"[BatchLLMClient] File uploaded successfully，input_file_id={input_file_id}", flush=True)

        job = self.create_batch_job(
            input_file_id=input_file_id,
            endpoint=endpoint,
            completion_window=completion_window,
            metadata=metadata,
        )
        if verbose:
            print(
                f"[BatchLLMClient] Batch job created: batch_id={job.batch_id}, "
                f"status={job.status}, endpoint={job.endpoint}",
                flush=True,
            )

        final_job = self.wait_for_batch_job(
            job.batch_id,
            poll_interval_s=poll_interval_s,
            verbose=verbose,
        )

        success_items: List[Dict[str, Any]] = []
        error_items: List[Dict[str, Any]] = []

        if final_job.output_file_id:
            if verbose:
                print(f"[BatchLLMClient] Start downloading successful results: {final_job.output_file_id}", flush=True)
            success_items = self.download_output_items(final_job.output_file_id)
            if verbose:
                print(f"[BatchLLMClient] Number of successful results: {len(success_items)}", flush=True)

        if final_job.error_file_id:
            if verbose:
                print(f"[BatchLLMClient] Start downloading error results: {final_job.error_file_id}", flush=True)
            error_items = self.download_error_items(final_job.error_file_id)
            if verbose:
                print(f"[BatchLLMClient] Number of error results: {len(error_items)}", flush=True)

        if not keep_input_file:
            try:
                Path(input_path).unlink(missing_ok=True)
                if verbose:
                    print(f"[BatchLLMClient] Temporary input file deleted: {input_path}", flush=True)
            except Exception as e:
                if verbose:
                    print(f"[BatchLLMClient] Failed to delete temporary input file: {e}", flush=True)

        if verbose:
            print(
                f"[BatchLLMClient] Job finished: batch_id={final_job.batch_id}, "
                f"final_status={final_job.status}",
                flush=True,
            )

        return BatchRunResult(
            batch_id=final_job.batch_id,
            status=final_job.status,
            output_file_id=final_job.output_file_id,
            error_file_id=final_job.error_file_id,
            success_items=success_items,
            error_items=error_items,
            raw_batch=final_job.raw or {},
        )

    def run_chat_batch(
        self,
        *,
        user_prompts: Sequence[str],
        system_prompt: Optional[str] = None,
        endpoint: str = "/v1/chat/completions",
        custom_id_prefix: str = "req",
        extra_body: Optional[Dict[str, Any]] = None,
        completion_window: Optional[str] = None,
        metadata: Optional[Dict[str, str]] = None,
        poll_interval_s: int = 15,
    ) -> BatchRunResult:
        requests = self.build_chat_requests_from_prompts(
            user_prompts=user_prompts,
            system_prompt=system_prompt,
            endpoint=endpoint,
            custom_id_prefix=custom_id_prefix,
            extra_body=extra_body,
        )
        return self.run_batch_requests(
            requests=requests,
            endpoint=endpoint,
            completion_window=completion_window,
            metadata=metadata,
            poll_interval_s=poll_interval_s,
        )

    # -------------------------
    # Convenience: extract text outputs
    # -------------------------
    @staticmethod
    def extract_chat_text_map(success_items: Sequence[Dict[str, Any]]) -> Dict[str, str]:
        """
        Convert successful Batch results to:
        {custom_id: assistant_text}
        """
        out: Dict[str, str] = {}

        for item in success_items:
            custom_id = item.get("custom_id")
            response = item.get("response") or {}
            body = response.get("body") or {}
            choices = body.get("choices") or []

            text = ""
            if choices:
                message = choices[0].get("message") or {}
                content = message.get("content")

                if isinstance(content, str):
                    text = content
                elif isinstance(content, list):
                    # Support chunked content structure
                    parts = []
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            parts.append(block.get("text", ""))
                    text = "".join(parts)

            if custom_id is not None:
                out[str(custom_id)] = text

        return out