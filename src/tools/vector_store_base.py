from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence


@dataclass
class SearchHit:
    """Unified search result format that hides differences across vector database responses."""
    id: str
    name: Optional[str]
    score: float
    payload: Dict[str, Any]


class VectorStore(ABC):
    """
    Abstract vector database interface (minimal usable version)
    Goal: the business layer depends only on this interface, not on a specific database SDK.
    """

    @abstractmethod
    def recreate_collection(self, name: str, dim: int, **kwargs) -> None:
        """Drop and recreate the collection (dangerous operation, often used to rebuild the vector database)."""
        raise NotImplementedError

    @abstractmethod
    def ensure_collection(self, name: str, dim: int, **kwargs) -> None:
        """Ensure the collection exists (reuse it if it exists; create it otherwise)."""
        raise NotImplementedError

    @abstractmethod
    def insert(self, name: str, data: List[Dict[str, Any]], **kwargs) -> None:
        """
        Insert data (the data structure is implementation-defined, but should contain at least: id, vector, payload/meta)
        """
        raise NotImplementedError

    @abstractmethod
    def search(
        self,
        collection_name: str,
        query_vectors: Sequence[Sequence[float]],
        top_k: int = 5,
        **kwargs,
    ) -> List[List[SearchHit]]:
        """
        Vector search (batch query):
        - Input query_vectors: [vec1, vec2, ...]
        - Returns: a hit list for each query
        """
        raise NotImplementedError
