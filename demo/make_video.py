"""Assemble the Capy Kondo demo video: ElevenLabs narration + recorded scenes + music + burned-in captions.

    python demo/make_video.py            # uses demo/build/raw/*.webm from demo/record.js
Output: demo/build/capykondo_demo.mp4
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")
BUILD = ROOT / "demo" / "build"
RAW = BUILD / "raw"
VOICE = os.environ.get("CAPY_VOICE_ID", "cgSgspJ2msm6clMCkdW9")      # Jessica - playful, bright, warm (young female)
TTS_MODEL = os.environ.get("CAPY_TTS_MODEL", "eleven_v4")
W, H, FPS = 1920, 1080, 30


def narration() -> list[dict]:
    trust = {}
    f = RAW / "trust_item.json"
    if f.exists():
        trust = json.loads(f.read_text() or "{}")
    d = trust.get("data") or {}
    claimed = (d.get("claimed") or {})
    t = d.get("trust") or {}
    if claimed.get("brand") and t.get("brand_visible") is not None:
        kind = (trust.get("name") or "this item").lower()
        bv = round(100 * t["brand_visible"])
        price_line = f"worth {int(claimed['price'])} dollars" if claimed.get("price") else ""
        s3 = (f"But models can be overconfident. Muse guessed this {kind} was {claimed['brand']} {price_line}. "
              "Before anything goes live, Subquadratic's SubQ Snap checks every claim against the real photo, in "
              f"under a second, with calibrated probabilities. Is the brand actually visible? {bv} percent. So the "
              f"code strips the brand, and re-prices it honestly at {int(trust.get('price') or 0)} dollars.")
    else:
        s3 = ("But models can be overconfident, so before anything goes live, Subquadratic's SubQ Snap checks every "
              "claim against the real photo, in under a second, with calibrated probabilities. If a brand isn't "
              "actually visible, the code strips it and re-prices the item honestly.")
    return [
        {"id": "1_title", "text": "Everyone has a pile of stuff they'd sell, if listing it weren't such a chore. "
                                  "Meet Capy Kondo. Point your camera at your clutter, and a very chill capybara "
                                  "sells it for you."},
        {"id": "2_scan", "text": "One photo is all it takes. Meta's Muse Spark finds every item and boxes it. SAM 3.1 "
                                 "then cuts all of them out in a single request, with pixel-accurate masks, so every "
                                 "listing photo is the item's real camera pixels. Nano Banana only paints the studio "
                                 "backdrop. Gemini 4 Argon inspects the condition and calls out flaws, while Muse "
                                 "searches the web for live price comps. Everything, listed and priced, in about a minute."},
        {"id": "3_trust", "text": s3},
        {"id": "4_brain", "text": "And Capy learns from it. Every correction, from a seller or from SubQ, becomes a "
                                  "candidate lesson. Capy's Brain tests it on held-out examples it has never seen, and "
                                  "keeps it only if it fixes the mistake with no regressions. Like this seller who "
                                  "insists every plush toy is a thirty dollar collectible: it hurts the held-out cases, "
                                  "so it's rolled back. Every decision is a git commit."},
        {"id": "5_agent", "text": "Buyers never need our app. Capy Kondo is a Fetch.ai agent on Agentverse, so anyone "
                                  "can shop right from ASI:One. Ask in plain words, and SubQ scores the whole shelf in "
                                  "one long-context call. Scammers get politely blocked. Haggle, and SubQ picks a "
                                  "strategy while code enforces the seller's floor price. Pay through the Agent Payment "
                                  "Protocol, get a pickup code, and the item flips to sold, right on the seller's screen."},
        {"id": "6_closing", "text": "Real pixels, verified claims, and a system that gets better with every "
                                    "correction. Capy Kondo. Make room for what matters."},
    ]


def sh(cmd: list[str]) -> str:
    return subprocess.run(cmd, check=True, capture_output=True, text=True).stdout


def duration(p: Path) -> float:
    return float(sh(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(p)]).strip())


def tts(text: str, out: Path) -> Path:
    if out.exists() and out.with_suffix(".txt").exists() and out.with_suffix(".txt").read_text() == text:
        return out
    key = os.environ["ELEVENLABS_API_KEY"]
    body = {"text": text, "model_id": TTS_MODEL,
            "voice_settings": {"stability": 0.45, "similarity_boost": 0.8, "style": 0.35, "use_speaker_boost": True}}
    r = httpx.post(f"https://api.elevenlabs.io/v1/text-to-speech/{VOICE}?output_format=mp3_44100_128",
                   headers={"xi-api-key": key}, json=body, timeout=180)
    if r.status_code != 200 and TTS_MODEL != "eleven_multilingual_v2":
        body["model_id"] = "eleven_multilingual_v2"
        r = httpx.post(f"https://api.elevenlabs.io/v1/text-to-speech/{VOICE}?output_format=mp3_44100_128",
                       headers={"xi-api-key": key}, json=body, timeout=180)
    r.raise_for_status()
    out.write_bytes(r.content)
    out.with_suffix(".txt").write_text(text)
    return out


def fit_scene(src: Path, target: float, out: Path, head_trim: float = 0.0) -> Path:
    """Scale/pad a recorded scene to exactly `target` seconds at 1920x1080."""
    v = duration(src) - head_trim
    speed = max(1.0, v / target)
    vf = (f"trim=start={head_trim},setpts=(PTS-STARTPTS)/{speed:.4f},"
          f"scale={W}:{H}:flags=lanczos,fps={FPS},tpad=stop_mode=clone:stop_duration={max(0.0, target):.2f},"
          f"trim=duration={target:.3f},format=yuv420p")
    sh(["ffmpeg", "-y", "-loglevel", "error", "-i", str(src), "-vf", vf, "-an", "-c:v", "libx264", "-preset", "medium",
        "-crf", "18", str(out)])
    return out


def asi1_scene(chat: Path, dash: Path, target: float, out: Path) -> Path:
    """Real ASI:One screen recording as the main picture; the seller dashboard as picture-in-picture."""
    vc, vd = duration(chat), duration(dash)
    sc, sd = max(1.0, vc / target), max(1.0, vd / target)
    fc = (f"color=c=0xfbf6ee:s={W}x{H}:r={FPS}:d={target:.2f}[bg];"
          f"[0:v]setpts=(PTS-STARTPTS)/{sc:.4f},scale=w={W - 80}:h={H - 80}:force_original_aspect_ratio=decrease,"
          f"tpad=stop_mode=clone:stop_duration={target:.2f}[c];"
          f"[1:v]setpts=(PTS-STARTPTS)/{sd:.4f},scale=640:-2,pad=iw+8:ih+8:4:4:color=0xf39a1e,"
          f"tpad=stop_mode=clone:stop_duration={target:.2f}[d];"
          f"[bg][c]overlay=(W-w)/2:(H-h)/2[b1];[b1][d]overlay=W-w-36:H-h-36,fps={FPS},"
          f"trim=duration={target:.3f},format=yuv420p[v]")
    sh(["ffmpeg", "-y", "-loglevel", "error", "-i", str(chat), "-i", str(dash), "-filter_complex", fc, "-map", "[v]",
        "-an", "-c:v", "libx264", "-preset", "medium", "-crf", "18", str(out)])
    return out


def srt_time(t: float) -> str:
    h, m = int(t // 3600), int(t % 3600 // 60)
    s = t - h * 3600 - m * 60
    return f"{h:02d}:{m:02d}:{int(s):02d},{int((s - int(s)) * 1000):03d}"


def captions(scenes: list[dict], out: Path) -> Path:
    lines, n = [], 1
    for sc in scenes:
        parts = [p.strip() for p in re.split(r"(?<=[.?!])\s+", sc["text"]) if p.strip()]
        chunks = []
        for p in parts:  # keep captions short
            words = p.split()
            while len(words) > 14:
                chunks.append(" ".join(words[:12]))
                words = words[12:]
            chunks.append(" ".join(words))
        total = sum(len(c) for c in chunks)
        t = sc["start"] + 0.15
        for c in chunks:
            dur = sc["voice"] * len(c) / total
            lines.append(f"{n}\n{srt_time(t)} --> {srt_time(t + dur - 0.05)}\n{c}\n")
            n += 1
            t += dur
    out.write_text("\n".join(lines))
    return out


def main() -> None:
    BUILD.mkdir(parents=True, exist_ok=True)
    (BUILD / "voice").mkdir(exist_ok=True)
    (BUILD / "fit").mkdir(exist_ok=True)
    scenes = narration()
    t = 0.0
    for sc in scenes:
        mp3 = tts(sc["text"], BUILD / "voice" / f"{sc['id']}.mp3")
        sc["voice"] = duration(mp3)
        sc["dur"] = sc["voice"] + (1.1 if sc["id"] not in ("6_closing",) else 2.2)
        sc["start"] = t
        t += sc["dur"]
        print(f"{sc['id']}: voice {sc['voice']:.1f}s scene {sc['dur']:.1f}s")
    total = t
    print(f"total {total:.1f}s")

    fitted = []
    for sc in scenes:
        src = RAW / f"{sc['id']}.webm"
        head = 0.0
        meta = RAW / f"{sc['id']}.json"
        if sc["id"] == "2_scan" and meta.exists():
            head = max(0.0, json.loads(meta.read_text())["marks"].get("click", 0) - 0.6)
        if sc["id"] == "4_brain" and meta.exists():
            head = max(0.0, json.loads(meta.read_text())["marks"].get("correct", 0) - 2.5)
        asi1 = next((ROOT / "demo" / "assets" / f"asi1_chat{ext}" for ext in (".mp4", ".mov", ".webm")
                     if (ROOT / "demo" / "assets" / f"asi1_chat{ext}").exists()), None)
        if sc["id"] == "5_agent" and asi1 is not None:
            fitted.append(asi1_scene(asi1, src, sc["dur"], BUILD / "fit" / "5_agent.mp4"))
            continue
        fitted.append(fit_scene(src, sc["dur"], BUILD / "fit" / f"{sc['id']}.mp4", head_trim=head))

    concat = BUILD / "concat.txt"
    concat.write_text("".join(f"file '{p}'\n" for p in fitted))
    video = BUILD / "video_only.mp4"
    sh(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(concat), "-c", "copy", str(video)])

    # narration track: each scene's voice placed at its start time
    inputs, filters = [], []
    for i, sc in enumerate(scenes):
        inputs += ["-i", str(BUILD / "voice" / f"{sc['id']}.mp3")]
        filters.append(f"[{i}:a]adelay={int(sc['start'] * 1000 + 150)}|{int(sc['start'] * 1000 + 150)}[v{i}]")
    mix = "".join(f"[v{i}]" for i in range(len(scenes)))
    filters.append(f"{mix}amix=inputs={len(scenes)}:normalize=0,apad=whole_dur={total:.2f}[voice]")
    voice = BUILD / "voice_track.wav"
    sh(["ffmpeg", "-y", "-loglevel", "error", *inputs, "-filter_complex", ";".join(filters), "-map", "[voice]",
        "-t", f"{total:.2f}", str(voice)])

    music = ROOT / "demo" / "assets" / "music.mp3"
    srt = captions(scenes, BUILD / "captions.srt")
    out = BUILD / "capykondo_demo.mp4"
    style = ("FontName=DejaVu Sans,Fontsize=17,PrimaryColour=&H00FFFFFF,BackColour=&H99000000,BorderStyle=3,"
             "Outline=6,Shadow=0,MarginV=34,Alignment=2")
    if music.exists():
        afilter = (f"[2:a]volume=0.13,afade=t=in:d=1.5,afade=t=out:st={total - 3:.2f}:d=3[m];"
                   f"[1:a][m]amix=inputs=2:normalize=0:duration=first,alimiter=limit=0.95[a]")
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(video), "-i", str(voice), "-i", str(music),
               "-filter_complex", afilter, "-map", "0:v", "-map", "[a]"]
    else:
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(video), "-i", str(voice), "-map", "0:v", "-map", "1:a"]
    cmd += ["-vf", f"subtitles={srt}:force_style='{style}'", "-c:v", "libx264", "-preset", "medium", "-crf", "20",
            "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", "-t", f"{total:.2f}", str(out)]
    sh(cmd)
    print("wrote", out, f"{duration(out):.1f}s")


if __name__ == "__main__":
    main()
