"""
embeddings.py — text -> 1024-dim vector, via Bedrock Titan v2, cached to disk.

The cache is not an optimisation. Re-embedding the corpus on every dev restart
is the one realistic way to overspend on this project, so every vector that
Bedrock returns is written to .cache/embeddings/ and never paid for twice.

Cache key is sha256(model_id | text). Keying on the model as well as the text
means swapping BEDROCK_EMBED_MODEL invalidates the cache automatically instead
of silently serving vectors from the old model.

Vectors are stored as float32, which is exactly what CockroachDB's VECTOR type
holds — so the cache round-trip is lossless with respect to the database.
"""

import hashlib
import json
import os
import random
import sys
import time
from array import array
from pathlib import Path

try:
    import boto3
    from botocore.exceptions import ClientError
except ImportError:
    sys.exit("boto3 not installed. Run:  pip install boto3 python-dotenv")

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

DIMS = 1024
CACHE_DIR = Path(__file__).parent / ".cache" / "embeddings"

_client = None


def model_id() -> str:
    """Never hardcoded — the Converse/invoke split exists so this is config."""
    m = os.getenv("BEDROCK_EMBED_MODEL")
    if not m:
        sys.exit("BEDROCK_EMBED_MODEL not set in .env")
    return m


def _bedrock():
    global _client
    if _client is None:
        _client = boto3.Session(
            aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
            aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"),
            region_name=os.getenv("AWS_REGION", "eu-west-2"),
        ).client("bedrock-runtime")
    return _client


def fingerprint(text: str, model: str | None = None) -> str:
    """Stable identity of 'this text, embedded by this model'. Also stored on
    the row so the pipeline can spot work it has already done."""
    model = model or model_id()
    return hashlib.sha256(f"{model}|{text}".encode("utf-8")).hexdigest()


def _cache_path(fp: str) -> Path:
    # Two-level fan-out; a flat directory of thousands of files is miserable
    # to work with on Windows.
    return CACHE_DIR / fp[:2] / f"{fp}.f32"


def _cache_read(fp: str) -> list[float] | None:
    p = _cache_path(fp)
    if not p.exists():
        return None
    a = array("f")
    with p.open("rb") as fh:
        a.frombytes(fh.read())
    if len(a) != DIMS:
        # Truncated or half-written file — treat as a miss rather than
        # poisoning the caller with a wrong-width vector.
        return None
    return list(a)


def _cache_write(fp: str, vec: list[float]) -> None:
    p = _cache_path(fp)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    with tmp.open("wb") as fh:
        fh.write(array("f", vec).tobytes())
    tmp.replace(p)  # atomic; a killed process leaves no half-written cache entry


def _invoke(text: str, model: str) -> list[float]:
    """One Bedrock call, with backoff on throttling."""
    body = json.dumps({
        "inputText": text,
        "dimensions": DIMS,
        # Normalised at the source, so L2 distance in the vector index ranks
        # identically to cosine similarity.
        "normalize": True,
    })
    delay = 1.0
    for attempt in range(6):
        try:
            resp = _bedrock().invoke_model(modelId=model, body=body)
            vec = json.loads(resp["body"].read())["embedding"]
            if len(vec) != DIMS:
                raise RuntimeError(
                    f"{model} returned {len(vec)} dims, schema expects {DIMS}"
                )
            return vec
        except ClientError as e:
            code = e.response["Error"]["Code"]
            if code in ("ThrottlingException", "TooManyRequestsException",
                        "ServiceUnavailableException", "ModelTimeoutException"):
                if attempt == 5:
                    raise
                time.sleep(delay + random.random() * 0.3)
                delay *= 2
                continue
            raise
    raise RuntimeError("unreachable")


def embed(text: str, *, model: str | None = None) -> list[float]:
    """Embed one string. Served from disk when we've seen it before."""
    text = (text or "").strip()
    if not text:
        raise ValueError("refusing to embed empty text")
    model = model or model_id()
    fp = fingerprint(text, model)

    cached = _cache_read(fp)
    if cached is not None:
        return cached

    vec = _invoke(text, model)
    _cache_write(fp, vec)
    return vec


def embed_many(texts: list[str], *, model: str | None = None,
               progress: bool = False) -> list[list[float]]:
    """Embed a list, reporting how much of it the cache absorbed."""
    model = model or model_id()
    out, hits = [], 0
    for i, t in enumerate(texts, 1):
        fp = fingerprint(t.strip(), model)
        if _cache_read(fp) is not None:
            hits += 1
        out.append(embed(t, model=model))
        if progress and (i % 25 == 0 or i == len(texts)):
            print(f"  embedded {i}/{len(texts)}  (cache hits: {hits})")
    return out


def to_sql(vec: list[float]) -> str:
    """CockroachDB accepts a VECTOR literal in pgvector's bracket form."""
    return "[" + ",".join(repr(float(x)) for x in vec) + "]"


def cache_stats() -> tuple[int, int]:
    """(files, bytes) currently cached."""
    if not CACHE_DIR.exists():
        return 0, 0
    files = list(CACHE_DIR.rglob("*.f32"))
    return len(files), sum(f.stat().st_size for f in files)
