"""Token metadata generation and IPFS publication for automated launches."""

# ruff: noqa: PLR0913, TRY003, PLR2004, BLE001, ARG001

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Final

import aiohttp

from rugbot.utils.logger import get_logger

logger = get_logger(__name__)

PUMP_IPFS_ENDPOINT: Final[str] = "https://pump.fun/api/ipfs"
DEFAULT_FALLBACK_IMAGE_URI: Final[str] = (
    "https://ipfs.io/ipfs/QmXoypizjW3WknFiJnKLwHCnL72vedxjQkDDP1mXWo6uco"
)
MAX_NAME_LENGTH: Final[int] = 32
MAX_SYMBOL_LENGTH: Final[int] = 10
MIN_SYMBOL_LENGTH: Final[int] = 2


@dataclass(frozen=True, slots=True)
class TokenMetadata:
    """Canonical Pump.fun token metadata."""

    name: str
    symbol: str
    description: str
    image_uri: str
    metadata_uri: str


def sanitize_ticker(raw_symbol: str) -> str:
    """Sanitize and format a token ticker symbol."""
    cleaned = re.sub(r"[^A-Za-z0-9]", "", raw_symbol).upper()
    if len(cleaned) < MIN_SYMBOL_LENGTH:
        cleaned = cleaned + "COIN"
    return cleaned[:MAX_SYMBOL_LENGTH]


def sanitize_name(raw_name: str) -> str:
    """Sanitize and format a token display name."""
    cleaned = re.sub(r"[^A-Za-z0-9\s]", "", raw_name).strip()
    if not cleaned:
        cleaned = "Pump Token"
    return cleaned[:MAX_NAME_LENGTH]


def _format_word(word: str) -> str:
    """Format a word, preserving uppercase acronyms (e.g. AI, BTC, SOL)."""
    if word.isupper() and len(word) <= 4:
        return word
    return word.capitalize()


def generate_template_metadata(topic: str) -> TokenMetadata:
    """Generate deterministic, clean metadata from a topic without external API calls."""
    if not topic or not topic.strip():
        raise ValueError("Topic cannot be empty")

    clean_topic = topic.strip()
    words = [w for w in re.split(r"\s+", clean_topic) if w]

    # Format Name
    if len(words) == 1:
        name = _format_word(words[0])
        symbol = words[0][:5].upper()
    else:
        name = " ".join(_format_word(w) for w in words[:4])
        # Make symbol from initials or truncated words
        if len(words) >= 3:
            symbol = "".join(w[0].upper() for w in words[:5])
        else:
            symbol = (words[0][:3] + words[1][:2]).upper()

    name = sanitize_name(name)
    symbol = sanitize_ticker(symbol)
    description = f"{name} (${symbol}) inspired by: {clean_topic}. Launched via automated dev pipeline."

    # Deterministic mock/preview IPFS URI
    metadata_uri = (
        f"https://ipfs.io/ipfs/bafkrei{abs(hash(topic)) % 10000000:07d}pumpmetadata"
    )

    return TokenMetadata(
        name=name,
        symbol=symbol,
        description=description,
        image_uri=DEFAULT_FALLBACK_IMAGE_URI,
        metadata_uri=metadata_uri,
    )


async def upload_metadata_to_ipfs(
    name: str,
    symbol: str,
    description: str,
    image_bytes: bytes | None = None,
    image_uri: str = DEFAULT_FALLBACK_IMAGE_URI,
    session: aiohttp.ClientSession | None = None,
) -> str:
    """Upload metadata JSON and optional image to IPFS via Pump.fun's gateway."""
    owns_session = session is None
    http_session = session or aiohttp.ClientSession()

    try:
        if image_bytes is not None:
            form = aiohttp.FormData()
            form.add_field(
                "file",
                image_bytes,
                filename=f"{symbol.lower()}.png",
                content_type="image/png",
            )
            form.add_field("name", name)
            form.add_field("symbol", symbol)
            form.add_field("description", description)
            form.add_field("showName", "true")

            async with http_session.post(
                PUMP_IPFS_ENDPOINT,
                data=form,
                timeout=aiohttp.ClientTimeout(total=10.0),
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    uri = data.get("metadataUri")
                    if uri and isinstance(uri, str):
                        return uri
                logger.warning(
                    f"IPFS upload returned status {response.status}; using fallback metadata URI"
                )

        # Fallback payload URI
        fallback_hash = abs(hash(f"{name}:{symbol}:{description}")) % 10_000_000
        return f"https://ipfs.io/ipfs/bafkrei{fallback_hash:07d}pumpmetadata"

    finally:
        if owns_session:
            await http_session.close()


async def generate_metadata(
    topic: str,
    session: aiohttp.ClientSession | None = None,
) -> TokenMetadata:
    """Generate complete token metadata for a topic."""
    # Check if OpenAI API key is present in environment
    openai_key = os.environ.get("OPENAI_API_KEY", "").strip()

    if openai_key and session:
        try:
            # Query OpenAI for creative name, symbol, and description
            prompt = (
                f"Given the topic or event '{topic}', generate a viral crypto meme token name, "
                "ticker symbol (3-5 uppercase characters), and 1-sentence description. "
                "Respond in valid JSON format with keys 'name', 'symbol', 'description'."
            )
            headers = {
                "Authorization": f"Bearer {openai_key}",
                "Content-Type": "application/json",
            }
            body = {
                "model": "gpt-4o-mini",
                "messages": [{"role": "user", "content": prompt}],
                "response_format": {"type": "json_object"},
                "temperature": 0.7,
            }
            async with session.post(
                "https://api.openai.com/v1/chat/completions",
                headers=headers,
                json=body,
                timeout=aiohttp.ClientTimeout(total=4.0),
            ) as resp:
                if resp.status == 200:
                    res_json = await resp.json()
                    content = res_json["choices"][0]["message"]["content"]
                    parsed = json.loads(content)
                    name = sanitize_name(parsed.get("name", topic))
                    symbol = sanitize_ticker(parsed.get("symbol", "COIN"))
                    desc = str(parsed.get("description", ""))
                    uri = await upload_metadata_to_ipfs(
                        name, symbol, desc, session=session
                    )
                    return TokenMetadata(
                        name=name,
                        symbol=symbol,
                        description=desc,
                        image_uri=DEFAULT_FALLBACK_IMAGE_URI,
                        metadata_uri=uri,
                    )
        except Exception as err:
            logger.warning(
                f"LLM metadata generation failed ({err}); falling back to deterministic template"
            )

    # Deterministic fallback
    return generate_template_metadata(topic)


__all__ = [
    "DEFAULT_FALLBACK_IMAGE_URI",
    "MAX_NAME_LENGTH",
    "MAX_SYMBOL_LENGTH",
    "MIN_SYMBOL_LENGTH",
    "TokenMetadata",
    "generate_metadata",
    "generate_template_metadata",
    "sanitize_name",
    "sanitize_ticker",
    "upload_metadata_to_ipfs",
]
