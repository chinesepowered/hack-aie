"""Studio photos: Nano Banana 2.1 paints an empty backdrop; the item itself stays real camera pixels."""
from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

from PIL import Image, ImageFilter

from . import config, llm

BACKDROP_DIR = config.MEDIA_DIR / "backdrops"
BACKDROP_DIR.mkdir(parents=True, exist_ok=True)
# ship pre-generated Nano Banana backdrops so the first live scans are fast
for _f in (config.ROOT / "capykondo" / "seed" / "backdrops").glob("*.jpg"):
    if not (BACKDROP_DIR / _f.name).exists():
        (BACKDROP_DIR / _f.name).write_bytes(_f.read_bytes())
_locks: dict[str, asyncio.Lock] = {}

SURFACES = {
    "clothing": "a softly wrinkled light oatmeal linen cloth laid flat",
    "apparel": "a softly wrinkled light oatmeal linen cloth laid flat",
    "electronics": "a clean matte warm-white desk surface with a soft wall behind it",
    "computer": "a clean matte warm-white desk surface with a soft wall behind it",
    "kitchen": "a warm light oak kitchen counter with a blurred sunny window behind",
    "drinkware": "a warm light oak kitchen counter with a blurred sunny window behind",
    "toy": "a pastel peach paper sweep, playful and soft",
    "toys": "a pastel peach paper sweep, playful and soft",
    "books": "a cozy wooden bookshelf ledge with soft warm light",
    "stationery": "a pale sage green paper sweep",
    "accessories": "a soft cream seamless paper sweep",
}
DEFAULT_SURFACE = "a soft cream seamless paper sweep"


def surface_for(category: str) -> str:
    c = (category or "").lower()
    for k, v in SURFACES.items():
        if k in c:
            return v
    return DEFAULT_SURFACE


async def backdrop(category: str) -> Path:
    surface = surface_for(category)
    key = hashlib.sha1(surface.encode()).hexdigest()[:10]
    path = BACKDROP_DIR / f"{key}.jpg"
    if path.exists():
        return path
    lock = _locks.setdefault(key, asyncio.Lock())
    async with lock:
        if path.exists():
            return path
        prompt = (f"An empty product-photography backdrop: {surface}. Square composition, eye-level, "
                  "soft diffused natural light, gentle falloff, lots of empty space in the center for a product, "
                  "photorealistic, no objects, no products, no people, no text, no logos.")
        await llm.nano_banana(prompt, path, aspect_ratio="1:1", image_size="1K")
    return path


def composite(cutout_path: str | Path, backdrop_path: str | Path, out_path: str | Path, size: int = 1024) -> Path:
    bg = Image.open(backdrop_path).convert("RGB").resize((size, size), Image.LANCZOS)
    item = Image.open(cutout_path).convert("RGBA")
    bbox = item.getchannel("A").getbbox()
    if bbox:
        item = item.crop(bbox)
    max_w, max_h = size * 0.74, size * 0.70
    s = min(max_w / item.width, max_h / item.height)
    item = item.resize((max(1, int(item.width * s)), max(1, int(item.height * s))), Image.LANCZOS)
    x = (size - item.width) // 2
    y = int(size * 0.56 - item.height / 2)
    y = min(y, size - item.height - int(size * 0.06))

    canvas = bg.convert("RGBA")
    # soft drop shadow from the item's own silhouette
    alpha = item.getchannel("A")
    shadow = Image.new("RGBA", item.size, (40, 30, 20, 0))
    shadow.putalpha(alpha.point(lambda a: int(a * 0.32)))
    pad = 40
    sh = Image.new("RGBA", (item.width + 2 * pad, item.height + 2 * pad), (0, 0, 0, 0))
    sh.paste(shadow, (pad, pad), shadow)
    sh = sh.filter(ImageFilter.GaussianBlur(14))
    canvas.alpha_composite(sh, (x - pad + 10, y - pad + 16))
    # contact shadow under the base
    contact = Image.new("RGBA", (item.width, max(8, item.height // 10)), (0, 0, 0, 0))
    from PIL import ImageDraw
    ImageDraw.Draw(contact).ellipse([0, 0, contact.width - 1, contact.height - 1], fill=(30, 20, 10, 70))
    contact = contact.filter(ImageFilter.GaussianBlur(8))
    canvas.alpha_composite(contact, (x, y + item.height - contact.height // 2))
    canvas.alpha_composite(item, (x, y))
    out_path = Path(out_path)
    canvas.convert("RGB").save(out_path, quality=90)
    return out_path


async def studio_photo(cutout_path: Path, category: str, out_path: Path) -> Path:
    bd = await backdrop(category)
    return await asyncio.to_thread(composite, cutout_path, bd, out_path)
