import hnswlib
import numpy as np
from pathlib import Path
from typing import List, Tuple

class VectorStore:
    def __init__(self, index_path: Path, dim: int = 384):
        self.index_path = index_path
        self.dim = dim
        self.index = hnswlib.Index(space='cosine', dim=dim)

    def add_items(self, data: List[List[float]], ids: List[int]):
        # space for V1: max_elements = len(data) * 2
        num_elements = len(data)
        self.index.init_index(max_elements=num_elements * 2, ef_construction=200, M=16)
        self.index.add_items(np.array(data), np.array(ids))

    def save(self):
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        self.index.save_index(str(self.index_path))

    def load(self):
        if self.index_path.exists():
            self.index.load_index(str(self.index_path))

    def knn_query(self, query_vector: List[float], k: int = 10) -> Tuple[List[int], List[float]]:
        # Handle uninitialized index
        try:
            n = self.index.element_count
            if n == 0:
                return [], []
        except Exception:
            return [], []

        k = min(k, n)
        if k <= 0:
            return [], []
        # hnswlib requires ef >= k. Default ef is small; raise it for the
        # query so larger k values do not throw.
        try:
            self.index.set_ef(max(k, 16))
        except Exception:
            pass
        labels, distances = self.index.knn_query(np.array(query_vector), k=k)
        return labels[0].tolist(), distances[0].tolist()
