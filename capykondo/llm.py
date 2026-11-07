"""Thin clients for Meta Model API (Muse + SAM 3.1) and the Gemini Interactions API."""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import mimetypes
import time
from pathlib import Path
from typing import Any

from google import genai
from openai import AsyncOpenAI

from . import config

log = logging.getLogger("capy.llm")

_meta: AsyncOpenAI | None = None
_gemini: genai.Client | None = None


def meta() -> AsyncOpenAI:
    global _meta
    if _meta is None:
        _meta = AsyncOpenAI(base_url=config.META_BASE_URL, api_key=config.MODEL_API_KEY,
                            timeout=600, max_retries=3)
    return _meta


def gemini() -> genai.Client:
    global _gemini
    if _gemini is None:
        _gemini = genai.Client(api_key=config.GEMINI_API_KEY)
    return _gemini


def data_url(path: str | Path, mime: str | None = None) -> str:
    path = Path(path)
    mime = mime or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode()


def b64(path: str | Path) -> str:
    return base64.b64encode(Path(path).read_bytes()).decode()


# ----------------------------------------------------------------------------------------
# Meta: Muse
# ----------------------------------------------------------------------------------------

async def upload_meta_file(path: str | Path) -> str:
    with open(path, "rb") as f:
        up = await meta().files.create(purpose="user_data", file=f)
    return up.id


async def muse_json(content: list[dict], schema: dict, *, name: str = "result",
                    model: str | None = None, effort: str = "low",
                    web_search: bool = False, instructions: str | None = None) -> tuple[dict, dict]:
    """Call Muse on the Responses API with a strict JSON schema.

    Returns (parsed_json, meta) where meta carries citations and timing.
    """
    model = model or config.MUSE_FAST
    kwargs: dict[str, Any] = dict(
        model=model,
        input=[{"type": "message", "role": "user", "content": content}],
        text={"format": {"type": "json_schema", "name": name, "schema": schema, "strict": True}},
        reasoning={"effort": effort},
    )
    if instructions:
        kwargs["instructions"] = instructions
    if web_search:
        kwargs["tools"] = [{"type": "web_search"}]
        kwargs["include"] = ["web_search_call.results"]
    t0 = time.time()
    resp = await meta().responses.create(**kwargs)
    text = resp.output_text or ""
    parsed = _loads_loose(text)
    citations: list[dict] = []
    searches = 0
    for item in resp.output or []:
        if getattr(item, "type", "") == "web_search_call":
            searches += 1
        if getattr(item, "type", "") == "message":
            for c in getattr(item, "content", []) or []:
                for a in getattr(c, "annotations", []) or []:
                    if getattr(a, "type", "") == "url_citation":
                        citations.append({"url": a.url, "title": getattr(a, "title", "")})
    return parsed, {"model": model, "seconds": round(time.time() - t0, 1),
                    "citations": citations, "searches": searches}


async def muse_text(messages: list[dict], *, model: str | None = None, effort: str = "low",
                    instructions: str | None = None, max_output_tokens: int | None = None) -> str:
    kwargs: dict[str, Any] = dict(model=model or config.MUSE_FAST, input=messages,
                                  reasoning={"effort": effort})
    if instructions:
        kwargs["instructions"] = instructions
    if max_output_tokens:
        kwargs["max_output_tokens"] = max_output_tokens
    resp = await meta().responses.create(**kwargs)
    return (resp.output_text or "").strip()


def _loads_loose(text: str) -> dict:
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start:end + 1])
        raise


# ----------------------------------------------------------------------------------------
# Meta: SAM 3.1
# ----------------------------------------------------------------------------------------

