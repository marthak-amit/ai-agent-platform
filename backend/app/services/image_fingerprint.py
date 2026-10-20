"""
Perceptual fingerprints (dHash) of the catalogue's product photos — "is this picture one of OUR products?" without a model call.

A customer who forwards a catalogue picture (or a screenshot of one) sends something near-identical to a product image of the shop. The
64-bit difference hash survives rescaling and re-compression; two pictures within ``MAX_DISTANCE`` bits are the same picture.

The index per tenant is built **in the background, off the event loop** (``ensure_index`` — called when the catalogue snapshot loads
and at startup — schedules it; ``match`` NEVER starts a build and returns ``None`` until the index exists, so a customer turn never waits
for downloads or hashing). It is cached per tenant and rebuilt only when the set of ``(sku, image_url)`` pairs changes, i.e. when the
catalogue's photos change — not on a timer. Only product-level ``image_url``s are indexed (at most ``MAX_PRODUCTS``), with a short
timeout each.

Pillow decoding is CPU work: it runs on a small dedicated thread pool (``_HASH_POOL``, 2 workers) so hashing 200 twelve-megapixel photos
(~17 s of CPU) can neither stall the event loop nor saturate every core; JPEGs are decoded at 1/8 scale (``Image.draft``), ~10× cheaper.
"""

from __future__ import annotations

import asyncio
import io
import logging
import time
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger(__name__)

MAX_DISTANCE = 8                 # of 64 bits
MAX_PRODUCTS = 200
FETCH_TIMEOUT = 6.0
_CONCURRENCY = 8

_indexes: dict[int, tuple[float, dict[str, int]]] = {}       # client_id → (built at, sku → hash)
_signatures: dict[int, frozenset[tuple[str, str]]] = {}      # client_id → the (sku, image_url) pairs the index was built from
_building: set[int] = set()
_tasks: set[asyncio.Task] = set()
_HASH_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="dhash")


def dhash(image_bytes: bytes) -> int | None:
    """64-bit difference hash of an image (None when it cannot be decoded)."""
    try:
        from PIL import Image

        image = Image.open(io.BytesIO(image_bytes))
        image.draft("L", (64, 64))        # JPEG only: decode at 1/2–1/8 scale instead of the full 12 MP (a no-op for other formats)
        image = image.convert("L").resize((9, 8), Image.BILINEAR)
        pixels = list(image.tobytes())
        bits = 0
        for row in range(8):
            for col in range(8):
                bits = (bits << 1) | (1 if pixels[row * 9 + col] > pixels[row * 9 + col + 1] else 0)
        return bits
    except Exception:  # noqa: BLE001 - not an image
        return None


def distance(a: int, b: int) -> int:
    """Hamming distance of two hashes."""
    return bin(a ^ b).count("1")


async def _fetch(url: str) -> bytes | None:
    """Download one catalogue image (None on any failure)."""
    try:
        import httpx

        async with httpx.AsyncClient(timeout=FETCH_TIMEOUT, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            return resp.content
    except Exception:  # noqa: BLE001
        return None


async def build_index(products: list[tuple[str, str]], fetch=None) -> dict[str, int]:
    """sku → hash for ``(sku, image_url)`` pairs (``fetch`` is the injectable download seam)."""
    fetch = fetch or _fetch
    sem = asyncio.Semaphore(_CONCURRENCY)
    out: dict[str, int] = {}

    async def one(sku: str, url: str) -> None:
        """Hash one product image."""
        async with sem:
            data = await fetch(url)
        h = await asyncio.get_running_loop().run_in_executor(_HASH_POOL, dhash, data) if data else None
        if h is not None:
            out[sku] = h

    await asyncio.gather(*(one(s, u) for s, u in products[:MAX_PRODUCTS]))
    return out


def find(index: dict[str, int], image_bytes: bytes) -> str | None:
    """The SKU whose catalogue image is within ``MAX_DISTANCE`` of this picture (the closest), else None."""
    h = dhash(image_bytes)
    if h is None or not index:
        return None
    best = min(((distance(h, v), sku) for sku, v in index.items()), default=None)
    return best[1] if best and best[0] <= MAX_DISTANCE else None


def set_index(client_id: int, index: dict[str, int]) -> None:
    """Install a ready-made index (tests; the background builder)."""
    _indexes[client_id] = (time.monotonic(), index)


def clear() -> None:
    """Forget every index (tests)."""
    _indexes.clear()
    _signatures.clear()
    _building.clear()


async def _build_in_background(client_id: int, products: list[tuple[str, str]], signature: frozenset) -> None:
    """Build and store a tenant's index; never raises."""
    try:
        _indexes[client_id] = (time.monotonic(), await build_index(products))
        _signatures[client_id] = signature
        logger.info("IMAGE_INDEX client=%s built hashes=%d", client_id, len(_indexes[client_id][1]))
    except Exception as exc:  # noqa: BLE001
        logger.warning("IMAGE_INDEX client=%s build failed: %s", client_id, exc)
    finally:
        _building.discard(client_id)


def ensure_index(client_id: int, products: "list[tuple[str, str]]") -> bool:
    """
    Make sure a tenant's index matches its catalogue photos: a no-op when it was built from exactly these ``(sku, image_url)`` pairs
    (or is being built), otherwise a background rebuild is scheduled. O(1) for the caller — nothing is downloaded or hashed here.
    Returns True when a build was scheduled.
    """
    pairs = [(s, u) for s, u in products if s and u][:MAX_PRODUCTS]
    if not pairs:
        return False
    signature = frozenset(pairs)
    if client_id in _building or _signatures.get(client_id) == signature:
        return False
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return False                       # no loop (sync caller / unit test): the startup warm-up or the next snapshot load schedules it
    _building.add(client_id)
    task = loop.create_task(_build_in_background(client_id, pairs, signature))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return True


def match(client_id: int, image_bytes: bytes, products: "list[tuple[str, str]] | None" = None) -> str | None:
    """
    The SKU of a catalogue picture this image is — cached index only; this function never starts a build and never waits. (`products` is
    accepted for older callers and ignored: ``ensure_index`` owns building.) Hashing the one query image is CPU work; ``match_async`` runs
    it off the event loop.
    """
    entry = _indexes.get(client_id)
    return find(entry[1], image_bytes) if entry else None


async def match_async(client_id: int, image_bytes: bytes) -> str | None:
    """``match`` in the hashing thread pool — the form the customer-turn paths use, so decoding a photo never holds the event loop."""
    return await asyncio.get_running_loop().run_in_executor(_HASH_POOL, lambda: match(client_id, image_bytes))
