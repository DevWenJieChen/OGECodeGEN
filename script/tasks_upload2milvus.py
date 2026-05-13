import argparse
import json
import time
import os
from pathlib import Path
from typing import Any, Dict, List, Optional
from langchain_openai import OpenAIEmbeddings  # type: ignore
from tqdm import tqdm

from src.tools.config import load_config
from src.tools.milvus_store import MilvusVectorStore
from src.tools.vector_store_base import SearchHit
from src.tools.model_clients import EmbeddingClient


def build_task_embedding_text(task: Dict[str, Any]) -> str:
    parts: List[str] = []

    def _clean_text(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, list):
            items = []
            for x in value:
                s = str(x).strip()
                if s:
                    items.append(s)
            return "；".join(items).strip()
        return str(value).strip()

    def _truncate(text: str, max_len: int) -> str:
        text = text.strip()
        if not text:
            return ""
        return text if len(text) <= max_len else text[:max_len].rstrip() + "..."

    def _add(label: str, value: Any, max_len: int | None = None) -> None:
        text = _clean_text(value)
        if not text:
            return
        if max_len is not None:
            text = _truncate(text, max_len)
        parts.append(f"{label}: {text}")

    _add("name", task.get("name"))
    _add("aliases", task.get("aliases"))
    _add("domains", task.get("domains"))
    _add("keywords", task.get("keywords"))
    _add("summary", task.get("summary"), max_len=300)
    _add("workflow", task.get("workflow"), max_len=1200)

    example_queries = task.get("example_queries")
    if isinstance(example_queries, list):
        example_queries = example_queries[:3]
    _add("example_queries", example_queries, max_len=400)

    _add("data_and_prerequisites", task.get("data_and_prerequisites"), max_len=400)

    return "\n".join(parts)


# =========================
#   Read task JSON file
# =========================

