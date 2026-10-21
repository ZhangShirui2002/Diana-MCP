"""Semantic retrieval plugin: embeddings of every knowledge chunk, searched by cosine similarity.

Registered through the diana_mcp.retrievers entry point and inactive until a model is configured:

    python -m diana_mcp knowledge vector-index --model <spec> [--device auto|cuda|cpu]

embeds every chunk of Context.documents() (manual, examples, keywords, script commands, experiences) into
<models>/vectors/<model>/ (models = DIANA_MCP_MODELS_DIR, default <project>/models) and records the model in
state/vector.json. Model specs:

    sentence-transformers:<name>   a Hugging Face model run by sentence-transformers (PyTorch), e.g. BAAI/bge-m3
    fastembed:<name>               an ONNX model run by the fastembed package (CPU, no PyTorch)
    hash                           deterministic token hashing, for tests; no download, no semantics

Building uses the GPU when PyTorch sees one (half precision). The server embeds queries on the CPU by default
("query_device" in vector.json), so it holds no GPU memory. The model is loaded once per server process in the
background (on the first search, or when diana_start begins a DIANA session); searches meanwhile go on without it.
A model downloads once (into <models>/huggingface) and is then kept as a plain copy in <models>/local, loaded
without contacting the Hub. Bare keywords and fragments of the manual's index are not embedded (see indexable()).
Everything runs locally; nothing is sent anywhere. Experience entries change over time: before searching, changed
entries are re-embedded (only those). numpy is used when installed, plain Python otherwise (small indexes only).
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol

from . import config
from .knowledge import Context, Document, Hit, Query
from .textindex import tokenize

CONFIG_FILE = "vector.json"
BATCH = 256  # texts per embed() call; the runtime batches further, sorted by length
MAX_TOKENS = 512  # longer chunks are cut to this many model tokens
DEFAULT_WEIGHT = 1.2

_LOCK = threading.RLock()  # the cached indexes and their updates (fast)
_LOAD_LOCK = threading.Lock()  # loading a model (slow: importing PyTorch alone can take half a minute)
_EMBEDDERS: dict[tuple[str, str], Any] = {}
_LOADING: set[tuple[str, str]] = set()
_LOAD_ERRORS: dict[tuple[str, str], str] = {}
_INDEXES: dict[str, VectorIndex] = {}


class NotReady(RuntimeError):
    """The model is still loading in the background; the search goes on without this retriever."""


def _numpy() -> Any:
    try:
        import numpy
    except ImportError:
        return None
    return numpy


class Embedder(Protocol):
    name: str

    def embed(self, texts: list[str], kind: str) -> Any: ...  # kind "query" or "document"; one row of floats each


def _normalize(vector: Iterable[float]) -> list[float]:
    vector = [float(v) for v in vector]
    norm = math.sqrt(sum(v * v for v in vector)) or 1.0
    return [v / norm for v in vector]


def canonical(spec: str) -> str:
    kind, sep, model = spec.strip().partition(":")
    return f"{'sentence-transformers' if kind == 'st' else kind}{sep}{model}"


class HashEmbedder:
    """Tokens hashed into signed buckets: exact-word overlap only, for testing the plumbing."""

    def __init__(self, dim: int = 256):
        self.name, self.dim = ("hash" if dim == 256 else f"hash:{dim}"), dim

    def embed(self, texts: list[str], kind: str) -> list[list[float]]:
        out = []
        for text in texts:
            vector = [0.0] * self.dim
            for token in tokenize(text):
                digest = hashlib.md5(token.encode("utf-8")).digest()
                vector[int.from_bytes(digest[:4], "little") % self.dim] += 1.0 if digest[4] & 1 else -1.0
            out.append(_normalize(vector))
        return out


class FastEmbedEmbedder:
    def __init__(self, model: str):
        from fastembed import TextEmbedding  # optional dependency

        self.name = f"fastembed:{model}"
        self.model = TextEmbedding(model_name=model, cache_dir=str(config.models_dir() / "fastembed"))
        self.e5 = "e5" in model.lower()  # e5 models expect "query: " / "passage: " prefixes

    def embed(self, texts: list[str], kind: str) -> list[list[float]]:
        if self.e5:
            texts = [("query: " if kind == "query" else "passage: ") + t for t in texts]
        return [list(map(float, v)) for v in self.model.embed(texts)]


class SentenceTransformerEmbedder:
    """A sentence-transformers model on a device ("auto" = the GPU if PyTorch sees one), half precision on a GPU."""

    def __init__(self, model: str, device: str | None = None):
        # Downloads go next to the indexes, not into the user profile, unless the user chose a place. Without
        # Windows developer mode the cache stores copies instead of symlinks, which is fine for one model.
        os.environ.setdefault("HF_HOME", str(config.models_dir() / "huggingface"))
        os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
        import torch
        from sentence_transformers import SentenceTransformer  # optional dependency

        if device in (None, "", "auto"):
            device = "cuda" if torch.cuda.is_available() else "cpu"
        self.name, self.device = f"sentence-transformers:{model}", device
        # After the first download the model loads from a plain local copy: no requests to the Hub at every server
        # start, and one copy on disk (the Hub cache may hold the weights twice, .bin and converted .safetensors).
        # Models that need their own code from the Hub (trust_remote_code) are not supported: nothing downloaded is
        # ever executed.
        local = local_model_dir(model)
        if (local / "modules.json").is_file():
            self.model = SentenceTransformer(str(local), device=device)
        else:
            self.model = SentenceTransformer(model, device=device)
            # Full precision, before any conversion to half precision; complete or not at all.
            partial = local.with_name(local.name + ".partial")
            shutil.rmtree(partial, ignore_errors=True)
            self.model.save(str(partial))
            os.replace(partial, local)
        if device.startswith("cuda"):
            self.model.half()
        self.model.max_seq_length = min(self.model.max_seq_length or MAX_TOKENS, MAX_TOKENS)
        self.e5 = "e5" in model.lower()

    def embed(self, texts: list[str], kind: str) -> Any:
        if self.e5:
            texts = [("query: " if kind == "query" else "passage: ") + t for t in texts]
        return self.model.encode(texts, batch_size=32, normalize_embeddings=True, convert_to_numpy=True,
                                 show_progress_bar=False)


def local_model_dir(model: str) -> Path:
    return config.models_dir() / "local" / slug(model)


def load_embedder(spec: str, device: str | None = None) -> Embedder:
    kind, _, model = canonical(spec).partition(":")
    if kind == "hash":
        return HashEmbedder(int(model) if model.isdigit() else 256)
    if kind == "fastembed":
        return FastEmbedEmbedder(model)
    if kind == "sentence-transformers":
        return SentenceTransformerEmbedder(model, device)
    raise ValueError(f"unknown embedding model spec {spec!r} (use sentence-transformers:<name>, fastembed:<name>, "
                     "hash)")


def shared_embedder(spec: str, device: str = "cpu") -> Embedder:
    """One model per process and device: loading one takes seconds and gigabytes of memory."""
    key = (canonical(spec), device)
    model = _EMBEDDERS.get(key)
    if model is not None:
        return model
    with _LOAD_LOCK:
        model = _EMBEDDERS.get(key)
        if model is None:
            _LOADING.add(key)
            try:
                model = _EMBEDDERS[key] = load_embedder(spec, device)
                _LOAD_ERRORS.pop(key, None)
            except Exception as exc:
                _LOAD_ERRORS[key] = f"{type(exc).__name__}: {exc}"
                raise
            finally:
                _LOADING.discard(key)
    return model


def _slow_to_load(spec: str) -> bool:
    return canonical(spec).partition(":")[0] in ("sentence-transformers", "fastembed")


def load_in_background(spec: str, device: str = "cpu") -> None:
    key = (canonical(spec), device)
    with _LOCK:
        if key in _EMBEDDERS or key in _LOADING:
            return
        _LOADING.add(key)

    def run() -> None:
        try:
            shared_embedder(spec, device)
        except Exception:  # recorded in _LOAD_ERRORS and reported by the next search
            pass
        finally:
            _LOADING.discard(key)

    threading.Thread(target=run, name="diana-mcp-vector-load", daemon=True).start()


class LazyEmbedder:
    """Stands for a model and loads it only when something has to be embedded, so refreshing an index whose chunks
    did not change costs no model load."""

    def __init__(self, spec: str, device: str | None = None):
        self.name, self.device, self._model = canonical(spec), device, None

    def embed(self, texts: list[str], kind: str) -> Any:
        if self._model is None:
            self._model = load_embedder(self.name, self.device)
        return self._model.embed(texts, kind)


MIN_CHARS = 40
_LIST_PAGE = re.compile(r"li\d+\.html$")  # the manual's index and lists of figures/tables


def indexable(document: Document) -> bool:
    """Chunks worth embedding. Bare keywords and fragments of the manual's alphabetical index (42% of all chunks)
    carry no meaning of their own: their vectors match any query a little and push real answers down. Keyword
    search still covers them."""
    return len(document.text.strip()) >= MIN_CHARS and not (
        document.source == "manual" and _LIST_PAGE.search(document.ref))


def slug(spec: str) -> str:
    return re.sub(r"[^\w.-]+", "_", spec).strip("_")


def content_hash(document: Document) -> str:
    return hashlib.sha1(f"{document.title}\n{document.text}".encode("utf-8")).hexdigest()[:16]


class VectorIndex:
    """Normalized vectors of the knowledge chunks, stored per model under <models>/vectors/."""

    def __init__(self, folder: Path):
        self.folder = folder
        self.ids: list[str] = []
        self.hashes: list[str] = []
        self.meta: list[dict[str, Any]] = []
        self.vectors: Any = []  # numpy array (n, dim) when numpy is available, else a list of lists
        self.model = ""
        self.stamp: int | None = None  # index.json's mtime when loaded or saved, to notice rebuilds by others
        self.experience_stamp: tuple | None = None  # the experience entries the vectors were last refreshed for
        self.community_stamp: tuple | None = None  # community commit/entries last refreshed for

    @property
    def exists(self) -> bool:
        return (self.folder / "index.json").is_file()

    def _mtime(self) -> int:
        return (self.folder / "index.json").stat().st_mtime_ns

    def load(self) -> VectorIndex:
        data = json.loads((self.folder / "index.json").read_text(encoding="utf-8"))
        self.model, self.ids, self.hashes, self.meta = data["model"], data["ids"], data["hashes"], data["meta"]
        np = _numpy()
        if np is not None and (self.folder / "vectors.npy").is_file():
            self.vectors = np.load(self.folder / "vectors.npy")
        else:
            self.vectors = json.loads((self.folder / "vectors.json").read_text(encoding="utf-8"))
        if len(self.vectors) != len(self.ids):
            raise ValueError(f"vector index {self.folder} is inconsistent (being rebuilt?); try again")
        self.stamp = self._mtime()
        return self

    def save(self) -> None:
        """Vectors first, then index.json (its mtime marks a complete index), each replaced in one step."""
        self.folder.mkdir(parents=True, exist_ok=True)
        np = _numpy()
        if np is not None:
            np.save(self.folder / "vectors.new.npy", np.asarray(self.vectors, dtype="float32"))
            os.replace(self.folder / "vectors.new.npy", self.folder / "vectors.npy")
        else:
            (self.folder / "vectors.new.json").write_text(json.dumps([list(v) for v in self.vectors]),
                                                          encoding="utf-8")
            os.replace(self.folder / "vectors.new.json", self.folder / "vectors.json")
        (self.folder / "index.new.json").write_text(json.dumps(
            {"model": self.model, "ids": self.ids, "hashes": self.hashes, "meta": self.meta, "built": time.time()},
            ensure_ascii=False), encoding="utf-8")
        os.replace(self.folder / "index.new.json", self.folder / "index.json")
        self.stamp = self._mtime()

    def update(self, documents: Iterable[Document], embedder: Embedder, only_prefix: str | None = None,
               progress: Callable[[int, int], None] | None = None) -> dict[str, int]:
        """Embed new or changed documents and reuse unchanged ones. Without ``only_prefix`` the index becomes
        exactly ``documents``; with it, ``documents`` replace just the chunks whose id starts with the prefix
        (e.g. "experience:") and every other chunk is kept. Vectors of another model are never reused."""
        documents = list(documents)
        same_model = self.model in ("", embedder.name)
        old = {i: (h, n) for n, (i, h) in enumerate(zip(self.ids, self.hashes))} if same_model else {}
        ids: list[str] = []
        hashes: list[str] = []
        meta: list[dict[str, Any]] = []
        origin: list[int | None] = []  # row of the reused vector in the old index, None = embed anew
        todo: list[int] = []
        for document in documents:
            digest = content_hash(document)
            known = old.pop(document.id, None)
            ids.append(document.id)
            hashes.append(digest)
            meta.append({"source": document.source, "ref": document.ref, "title": document.title,
                         "text": document.text[:300], **{k: v for k, v in document.meta.items()
                                                         if k in ("status", "community_status", "confidence",
                                                                  "repository", "commit")}})
            if known and known[0] == digest:
                origin.append(known[1])
            else:
                origin.append(None)
                todo.append(len(ids) - 1)
        if only_prefix:
            for doc_id, (digest, n) in old.items():
                if not doc_id.startswith(only_prefix):
                    ids.append(doc_id)
                    hashes.append(digest)
                    meta.append(self.meta[n])
                    origin.append(n)
        fresh: dict[int, Any] = {}
        for start in range(0, len(todo), BATCH):
            batch = todo[start:start + BATCH]
            texts = [f"{meta[i]['title']}\n{documents[i].text}" for i in batch]
            for i, vector in zip(batch, embedder.embed(texts, "document")):
                fresh[i] = vector
            if progress:
                progress(min(start + BATCH, len(todo)), len(todo))
        np = _numpy()
        if np is not None:
            dim = len(next(iter(fresh.values()))) if fresh else (
                int(np.asarray(self.vectors).shape[1]) if len(self.ids) and np.asarray(self.vectors).ndim == 2 else 0)
            matrix = np.zeros((len(ids), dim), dtype="float32")
            reused = [(row, n) for row, n in enumerate(origin) if n is not None]
            if reused:
                rows, sources = zip(*reused)
                matrix[list(rows)] = np.asarray(self.vectors, dtype="float32")[list(sources)]
            if fresh:
                rows = list(fresh)
                block = np.asarray([fresh[i] for i in rows], dtype="float32")
                norms = np.linalg.norm(block, axis=1, keepdims=True)
                matrix[rows] = block / np.where(norms == 0, 1.0, norms)
            vectors: Any = matrix
        else:
            vectors = [_normalize(fresh[row]) if n is None else list(self.vectors[n]) for row, n in enumerate(origin)]
        self.ids, self.hashes, self.meta, self.vectors, self.model = ids, hashes, meta, vectors, embedder.name
        return {"documents": len(ids), "embedded": len(todo), "reused": len(ids) - len(todo)}

    def search(self, vector: Iterable[float], k: int) -> list[tuple[int, float]]:
        if not self.ids or k <= 0:
            return []
        np = _numpy()
        if np is not None and isinstance(self.vectors, np.ndarray):
            query = np.asarray(vector, dtype="float32").ravel()
            query = query / (np.linalg.norm(query) or 1.0)
            scores = self.vectors @ query
            k = min(k, len(scores))
            top = np.argpartition(-scores, k - 1)[:k] if k < len(scores) else np.arange(len(scores))
            top = top[np.argsort(-scores[top])]
            return [(int(i), float(scores[i])) for i in top]
        query = _normalize(vector)
        scored = [(i, sum(a * b for a, b in zip(row, query))) for i, row in enumerate(self.vectors)]
        return sorted(scored, key=lambda item: -item[1])[:k]


# --- configuration --------------------------------------------------------------------------------------------

def settings_path() -> Path:
    return config.state_dir() / CONFIG_FILE


def stored_settings() -> dict[str, Any]:
    try:
        return json.loads(settings_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def settings() -> dict[str, Any]:
    """vector.json with the environment overrides used for comparisons: DIANA_MCP_VECTOR_MODEL (a spec, or "none"),
    DIANA_MCP_VECTOR_WEIGHT and DIANA_MCP_VECTOR_DEVICE (device for queries)."""
    data = stored_settings()
    model = os.environ.get("DIANA_MCP_VECTOR_MODEL")
    if model is not None:
        data["model"] = None if model.strip().lower() in ("", "none", "off") else canonical(model)
    if os.environ.get("DIANA_MCP_VECTOR_WEIGHT"):
        data["weight"] = float(os.environ["DIANA_MCP_VECTOR_WEIGHT"])
    if os.environ.get("DIANA_MCP_VECTOR_DEVICE"):
        data["query_device"] = os.environ["DIANA_MCP_VECTOR_DEVICE"]
    return data


def configured_model() -> str | None:
    return settings().get("model")


def index_for(spec: str) -> VectorIndex:
    return VectorIndex(config.models_dir() / "vectors" / slug(canonical(spec)))


def cached_index(spec: str) -> VectorIndex | None:
    """The index of a model, loaded once per process and again only after someone rebuilt it."""
    index = index_for(spec)
    if not index.exists:
        return None
    with _LOCK:
        key = str(index.folder)
        cached = _INDEXES.get(key)
        if cached is None or cached.stamp != index._mtime():
            cached = _INDEXES[key] = index.load()
        return cached


def build(context: Context, spec: str, progress: Callable[[int, int], None] | None = None,
          device: str | None = "auto", activate: bool = True) -> dict[str, Any]:
    """Build or refresh the index for a model and (by default) make it the active one. Unchanged chunks are reused;
    the model is loaded only if something needs embedding."""
    spec = canonical(spec)
    index = index_for(spec)
    if index.exists:
        index.load()
    started = time.time()
    documents = [d for d in context.documents() if indexable(d)]
    stats = index.update(documents, LazyEmbedder(spec, device), progress=progress)
    index.save()
    stats["seconds"] = round(time.time() - started, 1)
    if activate:
        keep = {k: v for k, v in stored_settings().items() if k in ("query_device", "weight")}
        settings_path().parent.mkdir(parents=True, exist_ok=True)
        settings_path().write_text(json.dumps({"model": spec, "built": time.time(), **stats, **keep}),
                                   encoding="utf-8")
    return {"model": spec, "folder": str(index.folder), "active": activate, **stats}


def disable() -> None:
    settings_path().unlink(missing_ok=True)


def status() -> dict[str, Any]:
    options = settings()
    spec = options.get("model")
    out: dict[str, Any] = {"model": spec, "query_device": options.get("query_device") or "cpu",
                           "weight": float(options.get("weight") or DEFAULT_WEIGHT)}
    if spec:
        index = index_for(spec)
        out["index"] = str(index.folder)
        out["index_exists"] = index.exists
        for key in ("documents", "built", "seconds"):
            if key in options:
                out[key] = options[key]
    return out


# --- the retriever --------------------------------------------------------------------------------------------

class VectorRetriever:
    name = "vector"

    def __init__(self, context: Context, index: VectorIndex, spec: str, weight: float = DEFAULT_WEIGHT,
                 device: str = "cpu"):
        self.context, self.index, self.spec = context, index, canonical(spec)
        self.weight, self.device = weight, device

    @property
    def embedder(self) -> Embedder:
        return shared_embedder(self.spec, self.device)

    def _refresh_dynamic_sources(self) -> None:
        """Incrementally refresh private and synchronized community experience; manuals are installation-static."""
        private = self.context.experiences.entries()
        sources: list[tuple[str, tuple, str]] = [
            ("experience", tuple(sorted((e.id, e.updated or "", e.status, len(e.body)) for e in private)),
             "experience:"),
        ]
        if self.context.community is not None and self.context.community.ready:
            public = self.context.community.entries()
            sources.append((
                "community",
                (self.context.community.status().get("commit"),
                 tuple(sorted((e.id, e.updated or "", str(e.extra.get("community_status")), len(e.body))
                              for e in public))),
                "community:",
            ))
        changed = False
        for source, stamp, prefix in sources:
            attr = "experience_stamp" if source == "experience" else "community_stamp"
            if stamp == getattr(self.index, attr):
                continue
            documents = [d for d in self.context.documents([source]) if indexable(d)]
            indexed = {i: h for i, h in zip(self.index.ids, self.index.hashes) if i.startswith(prefix)}
            if {d.id: content_hash(d) for d in documents} != indexed:
                before = list(self.index.ids)
                stats = self.index.update(documents, self.embedder, only_prefix=prefix)
                changed = changed or bool(stats["embedded"] or self.index.ids != before)
            setattr(self.index, attr, stamp)
        if changed:
            self.index.save()

    def search(self, query: Query, limit: int) -> list[Hit]:
        key = (self.spec, self.device)
        if key not in _EMBEDDERS and _slow_to_load(self.spec):
            # Never keep a search waiting for PyTorch and a gigabyte model: load them in the background and let
            # the other retrievers answer this time.
            error = _LOAD_ERRORS.pop(key, None)
            load_in_background(self.spec, self.device)
            raise NotReady(f"loading the embedding model failed ({error}); retrying in the background" if error else
                           "the embedding model is loading in the background (up to a minute after a server "
                           "start); this search used the other retrievers only")
        with _LOCK:  # one search at a time touches the shared index and model
            self._refresh_dynamic_sources()
            vector = self.embedder.embed([query.text], "query")[0]
            found = self.index.search(vector, limit * 4)
            best: dict[tuple[str, str], Hit] = {}
            for position, score in found:
                meta = self.index.meta[position]
                if meta.get("status") == "obsolete" or meta.get("community_status") == "obsolete":
                    continue
                key = (meta["source"], meta["ref"])
                if key not in best:
                    extra = {"status_key": meta["status"]} if "status" in meta else {}
                    community_status = meta.get("community_status")
                    if community_status:
                        confidence = float(meta.get("confidence", 0.0))
                        trust = {
                            "candidate": 0.45, "community-confirmed": 0.85,
                            "reproducibly-verified": 1.15, "disputed": 0.2, "obsolete": 0.05,
                        }.get(community_status, 0.2) * (0.75 + 0.5 * confidence)
                        extra.update({
                            "community_status": community_status,
                            "confidence": confidence,
                            "repository": meta.get("repository"),
                            "commit": meta.get("commit"),
                            "trust_factor": round(trust, 3),
                            "status_key": {
                                "candidate": "draft", "community-confirmed": "verified",
                                "reproducibly-verified": "verified", "disputed": "disputed",
                                "obsolete": "obsolete",
                            }.get(community_status, "draft"),
                        })
                    best[key] = Hit(meta["source"], meta["ref"], meta["title"], meta["text"][:200], score, extra)
                if len(best) >= limit:
                    break
        return list(best.values())


def factory(context: Context) -> VectorRetriever | None:
    """Entry point for diana_mcp.retrievers: the configured vector retriever, or None. Index and model are loaded
    once per process and shared by every search."""
    options = settings()
    spec = options.get("model")
    if not spec:
        return None
    index = cached_index(spec)
    if index is None:
        return None
    return VectorRetriever(context, index, spec, float(options.get("weight") or DEFAULT_WEIGHT),
                           options.get("query_device") or "cpu")


def warm_up(background: bool = False) -> bool:
    """Load the configured index and model now (in the background when a DIANA session starts), so searches do not
    have to go without the vector retriever. Returns whether one is configured."""
    options = settings()
    spec = options.get("model")
    if not spec or cached_index(spec) is None:
        return False
    device = options.get("query_device") or "cpu"
    if background:
        load_in_background(spec, device)
    else:
        shared_embedder(spec, device)
    return True
