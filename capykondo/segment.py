"""Turn SAM 3.1 results into per-object tracks, masks, SVG outlines and real-pixel cutouts."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageFilter

import meta_sam_parser as msp


def clean_raster(arr: np.ndarray) -> np.ndarray:
    """Keep the largest connected blob (plus big secondary blobs) and fill small holes."""
    a = (arr > 0).astype(np.uint8)
    if a.sum() == 0:
        return a
    n, labels, stats, _ = cv2.connectedComponentsWithStats(a, connectivity=8)
    if n > 2:
        areas = stats[1:, cv2.CC_STAT_AREA]
        biggest = areas.max()
        keep = [i + 1 for i, ar in enumerate(areas) if ar >= 0.4 * biggest]
        a = np.isin(labels, keep).astype(np.uint8)
    # fill holes: flood fill background from the border
    inv = (1 - a).astype(np.uint8)
    n2, lab2, st2, _ = cv2.connectedComponentsWithStats(inv, connectivity=4)
    h, w = a.shape
    for i in range(1, n2):
        x, y, bw, bh, area = st2[i]
        touches = x == 0 or y == 0 or x + bw >= w or y + bh >= h
        if not touches and area < 0.05 * a.size:
            a[lab2 == i] = 1
    return a


def _outline_from_raster(r: np.ndarray, bounds: tuple[int, int, int, int], epsilon: float, max_points: int) -> str:
    contours, _ = cv2.findContours((r * 255).astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return ""
    l, t, rr, b = bounds
    sx = (rr - l) / max(1, r.shape[1])
    sy = (b - t) / max(1, r.shape[0])
    parts = []
    for c in sorted(contours, key=cv2.contourArea, reverse=True)[:3]:
        if parts and cv2.contourArea(c) < 0.02 * r.size:
            continue
        eps = epsilon
        approx = cv2.approxPolyDP(c, eps, True)
        while len(approx) > max_points:
            eps *= 1.5
            approx = cv2.approxPolyDP(c, eps, True)
        pts = approx.reshape(-1, 2)
        if len(pts) < 3:
            continue
        parts.append("M" + "L".join(f"{l + x * sx:.1f},{t + y * sy:.1f}" for x, y in pts) + "Z")
    return "".join(parts)


def outline_job(args: tuple) -> str:
    """Process-pool friendly: decode one SAM mask and return its simplified outline path."""
    encoding, payload, width, height, bounds, epsilon, max_points = args
    m = msp.SegmentationMask(encoding=encoding, payload=payload, width=width, height=height)
    raw = msp.decode_mask_to_raster(m)
    r = clean_raster(np.frombuffer(raw, dtype=np.uint8).reshape(height, width))
    return _outline_from_raster(r, bounds, epsilon, max_points)


_NUM = None


def refit_path(path: str, src_box: tuple[float, float, float, float], dst_box: tuple[float, float, float, float]) -> str:
    """Map an outline drawn for src_box onto dst_box (translate + scale)."""
    import re
    global _NUM
    if _NUM is None:
        _NUM = re.compile(r"(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)")
    sl, st, sr, sb = src_box
    dl, dt, dr, db = dst_box
    kx = (dr - dl) / max(1e-6, sr - sl)
    ky = (db - dt) / max(1e-6, sb - st)
    return _NUM.sub(lambda m: f"{dl + (float(m.group(1)) - sl) * kx:.1f},{dt + (float(m.group(2)) - st) * ky:.1f}", path)


@dataclass
class Detection:
    object_id: str
    frame: int
    left: int
    top: int
    right: int
    bottom: int
    confidence: float | None
    mask: object = None          # meta_sam_parser.SegmentationMask (box-local raster)
    bounds: object = None        # SegmentationMaskBounds

    @property
    def area_box(self) -> int:
        return max(0, self.right - self.left) * max(0, self.bottom - self.top)

    def raster(self) -> np.ndarray:
        """Box-local binary mask as uint8 array of shape (h, w), cleaned (largest blob, holes filled)."""
        if getattr(self, "_raster", None) is None:
            raw = msp.decode_mask_to_raster(self.mask)
            arr = np.frombuffer(raw, dtype=np.uint8).reshape(self.mask.height, self.mask.width)
            self._raster = clean_raster(arr)
        return self._raster

    def outline(self, epsilon: float = 1.2, max_points: int = 120) -> str:
        """Simplified SVG path (source-media pixels) of the mask outline."""
        b = self.bounds
        return _outline_from_raster(self.raster(), (b.left, b.top, b.right, b.bottom), epsilon, max_points)

    def job_args(self, epsilon: float = 1.5, max_points: int = 60) -> tuple:
        b = self.bounds
        return (self.mask.encoding, self.mask.payload, self.mask.width, self.mask.height,
                (b.left, b.top, b.right, b.bottom), epsilon, max_points)

    def mask_area(self) -> int:
        return int(self.raster().sum())

    def svg_path(self) -> str:
        """SVG path in source-media pixel coordinates."""
        path = msp.decode_mask_to_svg_path(self.mask)
        if not path:
            return ""
        ox, oy = self.bounds.left, self.bounds.top
        sx = (self.bounds.right - self.bounds.left) / max(1, self.mask.width)
        sy = (self.bounds.bottom - self.bounds.top) / max(1, self.mask.height)
        return _transform_path(path, ox, oy, sx, sy)

    def contains(self, x: float, y: float) -> bool:
        if not (self.left <= x < self.right and self.top <= y < self.bottom):
            return False
        try:
            r = self.raster()
            bx = (x - self.bounds.left) * self.mask.width / max(1, self.bounds.right - self.bounds.left)
            by = (y - self.bounds.top) * self.mask.height / max(1, self.bounds.bottom - self.bounds.top)
            return bool(r[min(r.shape[0] - 1, max(0, int(by))), min(r.shape[1] - 1, max(0, int(bx)))])
        except Exception:  # noqa: BLE001
            return True


def _transform_path(path: str, ox: float, oy: float, sx: float, sy: float) -> str:
    out: list[str] = []
    tokens = path.replace(",", " ").replace("M", " M ").replace("L", " L ").replace("Z", " Z ").split()
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if t in ("M", "L"):
            x, y = float(tokens[i + 1]), float(tokens[i + 2])
            out.append(f"{t}{ox + x * sx:.1f},{oy + y * sy:.1f}")
            i += 3
        elif t == "Z":
            out.append("Z")
            i += 1
        else:  # implicit continuation of L
            x, y = float(tokens[i]), float(tokens[i + 1])
            out.append(f"L{ox + x * sx:.1f},{oy + y * sy:.1f}")
            i += 2
    return "".join(out)


@dataclass
class Track:
    object_id: str
    detections: dict[int, Detection] = field(default_factory=dict)

    @property
    def frames(self) -> list[int]:
        return sorted(self.detections)

    def mean_area(self) -> float:
        if not self.detections:
            return 0.0
        return float(np.mean([d.area_box for d in self.detections.values()]))


def detections_from_result(result) -> list[Detection]:
    boxes = {}
    masks = {}
    for r in result.records:
        fi = r.frame.frame_index if getattr(r, "frame", None) else 0
        key = (r.object_id, fi)
        if r.kind == "box":
            boxes[key] = r
        elif r.kind == "mask":
            masks[key] = r
    dets = []
    for key, b in boxes.items():
        m = masks.get(key)
        dets.append(Detection(object_id=str(b.object_id), frame=key[1], left=int(b.left), top=int(b.top),
                              right=int(b.right), bottom=int(b.bottom), confidence=b.confidence,
                              mask=m.mask if m else None, bounds=m.bounds if m else None))
    return dets


def tracks_from_result(result) -> dict[str, Track]:
    tracks: dict[str, Track] = {}
    for d in detections_from_result(result):
        if d.mask is None:
            continue
        tracks.setdefault(d.object_id, Track(d.object_id)).detections[d.frame] = d
    return tracks


def pick_detection(dets: list[Detection], point: tuple[float, float] | None) -> Detection | None:
    """Pick the instance under the reference point, else the closest / most confident one."""
    dets = [d for d in dets if d.mask is not None]
    if not dets:
        return None
    if point is not None:
        x, y = point
        hits = [d for d in dets if d.contains(x, y)]
        if hits:
            return min(hits, key=lambda d: d.area_box)  # innermost match
        def dist(d: Detection) -> float:
            cx, cy = (d.left + d.right) / 2, (d.top + d.bottom) / 2
            return (cx - x) ** 2 + (cy - y) ** 2
        return min(dets, key=dist)
    return max(dets, key=lambda d: (d.confidence or 0) * d.area_box)


def cutout(image: Image.Image, det: Detection, scale: float = 1.0, pad: int = 6) -> Image.Image:
    """Real-pixel RGBA cutout of the detection from `image`.

    `scale` maps detection coordinates (in the analysed image) to `image` coordinates.
    """
    r = det.raster().astype(np.uint8) * 255
    bw = max(1, det.bounds.right - det.bounds.left)
    bh = max(1, det.bounds.bottom - det.bounds.top)
    left = int(det.bounds.left * scale)
    top = int(det.bounds.top * scale)
    w = max(1, int(round(bw * scale)))
    h = max(1, int(round(bh * scale)))
    mask = Image.fromarray(r, mode="L").resize((w, h), Image.BILINEAR)
    mask = mask.filter(ImageFilter.GaussianBlur(radius=max(0.6, scale * 0.6)))
    crop = image.crop((left, top, left + w, top + h)).convert("RGBA")
    crop.putalpha(mask)
    canvas = Image.new("RGBA", (w + 2 * pad, h + 2 * pad), (0, 0, 0, 0))
    canvas.paste(crop, (pad, pad), crop)
    return canvas


def bbox_crop(image: Image.Image, det: Detection, scale: float = 1.0, margin: float = 0.12) -> Image.Image:
    """Plain rectangular crop around the detection with some context (for inspection prompts)."""
    w, h = image.size
    l, t, r, b = det.left * scale, det.top * scale, det.right * scale, det.bottom * scale
    mx, my = (r - l) * margin, (b - t) * margin
    return image.crop((max(0, int(l - mx)), max(0, int(t - my)), min(w, int(r + mx)), min(h, int(b + my))))


def save_rgba(im: Image.Image, path: str | Path) -> Path:
    im.save(path, format="PNG")
    return Path(path)
