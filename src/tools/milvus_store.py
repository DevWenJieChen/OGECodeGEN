# src/milvus_vector_store.py
from __future__ import annotations

from typing import Any, Dict, List, Sequence, Optional
from pymilvus import MilvusClient, DataType

from src.tools.vector_store_base import VectorStore, SearchHit


class MilvusVectorStore(VectorStore):
    """
    Milvus VectorStore implementation (based on pymilvus.MilvusClient)
    The schema contains four fixed fields:
       - id      : VARCHAR，primary key (must be unique within the collection)
       - name    : VARCHAR，human-readable name (not required to be unique)
       - vector  : FLOAT_VECTOR(dim)，vector field
       - payload : JSON，raw knowledge / structured knowledge (returned to the LLM after retrieval)
    This class only wraps basic Milvus capabilities: collection creation / index creation / insertion / search
    """

    def __init__(self, cfg: dict):
        milvus_cfg = cfg.get("milvus", {})
        host = milvus_cfg.get("host")
        port = milvus_cfg.get("port")
        token = milvus_cfg.get("token")

        if not host or not port:
            raise ValueError("milvus.host / milvus.port must be configured")
        # conn_uri = f"{host}:{port}"
        conn_uri = f"{host}"

        self._client = MilvusClient(uri=conn_uri, token=token)
        self._dynamic_field = bool(milvus_cfg.get("dynamic_field", True))
        self._index_type = milvus_cfg.get("index_type")
        self._metric_type = milvus_cfg.get("metric_type")

    def _create_collection(self, name: str, dim: int) -> None:
        """
        Explicitly create a collection (schema + index)
        """
        if dim <= 0:
            raise ValueError("dim must be a positive integer")
        schema = MilvusClient.create_schema(
            auto_id=False,
            enable_dynamic_field=self._dynamic_field,
        )

        # Primary key field: use VARCHAR to support op_id / coverage_id / task_id, etc.
        schema.add_field(
            field_name="id",
            datatype=DataType.VARCHAR,
            is_primary=True,
            max_length=128,
        )
        # name field: explicitly store a name for human readability
        schema.add_field(
            field_name="name",
            datatype=DataType.VARCHAR,
            max_length=256,
        )
        # Vector field: fixed name is vector
        schema.add_field(
            field_name="vector",
            datatype=DataType.FLOAT_VECTOR,
            dim=int(dim),
        )
        # Payload field: used to store raw knowledge; currently stores JSON
        schema.add_field(
            field_name="payload",
            datatype=DataType.JSON,
        )

        # Create an index using the configured defaults
        index_params = self._client.prepare_index_params()
        index_params.add_index(
            field_name="vector",
            index_type=self._index_type,
            metric_type=self._metric_type,
        )

        self._client.create_collection(
            collection_name=name,
            schema=schema,
            index_params=index_params,
        )



    def recreate_collection(self, collection_name: str, dim: int, **kwargs) -> None:
        """
        Drop and recreate the collection
        Minimum parameters for create_collection: collection_name + dimension
        Other parameters (metric_type, index_type) can be extended through kwargs
        """
        if self._client.has_collection(collection_name):
            self._client.drop_collection(collection_name)

        self._create_collection(name=collection_name, dim=dim)

    def ensure_collection(self, collection_name: str, dim: int, **kwargs) -> None:
        """
        Reuse the collection if it exists; create it otherwise
        See recreate_collection for other details
        """
        if self._client.has_collection(collection_name):
            return
        self._create_collection(name=collection_name, dim=dim)


    def insert(self, collection_name: str, data: List[Dict[str, Any]], **kwargs) -> None:
        """
        Each item in data should follow a unified structure (can be guaranteed upstream):
            - id: str
            - vector: List[float]
            - name: str（human-readable name）
            - payload: dict（raw knowledge / structured knowledge）
        MilvusClient.insert accepts a list of dicts, but field names must match the collection schema.
        """
        self._client.insert(
            collection_name=collection_name,
            data=data,
            **kwargs,
        )


    def search(
        self,
        collection_name: str,
        query_vectors: Sequence[Sequence[float]],
        top_k: int = 5,
        output_fields: Optional[List[str]] = None,
        **kwargs,
    ) -> List[List[SearchHit]]:
        """
        Return SearchHit uniformly and hide differences in the raw Milvus response structure.
        Inputs:
        - query_vectors: [[...], [...]]，each element is a query vector
        - top_k: number of candidates returned for each query
        - output_fields: which fields Milvus should return
            Defaults to ["id", "name", "payload"]
        Returns:
        - List[List[SearchHit]]：
            the outer list corresponds to each query, and the inner list contains hits for that query
        """
        if not query_vectors:
            return []

        fields = output_fields or ["id", "name", "payload"]
        raw = self._client.search(
            collection_name=collection_name,
            data=list(query_vectors),
            limit=int(top_k),
            output_fields=fields,
            **kwargs,
        )

        # Handle different versions where the returned structure may vary
        results: List[List[SearchHit]] = []
        for hits in raw:
            one_query: List[SearchHit] = []
            for h in hits:
                entity = h.get("entity") or {}
                # Primary key: prefer the top-level id, then entity["id"]
                _id = h.get("id") or entity.get("id") or ""
                _id = str(_id)
                distance = h.get("distance")
                if distance is None:
                    # Some responses return distance: smaller can mean more similar; in other cases larger similarity is better
                    # Do not force a conversion here; map it directly to score (the business layer only needs it for sorting/display)
                    distance = h.get("score", 0.0)

                entity = h.get("entity") or {}

                # payload / meta field names may differ; normalize them as payload here
                payload = entity.get("payload")
                if payload is None:
                    payload = entity.get("meta")
                if payload is None:
                    # If output_fields is not specified, entity may be empty
                    payload = {}

                one_query.append(SearchHit(id=_id, name= payload.get("name"),score=float(distance), payload=payload))
            results.append(one_query)
        return results

    # =========================
    # Other utility methods
    # =========================

    def has_collection(self, collection_name: str) -> bool:
        """Check whether the collection exists"""
        return bool(self._client.has_collection(collection_name))

    def drop_collection(self, collection_name: str) -> None:
        """Drop the collection (dangerous operation)"""
        if self._client.has_collection(collection_name):
            self._client.drop_collection(collection_name)
