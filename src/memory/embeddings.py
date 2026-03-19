"""Embedding generation — supports Ollama (local) or Voyage API."""

import asyncio
import logging
import struct

import aiohttp

log = logging.getLogger(__name__)

VOYAGE_API_URL = "https://api.voyageai.com/v1/embeddings"
OLLAMA_EMBED_URL = "http://127.0.0.1:11434/api/embed"


def embedding_to_blob(embedding: list[float]) -> bytes:
    """Pack a float list into a compact binary blob."""
    return struct.pack(f"{len(embedding)}f", *embedding)


def blob_to_embedding(blob: bytes) -> list[float]:
    """Unpack a binary blob back into a float list."""
    n = len(blob) // 4
    return list(struct.unpack(f"{n}f", blob))


class OllamaEmbeddingClient:
    """Async client for Ollama local embeddings (nomic-embed-text etc.)."""

    def __init__(self, model: str = "nomic-embed-text", dimensions: int = 768):
        self.model = model
        self.dimensions = dimensions
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def embed(self, texts: list[str], input_type: str = "document") -> list[list[float]]:
        """Embed a batch of texts."""
        session = await self._get_session()
        payload = {"model": self.model, "input": texts}

        for attempt in range(3):
            try:
                async with session.post(OLLAMA_EMBED_URL, json=payload) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return data["embeddings"]
                    else:
                        body = await resp.text()
                        log.error("Ollama embed error %d: %s", resp.status, body)
                        raise RuntimeError(f"Ollama embed error {resp.status}: {body}")
            except aiohttp.ClientError as e:
                if attempt == 2:
                    raise
                await asyncio.sleep(2 ** attempt)

        raise RuntimeError("Ollama embeddings: max retries exceeded")

    async def embed_single(self, text: str, input_type: str = "query") -> list[float]:
        results = await self.embed([text], input_type=input_type)
        return results[0]

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()


class EmbeddingClient:
    """Async client for Voyage embeddings API."""

    def __init__(
        self,
        api_key: str,
        model: str = "voyage-3",
        dimensions: int = 1024,
    ):
        self.api_key = api_key
        self.model = model
        self.dimensions = dimensions
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                }
            )
        return self._session

    async def embed(
        self,
        texts: list[str],
        input_type: str = "document",
    ) -> list[list[float]]:
        """Embed a batch of texts. Returns list of embedding vectors."""
        session = await self._get_session()
        payload = {
            "model": self.model,
            "input": texts,
            "input_type": input_type,
            "output_dimension": self.dimensions,
        }

        for attempt in range(4):
            try:
                async with session.post(VOYAGE_API_URL, json=payload) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        results = sorted(data["data"], key=lambda x: x["index"])
                        return [r["embedding"] for r in results]
                    elif resp.status in (429, 500, 502, 503):
                        wait = (2**attempt) + 0.5
                        log.warning("Voyage API %d, retrying in %.1fs", resp.status, wait)
                        await asyncio.sleep(wait)
                    else:
                        body = await resp.text()
                        log.error("Voyage API error %d: %s", resp.status, body)
                        raise RuntimeError(f"Voyage API error {resp.status}: {body}")
            except aiohttp.ClientError as e:
                if attempt == 3:
                    raise
                wait = (2**attempt) + 0.5
                log.warning("Voyage request failed: %s, retrying in %.1fs", e, wait)
                await asyncio.sleep(wait)

        raise RuntimeError("Voyage API: max retries exceeded")

    async def embed_single(self, text: str, input_type: str = "query") -> list[float]:
        results = await self.embed([text], input_type=input_type)
        return results[0]

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