def load_task_list(json_path: Path) -> List[Dict[str, Any]]:
    """
    Read a JSON file with the [{},{},...] structure.
    """
    data = json.loads(json_path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("The operator file must be a JSON array: [{...},{...}]")
    # Filter out non-dict elements
    return [x for x in data if isinstance(x, dict)]


# =========================
#   Create EmbeddingClient
# =========================

def get_embedding_client(cfg: dict) -> EmbeddingClient:
    """
    Construct an EmbeddingClient from config.yaml.
    """
    emb_cfg = cfg.get("embedding", {})
    provider = emb_cfg.get("provider")
    model = emb_cfg.get("model", "")
    dim = cfg.get("retrieval").get("dim")
    if not model:
        raise ValueError("embedding.model in config.yaml cannot be empty")

    return EmbeddingClient(
        provider=provider,
        model=model,
        dimensions=dim,
        api_key=emb_cfg.get("api_key", ""),
        base_url=emb_cfg.get("base_url"),
        timeout_s=int(emb_cfg.get("timeout_s", 60)),
    )

# =========================
#  Upload to Milvus
# =========================

def upload_operators(
    *,
    cfg: dict,
    store: MilvusVectorStore,
    embedder: EmbeddingClient,
    collection_name: str,
    dim: int,
    tasks: List[Dict[str, Any]],
    recreate: bool,
    batch_size: int = 64,
) -> int:
    """
    Upload task knowledge cards to Milvus in batches, with batch-level progress.
    Must be called with keyword arguments, not positional arguments.
    """
    # Create or ensure the collection exists
    if recreate:
        store.recreate_collection(collection_name, dim)
    else:
        store.ensure_collection(collection_name, dim)

    # Filter out tasks without an id to keep the progress accurate
    valid_tasks = [t for t in tasks if (t.get("id") or "").strip()]
    total_tasks  = len(valid_tasks)

    if total_tasks == 0:
        return 0

    total = 0
    buf_texts: List[str] = []
    buf_rows: List[Dict[str, Any]] = []

    # Number of batches for tqdm display
    total_batches = (total_tasks + batch_size - 1) // batch_size

    with tqdm(
        total=total_batches,
        desc="Uploading tasks to Milvus",
        unit="batch",
        ncols=100,
    ) as pbar:

        for task in valid_tasks:
            task_id = task["id"].strip()+str(int(time.time() * 1000))
            task_name  = task["name"].strip()
            emb_text = build_task_embedding_text(task)
            if not emb_text:
                continue

            buf_texts.append(emb_text)
            buf_rows.append({
                "id": task_id,
                "name": task_name,
                "vector": None,
                "payload": task,
            })

            # Full batch
            if len(buf_rows) >= batch_size:
                vectors = embedder.embed_documents(buf_texts)

                # Validate dimension consistency
                for i, v in enumerate(vectors):
                    if len(v) != dim:
                        raise ValueError(
                            f"Embedding dimension mismatch: item {i} got={len(v)} expected={dim}"
                        )

                for row, vec in zip(buf_rows, vectors):
                    row["vector"] = vec

                store.insert(collection_name, buf_rows)
                total += len(buf_rows)

                buf_texts.clear()
                buf_rows.clear()

                # Update the progress bar for one batch
                pbar.update(1)

        # Final partial batch
        if buf_rows:
            vectors = embedder.embed_documents(buf_texts)
            for i, v in enumerate(vectors):
                if len(v) != dim:
                    raise ValueError(
                        f"Embedding dimension mismatch: item {i} got={len(v)} expected={dim}"
                    )

            for row, vec in zip(buf_rows, vectors):
                row["vector"] = vec

            store.insert(collection_name, buf_rows)
            total += len(buf_rows)

            pbar.update(1)

    return total

# =========================
#   Simple retrieval test
# =========================
def test_search(
    *,
    embedder: EmbeddingClient,
    store: MilvusVectorStore,
    collection_name: str,
    query: str,
    top_k: int,
) -> List[SearchHit]:
    """
    Run a vector retrieval test with a natural-language query and return a list of SearchHit objects.
    """
    q_vec = embedder.embed_query(query)
    hits_batch = store.search(collection_name, [q_vec], top_k=top_k, output_fields=["id", "name", "payload"])
    return hits_batch[0] if hits_batch else []


# =========================
# Script entry function
# =========================

def main(ide_run: bool = False) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default="data_json/task_info_total.json", help="Task JSON file path ([{},{},...])")
    ap.add_argument("--recreate", action="store_true", help="Whether to delete and recreate the collection (dangerous)")
    ap.add_argument("--batch_size", type=int, default=6, help="Batch size for vectorization and insertion")
    args = ap.parse_args()

    cfg = load_config("config.yaml")

    # Read Milvus configuration
    milvus_cfg = cfg.get("milvus", {})
    retrieval_cfg = cfg.get("retrieval", {})
    collection_name = milvus_cfg.get("collection_tasks", "task_kb")
    dim = int(retrieval_cfg.get("dim", 763))
    if dim <= 0:
        raise ValueError("milvus.dim in config.yaml must be a positive integer and match the embedding output dimension")

    # Initialize clients
    store = MilvusVectorStore(cfg)
    embedder = get_embedding_client(cfg)

    # Read operator file
    tasks = load_task_list(Path(args.json))
    print(f"[INFO] Number of task knowledge entries read：{len(tasks)}")

    if ide_run:
        args.recreate = ide_run

    # Upload
    written = upload_operators(
        cfg=cfg,
        store=store,
        embedder=embedder,
        collection_name=collection_name,
        dim=dim,
        tasks=tasks,
        recreate=args.recreate,
        batch_size=args.batch_size,
    )
    print(f"[INFO] Upload completed: wrote {written} rows to collection='{collection_name}'")


def test_from_ide(query:str) -> None:
    """
    Run a quick test after upload.
    """
    cfg = load_config()
    store = MilvusVectorStore(cfg)
    embedder = get_embedding_client(cfg)
    collection_name = cfg["milvus"].get("collection_tasks", "task_kb")
    hits = test_search(
        embedder=embedder,
        store=store,
        collection_name=collection_name,
        query=query,
        top_k=cfg["retrieval"].get("top_k", 5),
    )
    for i, h in enumerate(hits, 1):
        print(f"#{i} id={h.id} score={h.score:.6f} name={h.name}")


if __name__ == "__main__":
    print(os.getcwd())
    # main()
    main(ide_run=True)
    test_from_ide("effects of topographic factors on plants")