"""Generate the Capy Kondo mascot set with Nano Banana 2.1 and cut it out with SAM 3.1."""
import asyncio, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from capykondo import llm, segment, media

OUT = Path(__file__).resolve().parent.parent / "web" / "static" / "img"
STYLE = ("Cute kawaii mascot character: a chubby, calm capybara with a small yuzu (round orange citrus with a green leaf) "
         "balanced on its head. Soft rounded shapes, warm caramel-brown fur, rosy cheeks, gentle smile, thick clean "
         "dark-brown outline, flat sticker illustration with subtle soft shading. Centered full body, plain pure white "
         "background, no text, no shadows on the background.")
POSES = {
    "capy_logo": "Front-facing, holding a small paper price tag with a string, happy.",
    "capy_scan": "Peering through a big magnifying glass, curious and focused.",
    "capy_sold": "Celebrating with both arms up, tiny confetti pieces around it, eyes closed happily.",
    "capy_brain": "Wearing round glasses, pointing at a tiny chalkboard with a check mark on it, proud teacher.",
    "capy_sleep": "Sleeping peacefully curled up, a tiny 'z' bubble, relaxed.",
}

async def one(name, pose, ref=None):
    raw = OUT / f"{name}_raw.jpg"
    prompt = STYLE + " Pose: " + pose + (" Keep the exact same character design as the reference image." if ref else "")
    await llm.nano_banana(prompt, raw, refs=[ref] if ref else None, aspect_ratio="1:1", image_size="1K")
    res = await llm.sam_segment("capybara", image_path=raw)
    dets = [d for d in segment.detections_from_result(res) if d.mask is not None]
    if dets:
        d = max(dets, key=lambda d: d.area_box)
        im = media.load_image(raw)
        cut = segment.cutout(im, d, scale=1.0, pad=8)
        cut.save(OUT / f"{name}.png")
        print(name, "cut", cut.size)
    else:
        print(name, "no capybara found; keeping raw")

async def main():
    await one("capy_logo", POSES["capy_logo"])
    ref = OUT / "capy_logo_raw.jpg"
    await asyncio.gather(*[one(n, p, ref) for n, p in POSES.items() if n != "capy_logo"])

if __name__ == "__main__":
    asyncio.run(main())
