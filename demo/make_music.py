"""Background music for the demo video, generated with Google's Lyria 3.5."""
import base64, os, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from capykondo import llm

OUT = Path(__file__).resolve().parent / "assets" / "music.mp3"
PROMPT = ("A 2 minute 40 second instrumental lo-fi chillhop track. Warm, cozy and playful: soft Rhodes keys, mellow "
          "boom-bap drums at 84 BPM, round upright bass, gentle vinyl crackle, and a light, happy marimba melody. "
          "No vocals at all. Calm and content, like a capybara relaxing in a hot spring with a little orange on its "
          "head. Consistent energy throughout so it can sit quietly under a voiceover, with a soft ending.")
t = time.time()
it = llm.gemini().interactions.create(model=os.environ.get("LYRIA_MODEL", "lyria-3-pro-preview"), input=PROMPT, response_format={"type": "audio"})
OUT.write_bytes(base64.b64decode(it.output_audio.data))
print("music", OUT, round(time.time() - t, 1), "s", OUT.stat().st_size, "bytes")
