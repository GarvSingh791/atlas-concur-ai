"""EmbeddingsProvider interface."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod

logger = logging.getLogger(__name__)


class EmbeddingsProvider(ABC):
    """Model-agnostic embeddings interface.

    All implementations must return normalized (unit L2 norm) vectors of
    length EMBEDDING_DIMENSION (1536). Same input → same vector (idempotent).
    """

    @abstractmethod
    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of texts. Returns one vector per text."""
