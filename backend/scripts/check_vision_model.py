"""
Check that the configured vision model really accepts image input on Groq.

Sends one tiny generated image (a solid red square) in the same `image_url` content-block
shape vision_service uses, and asks for its colour. Prints the HTTP status, the model's
final answer and PASS/FAIL. Diagnostic only: it writes nothing to the DB or llm_usage.

Run from backend/:
    python scripts/check_vision_model.py [model-id]

Defaults to LLM_MODEL_VISION from backend/.env (or app/config.py). Exit code:
0 model accepted the image and described it · 1 model rejected/garbled it · 2 no API key.
"""

from __future__ import annotations

import base64
import os
import struct
import sys
import zlib

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import Settings  # noqa: E402
from app.services import llm_client  # noqa: E402


def red_square_png(size: int = 64) -> bytes:
    """Build a solid-red RGB PNG without any imaging dependency."""
    def chunk(tag: bytes, data: bytes) -> bytes:
        """One PNG chunk: length, tag, data, CRC."""
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    row = b"\x00" + b"\xff\x00\x00" * size
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(row * size))
        + chunk(b"IEND", b"")
    )


def main() -> int:
    """CLI entry point."""
    try:
        from dotenv import load_dotenv

        load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))
    except ImportError:
        pass
    api_key = os.environ.get("GROQ_API_KEY", "")
    base_url = os.environ.get("GROQ_BASE_URL", Settings.model_fields["groq_base_url"].default)
    model = (sys.argv[1] if len(sys.argv) > 1 else os.environ.get("LLM_MODEL_VISION")) or Settings.model_fields["llm_model_vision"].default
    if not api_key:
        print("ERROR: GROQ_API_KEY is not set (env or backend/.env).")
        return 2

    b64 = base64.b64encode(red_square_png()).decode()
    body = {
        "model": model,
        "max_tokens": 600,
        "temperature": 0,
        "messages": [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
            {"type": "text", "text": "What single colour is this image? Answer with one word."},
        ]}],
    }
    resp = httpx.post(f"{base_url}/chat/completions", json=body, headers={"Authorization": f"Bearer {api_key}"}, timeout=60)
    print(f"model={model} HTTP {resp.status_code}")
    if resp.status_code != 200:
        print("body:", resp.text[:400])
        print("FAIL: model rejected the request — set LLM_MODEL_VISION='' to disable vision cleanly.")
        return 1
    data = resp.json()
    answer = llm_client.strip_reasoning(data["choices"][0]["message"].get("content") or "")
    print(f"answer={answer!r} usage={data.get('usage', {}).get('completion_tokens')} completion tokens")
    if "red" in answer.lower():
        print("PASS: model accepts image input and described it correctly.")
        return 0
    print("FAIL: HTTP 200 but the answer does not describe the image (empty/garbled).")
    return 1


if __name__ == "__main__":
    sys.exit(main())
