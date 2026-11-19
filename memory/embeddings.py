from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Protocol, Sequence


class EmbeddingProvider(Protocol):
    model_id: str
    dimensions: int

    @property
    def available(self) -> bool: ...

    @property
    def error(self) -> str: ...

    def warmup(self) -> "EmbeddingStatus": ...

    def encode(self, texts: Sequence[str]) -> list[list[float]]: ...


@dataclass(frozen=True)
class EmbeddingStatus:
    provider: str
    model_id: str
    available: bool
    dimensions: int = 0
    load_ms: float = 0.0
    warmup_ms: float = 0.0
    error: str = ""


class DisabledEmbeddingProvider:
    model_id = "disabled"
    dimensions = 0
    available = False
    error = "embedding provider is disabled"

    def warmup(self) -> EmbeddingStatus:
        return EmbeddingStatus(
            provider="disabled",
            model_id=self.model_id,
            available=False,
            error=self.error,
        )

    def encode(self, texts: Sequence[str]) -> list[list[float]]:
        return []


class SentenceTransformerEmbeddingProvider:
    """Lazy, process-long local sentence-transformer with one explicit warmup.

    Imports stay optional so My-Agent can degrade to BM25 without making the
    normal runtime depend on torch. The model object is retained for the whole
    process and guarded because indexing and retrieval can happen concurrently.
    """

    def __init__(
        self,
        model_id: str,
        *,
        device: str = "cpu",
        local_files_only: bool = True,
    ):
        self.model_id = str(model_id or "BAAI/bge-small-zh-v1.5")
        self.device = str(device or "cpu")
        self.local_files_only = bool(local_files_only)
        self.dimensions = 0
        self._model: Any = None
        self._error = ""
        self._status: EmbeddingStatus | None = None
        self._lock = threading.RLock()

    @property
    def available(self) -> bool:
        return self._model is not None and not self._error

    @property
    def error(self) -> str:
        return self._error

    def warmup(self) -> EmbeddingStatus:
        with self._lock:
            if self._status is not None:
                return self._status

            load_started = time.perf_counter()
            try:
                from sentence_transformers import SentenceTransformer

                self._model = SentenceTransformer(
                    self.model_id,
                    device=self.device,
                    local_files_only=self.local_files_only,
                )
                load_ms = (time.perf_counter() - load_started) * 1000
                warm_started = time.perf_counter()
                vector = self._encode_loaded(["My-Agent memory warmup"])[0]
                warmup_ms = (time.perf_counter() - warm_started) * 1000
                self.dimensions = len(vector)
                self._status = EmbeddingStatus(
                    provider="sentence-transformers",
                    model_id=self.model_id,
                    available=True,
                    dimensions=self.dimensions,
                    load_ms=round(load_ms, 2),
                    warmup_ms=round(warmup_ms, 2),
                )
            except Exception as exc:
                self._model = None
                self._error = f"{type(exc).__name__}: {exc}"
                self._status = EmbeddingStatus(
                    provider="sentence-transformers",
                    model_id=self.model_id,
                    available=False,
                    load_ms=round((time.perf_counter() - load_started) * 1000, 2),
                    error=self._error,
                )
            return self._status

    def encode(self, texts: Sequence[str]) -> list[list[float]]:
        items = [str(text or "") for text in texts]
        if not items:
            return []
        with self._lock:
            if self._status is None:
                self.warmup()
            if not self.available:
                return []
            return self._encode_loaded(items)

    def _encode_loaded(self, texts: Sequence[str]) -> list[list[float]]:
        encoded = self._model.encode(
            list(texts),
            normalize_embeddings=True,
            convert_to_numpy=True,
            show_progress_bar=False,
        )
        return [[float(value) for value in row] for row in encoded]


def create_embedding_provider(
    provider: str,
    *,
    model_id: str,
    device: str = "cpu",
    local_files_only: bool = True,
) -> EmbeddingProvider:
    normalized = str(provider or "disabled").strip().lower()
    if normalized in {"", "disabled", "none", "off"}:
        return DisabledEmbeddingProvider()
    if normalized in {"local", "sentence-transformers", "sentence_transformers"}:
        return SentenceTransformerEmbeddingProvider(
            model_id,
            device=device,
            local_files_only=local_files_only,
        )
    disabled = DisabledEmbeddingProvider()
    disabled.error = f"unknown embedding provider: {provider}"  # type: ignore[misc]
    return disabled
