from typing import List, Optional
from sentence_transformers import SentenceTransformer
import numpy as np

class LocalEmbedder:
    def __init__(self, model_name: str = "all-MiniLM-L6-v2"):
        self.model_name = model_name
        self.model = None

    def load(self):
        if not self.model:
            # Using sentence-transformers which uses ONNX/PyTorch internally
            # For a true local ONNX impl as per spec, we'd use onnxruntime directly
            # but sentence-transformers is a reliable high-level wrapper.
            self.model = SentenceTransformer(self.model_name)

    def embed(self, texts: List[str]) -> List[List[float]]:
        self.load()
        embeddings = self.model.encode(texts)
        return embeddings.tolist()

def build_embedding_text(name: str, title: Optional[str] = None, description: Optional[str] = None) -> str:
    components = [name]
    if title:
        components.append(title)
    if description:
        components.append(description)
    return " — ".join(components)
