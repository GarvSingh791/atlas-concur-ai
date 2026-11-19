from services.embeddings_provider.factory import get_embeddings_provider
from services.embeddings_provider.interface import EmbeddingsProvider
from services.embeddings_provider.local import LocalEmbeddingsProvider, embed
from services.embeddings_provider.openrouter import OpenRouterEmbeddingsProvider
from services.embeddings_provider.real import RealEmbeddingsProvider

__all__ = [
    "EmbeddingsProvider",
    "LocalEmbeddingsProvider",
    "OpenRouterEmbeddingsProvider",
    "RealEmbeddingsProvider",
    "get_embeddings_provider",
    "embed",
]
