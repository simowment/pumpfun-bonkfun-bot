"""Point-in-time capture of a launch's metadata JSON and image.

The create event only carries a metadata ``uri``. Its JSON (description,
image, twitter/telegram/website links) and image are the coin's narrative, and
they can be edited or deleted later, so the collector fetches them once at
launch and stores them with the time they were seen.
"""

from __future__ import annotations

import asyncio
import io
import time
from dataclasses import dataclass

import aiohttp
from PIL import Image

# ipfs.io answers 429 to plain HTTP clients (service-worker gateway only), and
# Pump's pinata gateway 403s CIDs it did not pin; Filebase serves any CID.
IPFS_IO_PREFIX = "https://ipfs.io/ipfs/"
IPFS_GATEWAY_PREFIX = "https://ipfs.filebase.io/ipfs/"
FETCH_TIMEOUT_SECONDS = 10
MAX_IMAGE_BYTES = 5 * 1024 * 1024
IMAGE_CHUNK_BYTES = 64 * 1024
THUMBNAIL_PX = 256
THUMBNAIL_WEBP_QUALITY = 80
# 8x8 difference hash: 64 bits, compared with Hamming distance.
DHASH_SIZE = 8

METADATA_OK = "ok"
METADATA_NO_IMAGE = "no_image"
METADATA_ERROR = "metadata_error"
IMAGE_ERROR = "image_error"


@dataclass(frozen=True, slots=True)
class LaunchMetadata:
    """What a launch's metadata ``uri`` served when it was fetched."""

    status: str
    fetched_at_ms: int
    fetch_ms: int
    metadata: dict[str, object] | None = None
    error: str | None = None
    image_dhash: str | None = None
    thumbnail_webp: bytes | None = None


def gateway_url(url: str) -> str:
    """Route ipfs.io links through a gateway that serves plain HTTP clients."""

    if url.startswith(IPFS_IO_PREFIX):
        return IPFS_GATEWAY_PREFIX + url.removeprefix(IPFS_IO_PREFIX)
    return url


def thumbnail_and_dhash(raw: bytes) -> tuple[bytes, str]:
    """Return a WebP thumbnail and the 64-bit difference hash of an image."""

    with Image.open(io.BytesIO(raw)) as image:
        rgb = image.convert("RGB")
    grey = rgb.convert("L").resize((DHASH_SIZE + 1, DHASH_SIZE))
    pixels = list(grey.getdata())
    bits = 0
    for row in range(DHASH_SIZE):
        for col in range(DHASH_SIZE):
            left = pixels[row * (DHASH_SIZE + 1) + col]
            bits = (bits << 1) | (left > pixels[row * (DHASH_SIZE + 1) + col + 1])
    rgb.thumbnail((THUMBNAIL_PX, THUMBNAIL_PX))
    out = io.BytesIO()
    rgb.save(out, format="WEBP", quality=THUMBNAIL_WEBP_QUALITY)
    return out.getvalue(), f"{bits:016x}"


async def _get_metadata(session: aiohttp.ClientSession, uri: str) -> dict[str, object]:
    async with session.get(gateway_url(uri)) as response:
        response.raise_for_status()
        metadata = await response.json(content_type=None)
    if not isinstance(metadata, dict):
        raise TypeError("metadata is not a JSON object")  # noqa: TRY003
    return metadata


async def _get_image(session: aiohttp.ClientSession, url: str) -> bytes:
    async with session.get(gateway_url(url)) as response:
        response.raise_for_status()
        body = bytearray()
        async for chunk in response.content.iter_chunked(IMAGE_CHUNK_BYTES):
            body += chunk
            if len(body) > MAX_IMAGE_BYTES:
                raise ValueError("image exceeds size limit")  # noqa: TRY003
    return bytes(body)


async def fetch_launch_metadata(
    session: aiohttp.ClientSession, uri: str
) -> LaunchMetadata:
    """Fetch a launch's metadata JSON and image; failures become a status."""

    started = time.monotonic()

    def done(
        status: str,
        metadata: dict[str, object] | None = None,
        error: Exception | None = None,
        image: tuple[bytes, str] | None = None,
    ) -> LaunchMetadata:
        return LaunchMetadata(
            status=status,
            fetched_at_ms=int(time.time() * 1000),
            fetch_ms=int((time.monotonic() - started) * 1000),
            metadata=metadata,
            error=None if error is None else f"{type(error).__name__}: {error}",
            thumbnail_webp=None if image is None else image[0],
            image_dhash=None if image is None else image[1],
        )

    try:
        metadata = await _get_metadata(session, uri)
    except (aiohttp.ClientError, TimeoutError, ValueError, TypeError) as exc:
        return done(METADATA_ERROR, error=exc)
    image_url = metadata.get("image")
    if not isinstance(image_url, str) or not image_url:
        return done(METADATA_NO_IMAGE, metadata)
    try:
        raw = await _get_image(session, image_url)
        image = await asyncio.to_thread(thumbnail_and_dhash, raw)
    except (
        aiohttp.ClientError,
        TimeoutError,
        ValueError,
        OSError,
        Image.DecompressionBombError,
    ) as exc:
        return done(IMAGE_ERROR, metadata, error=exc)
    return done(METADATA_OK, metadata, image=image)
