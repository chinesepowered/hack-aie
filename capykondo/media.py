"""ffmpeg / image helpers."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from PIL import Image, ImageOps


def run(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def probe(path: str | Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=width,height,r_frame_rate,nb_frames:stream_side_data=rotation:format=duration",
         "-of", "json", str(path)],
        check=True, capture_output=True, text=True).stdout
    d = json.loads(out)
    st = (d.get("streams") or [{}])[0]
    num, den = (st.get("r_frame_rate") or "30/1").split("/")
    fps = float(num) / float(den or 1) if float(den or 1) else 30.0
    return {"width": st.get("width"), "height": st.get("height"), "fps": fps,
            "duration": float((d.get("format") or {}).get("duration") or 0)}


def normalize_video(src: str | Path, dst: str | Path, *, width: int, fps: float | None = None,
                    max_seconds: float = 20) -> Path:
    """Re-encode to H.264 MP4 (faststart), display orientation applied, even dims, capped length."""
    vf = [f"scale='min({width},iw)':-2"]
    if fps:
        vf.append(f"fps={fps}")
    vf.append("format=yuv420p")
    run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(src), "-t", str(max_seconds),
         "-vf", ",".join(vf), "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
         "-movflags", "+faststart", str(dst)])
    return Path(dst)


def extract_frame(video: str | Path, t_seconds: float, dst: str | Path) -> Path:
    run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{max(0.0, t_seconds):.3f}", "-i", str(video),
         "-frames:v", "1", "-q:v", "2", str(dst)])
    return Path(dst)


def load_image(path: str | Path) -> Image.Image:
    im = Image.open(path)
    im = ImageOps.exif_transpose(im)
    return im.convert("RGB")


def save_scaled(im: Image.Image, dst: str | Path, max_side: int, quality: int = 90) -> tuple[Path, float]:
    """Save a downscaled copy; returns (path, scale) where scale = new/original."""
    w, h = im.size
    s = min(1.0, max_side / max(w, h))
    if s < 1.0:
        im = im.resize((round(w * s), round(h * s)), Image.LANCZOS)
    im.save(dst, quality=quality)
    return Path(dst), s