async def sam_segment(phrase_or_prompt: str, *, image_path: str | Path | None = None,
                      video_file_id: str | None = None, include_confidence: bool = True,
                      metadata: dict | None = None):
    """Run SAM 3.1 and return the parsed result (meta_sam_parser result object)."""
    import meta_sam_parser as msp

    if image_path is not None:
        media = {"type": "input_image", "image_url": data_url(image_path)}
        fmt = msp.image_segmentation_format()
    elif video_file_id is not None:
        media = {"type": "input_video", "file_id": video_file_id}
        fmt = msp.video_segmentation_format()
    else:
        raise ValueError("need image_path or video_file_id")
    md = {"include_confidence": "true"} if include_confidence else {}
    if metadata:
        md.update(metadata)
    for attempt in range(3):
        try:
            stream = await meta().responses.create(
                model=config.SAM, stream=True, metadata=md or None,
                input=[{"type": "message", "role": "user", "content": [
                    {"type": "input_text", "text": phrase_or_prompt}, media]}],
            )
            parsed = msp.parse_responses_stream(stream, fmt)
            fr = parsed.final_result
            return await (fr() if callable(fr) else fr)
        except Exception as e:  # noqa: BLE001 - retry transient API errors
            log.warning("SAM attempt %d failed for %r: %s", attempt + 1, phrase_or_prompt, e)
            if attempt == 2:
                raise
            await asyncio.sleep(1.5 * (attempt + 1))


# ----------------------------------------------------------------------------------------
# Google: Gemini 4 Argon + Nano Banana 2.1
# ----------------------------------------------------------------------------------------

async def _gemini_call(kwargs: dict[str, Any]):
    """Interactions API call on the SDK's native async client, with a few retries."""
    last: Exception | None = None
    for attempt in range(3):
        try:
            return await gemini().aio.interactions.create(**kwargs)
        except Exception as e:  # noqa: BLE001
            last = e
            log.warning("gemini attempt %d failed (%s): %s", attempt + 1, kwargs.get("model"), e)
            await asyncio.sleep(1.5 * (attempt + 1))
    raise last  # type: ignore[misc]

async def gemini_json(parts: list[dict] | str, schema: dict, *, model: str | None = None,
                      thinking: str = "low", system: str | None = None) -> tuple[dict, dict]:
    model = model or config.ARGON

    kwargs: dict[str, Any] = dict(
        model=model, input=parts,
        response_format={"type": "text", "mime_type": "application/json", "schema": schema},
        generation_config={"thinking_level": thinking},
    )
    if system:
        kwargs["system_instruction"] = system
    t0 = time.time()
    it = await _gemini_call(kwargs)
    parsed = _loads_loose(it.output_text or "{}")
    return parsed, {"model": model, "seconds": round(time.time() - t0, 1)}


async def gemini_text(parts: list[dict] | str, *, model: str | None = None, thinking: str = "low",
                      system: str | None = None) -> str:
    model = model or config.FLASH

    kwargs: dict[str, Any] = dict(model=model, input=parts, generation_config={"thinking_level": thinking})
    if system:
        kwargs["system_instruction"] = system
    it = await _gemini_call(kwargs)
    return (it.output_text or "").strip()


async def nano_banana(prompt: str, out_path: str | Path, *, refs: list[str | Path] | None = None,
                      aspect_ratio: str = "1:1", image_size: str = "1K") -> Path:
    """Generate (or edit, with reference images) an image with Nano Banana 2.1; writes a JPEG."""

    if refs:
        parts: list[dict] = [{"type": "text", "text": prompt}]
        for r in refs:
            mime = mimetypes.guess_type(str(r))[0] or "image/jpeg"
            parts.append({"type": "image", "data": b64(r), "mime_type": mime})
        inp: Any = parts
    else:
        inp = prompt
    it = await _gemini_call(dict(
        model=config.NANO_BANANA, input=inp,
        response_format={"type": "image", "mime_type": "image/jpeg",
                         "aspect_ratio": aspect_ratio, "image_size": image_size}))
    img = it.output_image
    if img is None:
        raise RuntimeError("Nano Banana returned no image")
    out_path = Path(out_path)
    out_path.write_bytes(base64.b64decode(img.data))
    return out_path
