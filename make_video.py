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
    d = ImageDraw.Draw(ov)
    f = font(size)
    lines = textwrap.wrap(text, width=width)[:4]
    lh = int(size * 1.3)
    box = lh * len(lines) + 80
    top = (H - box) // 2
    d.rectangle([(120, top), (W - 120, top + box)], fill=(0, 0, 0, 150))
    y = top + 40
    for ln in lines:
        d.text(((W - d.textlength(ln, font=f)) / 2, y), ln, font=f, fill="white")
        y += lh
    Image.alpha_composite(base.convert("RGBA"), ov).convert("RGB").save(out)
    return out


# ---------- 4. assemble ----------
def make_segment(png, wav, dur, out):
    frames = int((dur + 0.5) * 30) + 1
    vf = (f"scale=2400:-1,zoompan=z='min(zoom+0.0006,1.12)':x='iw/2-(iw/zoom/2)':"
          f"y='ih/2-(ih/zoom/2)':d={frames}:s={W}x{H}:fps=30,format=yuv420p")
    run(["ffmpeg", "-y", "-i", str(png), "-i", str(wav), "-vf", vf,
         "-af", "apad=pad_dur=0.5", "-t", f"{dur + 0.5:.2f}",
         "-c:v", "libx264", "-preset", "medium", "-crf", "20",
         "-c:a", "aac", "-b:a", "160k", "-ar", "44100", "-ac", "2", str(out)])


def concat(segs, out):
    lst = Path("work") / "list.txt"
    lst.write_text("".join(f"file '{Path(s).resolve()}'\n" for s in segs))
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
         "-c", "copy", "-movflags", "+faststart", str(out)])


def main():
    topic = os.environ["TOPIC"]
    OUT.mkdir(exist_ok=True)
    work = Path("work"); work.mkdir(exist_ok=True)
    print("1/4 Writing script")
    data = write_script(topic)
    (OUT / "script.json").write_text(json.dumps(data, indent=2))
    voice = load_piper()
    segs = []
    for i, sc in enumerate(data["scenes"]):
        print(f"2/4 Scene {i + 1}/{len(data['scenes'])}")
        wav = work / f"s{i}.wav"
        speak(sc["narration"], wav, voice)
        bg = fetch_image(sc.get("image_query", topic), work / f"s{i}.jpg")
        png = render_frame(bg, sc["on_screen_text"], work / f"s{i}.png")
        seg = work / f"s{i}.mp4"
        make_segment(png, wav, wav_seconds(wav), seg)
        segs.append(seg)
    print("3/4 Joining scenes")
    concat(segs, OUT / "video.mp4")
    first_bg = work / "s0.jpg"
    render_frame(first_bg if first_bg.exists() else None, data["title"],
                 OUT / "thumbnail.png", size=110, width=20)
    (OUT / "meta.txt").write_text(
        f"TITLE:\n{data['title']}\n\nDESCRIPTION:\n{data['description']}\n\nTAGS:\n{', '.join(data.get('tags', []))}\n")
    print("4/4 Done")


if __name__ == "__main__":
    main()
