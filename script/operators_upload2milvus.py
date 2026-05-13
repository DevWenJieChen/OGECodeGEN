import argparse
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional
from langchain_openai import OpenAIEmbeddings  # type: ignore
from tqdm import tqdm

from src.tools.config import load_config
from src.tools.milvus_store import MilvusVectorStore
from src.tools.vector_store_base import SearchHit
from src.tools.model_clients import EmbeddingClient

# =========================
#  Assemble embedding text using the specified fields
# =========================

def build_operator_embedding_text(op: Dict[str, Any]) -> str:
    """
    Embedding text composition as required:
    name, display_name, category, functional_semantic, details_description,
    inputs/outputs description fields in,
    examples description fields in
    """
    # Prefer the prebuilt embedding_text for embedding text.
    prebuilt = (op.get("embedding_text") or "").strip()
    if prebuilt:
        return prebuilt

    parts: List[str] = []

    def add(label: str, value: Optional[str]) -> None:
        v = (value or "").strip()
        if v:
            parts.append(f"{label}: {v}")

    add("name", op.get("name"))
    add("display_name", op.get("display_name"))
    add("category", op.get("category"))
    add("functional_semantic", op.get("functional_semantic"))
    add("details_description", op.get("details_description"))

    # inputs[].description
    inputs = op.get("inputs") or []
    for item in inputs:
        if isinstance(item, dict):
            name = item.get("name")
            typ = item.get("type")
            desc = item.get("description")
            line = f"input parameter {name}(type={typ})"
            add(line, desc)

    # outputs[].description
    outputs = op.get("outputs") or []
    for item in outputs:
        if isinstance(item, dict):
            name = item.get("name")
            typ = item.get("type")
            desc = item.get("description")
            line = f"input parameter {name}(type={typ})"
            add(line, desc)

    # examples[].description
    examples = op.get("examples") or []
    for item in examples:
        if isinstance(item, dict):
            add(f"usage example:", item.get("description"))

    return "\n".join(parts)


def build_operator_payload(op: Dict[str, Any]) -> Dict[str, Any]:
    """
    Keep only the core fields required for code generation in the payload:
    - Basic identity information
    - Functional semantics and detailed description
    - Complete inputs / outputs
    - Keep title / description / code in examples so the LLM can reference usage patterns and parameter values
    - info / applicable_data, kept only when non-empty
    """
    payload: Dict[str, Any] = {
        "name": op.get("name"),
        "display_name": op.get("display_name"),
        "category": op.get("category"),
        "source": op.get("source"),
        "functional_semantic": op.get("functional_semantic"),
        "details_description": op.get("details_description"),
        "inputs": op.get("inputs") or [],
        "outputs": op.get("outputs") or [],
    }

    examples_compact: List[Dict[str, Any]] = []
    for ex in op.get("examples") or []:
        if not isinstance(ex, dict):
            continue
        title = (ex.get("title") or "").strip()
        desc = (ex.get("description") or "").strip()
        code = (ex.get("code") or "").strip()
        item: Dict[str, Any] = {}
        if title:
            item["title"] = title
        if desc:
            item["description"] = desc
        if code:
            item["code"] = code
        if item:
            examples_compact.append(item)
    if examples_compact:
        payload["examples"] = examples_compact


    return payload


# =========================
#   Read operator JSON file
# =========================

def load_operator_list(json_path: Path) -> List[Dict[str, Any]]:
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
    operators: List[Dict[str, Any]],
    recreate: bool,
    batch_size: int = 64,
) -> int:
    # 1) Create or ensure the collection exists
    if recreate:
        store.recreate_collection(collection_name, dim)
    else:
        store.ensure_collection(collection_name, dim)

    # Filter out operators without a name to keep the progress accurate
    valid_ops = [op for op in operators if (op.get("name") or "").strip()]
    total_ops = len(valid_ops)

    if total_ops == 0:
        return 0

    total = 0
    buf_texts: List[str] = []
    buf_rows: List[Dict[str, Any]] = []

    # Number of batches for tqdm display
    total_batches = (total_ops + batch_size - 1) // batch_size

    with tqdm(
        total=total_batches,
        desc="Uploading operators to Milvus",
        unit="batch",
        ncols=100,
    ) as pbar:

        for op in valid_ops:
            op_id = op["name"].strip()
            op_name = op["display_name"].strip()

            emb_text = build_operator_embedding_text(op)
            if not emb_text:
                continue

            payload = build_operator_payload(op)
            buf_texts.append(emb_text)
            buf_rows.append({
                "id": op_id,
                "name": op_name,
                "vector": None,
                "payload": payload,
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
    q_vec = embedder.embed_query(query)
    hits_batch = store.search(collection_name, [q_vec], top_k=top_k, output_fields=["id", "name", "payload"])
    return hits_batch[0] if hits_batch else []


# =========================
# Script entry function
# =========================

def main(ide_run: bool = False) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default="data_json/operator_info_core_embedding_minimal_del.json", help="Operator JSON file path ([{},{},...])")
    ap.add_argument("--recreate", action="store_true", help="Whether to delete and recreate the collection (dangerous)")
    ap.add_argument("--batch_size", type=int, default=6, help="Batch size for vectorization and insertion")
    args = ap.parse_args()

    cfg = load_config("config.yaml")

    # Read Milvus configuration
    milvus_cfg = cfg.get("milvus", {})
    retrieval_cfg = cfg.get("retrieval", {})
    collection_name = milvus_cfg.get("collection_operators", "operator_kb")
    dim = int(retrieval_cfg.get("dim", 763))
    if dim <= 0:
        raise ValueError("milvus.dim in config.yaml must be a positive integer and match the embedding output dimension")

    # Initialize clients
    store = MilvusVectorStore(cfg)
    embedder = get_embedding_client(cfg)

    # Read operator file
    ops = load_operator_list(Path(args.json))
    print(f"[INFO] Number of operator entries read：{len(ops)}")

    if ide_run:
        args.recreate = ide_run

    # Upload
    written = upload_operators(
        cfg=cfg,
        store=store,
        embedder=embedder,
        collection_name=collection_name,
        dim=dim,
        operators=ops,
        recreate=args.recreate,
        batch_size=args.batch_size,
    )
    print(f"[INFO] Upload completed: wrote {written} rows to collection='{collection_name}'")

def test_from_ide(query:str) -> None:
    cfg = load_config()
    store = MilvusVectorStore(cfg)
    embedder = get_embedding_client(cfg)
    collection_name = cfg["milvus"].get("collection_operators", "operator_kb")
    hits = test_search(
        embedder=embedder,
        store=store,
        collection_name=collection_name,
        query=query,
        top_k=20#cfg["retrieval"].get("top_k", 15),
    )
    for i, h in enumerate(hits, 1):
        print(f"{i}. {h.payload}")


if __name__ == "__main__":
    print(os.getcwd())
    # main()
    main(ide_run=True)
    test_from_ide("slope")
