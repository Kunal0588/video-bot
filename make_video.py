"""Stage 1: topic -> script (Gemini) -> voice (Piper/espeak) -> video (ffmpeg)."""
import json, os, re, subprocess, textwrap, time, wave
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageEnhance, ImageFont

W, H = 1920, 1080
OUT = Path("output")
MINUTES = int(os.getenv("VIDEO_MINUTES", "4"))
MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
VOICE = "en_US-lessac-medium"
VOICE_URL = "https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/lessac/medium/"
FONTS = ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
         "/System/Library/Fonts/Supplemental/Arial Bold.ttf"]


def run(cmd):
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


# ---------- 1. script ----------
def write_script(topic):
    prompt = f"""Write a faceless educational YouTube video about: "{topic}".
About {MINUTES} minutes of narration (~{MINUTES * 150} words), {MINUTES * 3} scenes.
Be accurate and genuinely useful. Open with a hook, teach in a logical order, end with a short recap.
Do not invent statistics, quotes or sources; stay general if unsure.
Return ONLY JSON: {{"title": "<under 70 chars, specific>", "description": "<2-3 sentences>",
"tags": ["..."], "scenes": [{{"narration": "<2-4 spoken sentences>",
"on_screen_text": "<max 12 words>", "image_query": "<2-4 word stock photo search>"}}]}}"""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"
    for attempt in range(4):
        r = requests.post(url, headers={"x-goog-api-key": os.environ["GEMINI_API_KEY"]},
                          json={"contents": [{"parts": [{"text": prompt}]}],
                                "generationConfig": {"responseMimeType": "application/json"}},
                          timeout=180)
        if r.status_code in (429, 500, 503):
            print(f"Gemini busy ({r.status_code}), retrying...")
            time.sleep(20 * (attempt + 1))
            continue
        r.raise_for_status()
        break
    text = r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
    data = json.loads(re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip())
    assert data.get("scenes"), "no scenes returned"
    return data


# ---------- 2. voice ----------
def load_piper():
    try:
        from piper import PiperVoice
        for ext in (".onnx", ".onnx.json"):
            p = Path(VOICE + ext)
            if not p.exists():
                resp = requests.get(VOICE_URL + VOICE + ext, timeout=300)
                resp.raise_for_status()
                p.write_bytes(resp.content)
        return PiperVoice.load(VOICE + ".onnx")
    except Exception as e:
        print("Piper unavailable, using espeak-ng instead:", e)
        return None


def speak(text, path, voice):
    if voice:
        try:
            with wave.open(str(path), "wb") as wf:
                voice.synthesize(text, wf)
            return
        except Exception as e:
            print("Piper failed on a scene, using espeak-ng:", e)
    run(["espeak-ng", "-s", "150", "-w", str(path), text])


def wav_seconds(path):
    with wave.open(str(path)) as w:
        return w.getnframes() / w.getframerate()


# ---------- 3. visuals ----------
def font(size):
    for f in FONTS:
        if os.path.exists(f):
            return ImageFont.truetype(f, size)
    return ImageFont.load_default(size)


def fetch_image(query, dest):
    """Try Pexels, then Pixabay; return None (plain background) if neither works."""
    try:
        if os.getenv("PEXELS_API_KEY"):
            r = requests.get("https://api.pexels.com/v1/search",
                             headers={"Authorization": os.environ["PEXELS_API_KEY"]},
                             params={"query": query, "orientation": "landscape", "per_page": 3}, timeout=30)
            r.raise_for_status()
            hits = r.json().get("photos", [])
            url = hits[0]["src"]["large2x"] if hits else None
        elif os.getenv("PIXABAY_API_KEY"):
            r = requests.get("https://pixabay.com/api/",
                             params={"key": os.environ["PIXABAY_API_KEY"], "q": query,
                                     "image_type": "photo", "orientation": "horizontal",
                                     "safesearch": "true", "per_page": 3}, timeout=30)
            r.raise_for_status()
            hits = r.json().get("hits", [])
            url = hits[0]["largeImageURL"] if hits else None
        else:
            return None
        if not url:
            return None
        img = requests.get(url, timeout=60)
        img.raise_for_status()
        Path(dest).write_bytes(img.content)
        return dest
    except Exception as e:
        print(f"image fetch failed for '{query}': {e}")
        return None


def render_frame(bg, text, out, size=78, width=30):
    if bg:
        img = Image.open(bg).convert("RGB")
        r = max(W / img.width, H / img.height)
        img = img.resize((int(img.width * r) + 1, int(img.height * r) + 1))
        l, t = (img.width - W) // 2, (img.height - H) // 2
        base = img.crop((l, t, l + W, t + H))
    else:
        base = Image.new("RGB", (W, H), (18, 28, 56))
    base = ImageEnhance.Brightness(base).enhance(0.6)
    ov = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDra
