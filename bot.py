import os, re, sys, wave, time, datetime, subprocess, audioop
from google import genai
from google.genai import types
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

PDF_PATH = "ANIDOC MAGICAL PROMPT v4.pdf"
TEXT_MODELS = os.getenv(
    "TEXT_MODELS", "gemini-3.8-flash,gemini-3.7-flash,gemini-2.5-flash"
).split(",")
TTS_MODELS = os.getenv(
    "TTS_MODELS",
    "gemini-3.8-flash-tts,gemini-3.8-flash-lite-tts,"
    "gemini-3.1-flash-tts-preview,gemini-2.5-flash-preview-tts",
).split(",")
VOICE = os.getenv("VOICE", "Charon")  # deep male voice (Orus / Algenib-ও try করতে পারো)
TOPIC = os.environ["TOPIC"]

KEYS = [k.strip() for k in os.environ["GEMINI_API_KEYS"].split(",") if k.strip()]
if not KEYS:
    sys.exit("GEMINI_API_KEYS খালি")

RETRY_WORDS = ("429", "quota", "resource_exhausted", "rate", "503",
               "unavailable", "overloaded", "deadline", "disconnected",
               "timeout", "timed out", "remoteprotocol", "readerror",
               "connecterror", "500", "internal")
MODEL_GONE = ("404", "not_found", "no longer available", "not found")
KEY_DEAD = ("permission_denied", "api key not valid", "api_key_invalid")

# ---------- 0. Drive আগে check (অনেকক্ষণ কাজ করার পর fail এড়াতে) ----------
creds = Credentials(
    None,
    refresh_token=os.environ["GDRIVE_REFRESH_TOKEN"],
    client_id=os.environ["GDRIVE_CLIENT_ID"],
    client_secret=os.environ["GDRIVE_CLIENT_SECRET"],
    token_uri="https://oauth2.googleapis.com/token",
    scopes=["https://www.googleapis.com/auth/drive"],
)
creds.refresh(Request())
drive = build("drive", "v3", credentials=creds)
print("Drive login OK")


class Rotator:
    def __init__(self, keys):
        self.keys, self.i = keys, 0

    def call(self, fn):
        last = None
        for _ in range(len(self.keys) + 1):
            if not self.keys:
                break
            self.i %= len(self.keys)
            client = genai.Client(
                api_key=self.keys[self.i],
                http_options=types.HttpOptions(timeout=300000),
            )
            try:
                return fn(client)
            except Exception as e:
                msg = str(e).lower()
                print(f"Key #{self.i + 1} error: {type(e).__name__}: {str(e)[:250]}")
                if any(w in msg for w in MODEL_GONE):
                    raise
                if any(w in msg for w in KEY_DEAD):
                    print("এই key বাদ দেওয়া হলো (permission/billing সমস্যা)")
                    self.keys.pop(self.i)
                    last = e
                    continue
                if any(w in msg for w in RETRY_WORDS):
                    last = e
                    self.i += 1
                    time.sleep(2)
                    continue
                raise
        raise RuntimeError("সব Gemini key ব্যর্থ হয়েছে") from last


rot = Rotator(KEYS)


def with_models(models, fn):
    last = None
    for m in list(models):
        try:
            result = rot.call(lambda c: fn(c, m))
            models.remove(m)
            models.insert(0, m)  # যেটা কাজ করল সেটাই পরে ব্যবহার হবে
            return result
        except Exception as e:
            if any(w in str(e).lower() for w in MODEL_GONE):
                print(f"Model {m} পাওয়া যায়নি, পরেরটা...")
                last = e
                continue
            raise
    raise RuntimeError("কোনো model কাজ করেনি") from last


# ---------- 1. Script ----------
pdf_bytes = open(PDF_PATH, "rb").read()

LANG_RULE = (
    "ভাষার নিয়ম (PDF-এ ভাষা নিয়ে যা-ই লেখা থাকুক, এই নিয়ম সবার ওপরে): "
    "Script হবে হিন্দি আর English মিশিয়ে। সব হিন্দি শব্দ অবশ্যই দেবনাগরী লিপিতে "
    "(যেমন: यह कहानी है एक रहस्यमयी जगह की) লিখবে। হিন্দি কখনো রোমান/English অক্ষরে "
    "(Latin) লিখবে না। শুধু English শব্দগুলো (যেমন black hole, universe, mission, "
    "discovery, history, technology) English অক্ষরে লিখবে, দেবনাগরীতে বদলাবে না। "
    "সহজ, রোজকার হিন্দি ব্যবহার করো, আর আধুনিক Indian documentary-র মতো "
    "প্রয়োজন মতো English শব্দ মেশাও।"
)


def gen_text(client, model, contents):
    out = []
    for ch in client.models.generate_content_stream(model=model, contents=contents):
        if ch.text:
            out.append(ch.text)
    return "".join(out)


def make_script(client, model):
    prompt = (
        "সংযুক্ত PDF-এ ANIDOC-এর নির্দেশনা আছে। সেটা অনুসরণ করে "
        f"এই বিষয়ে পূর্ণ documentary script লেখো: {TOPIC}\n\n" + LANG_RULE
    )
    return gen_text(client, model, [
        types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf"), prompt])


script = with_models(TEXT_MODELS, make_script)
print("Script তৈরি:", len(script), "chars")


# ---------- 2. শুধু narration ----------
def make_narration(client, model):
    prompt = (
        "নিচের script থেকে শুধু narrator-এর বলার লাইনগুলো বের করো। "
        "Scene heading, timestamp, [brackets], visual/music notes, markdown চিহ্ন (*, #, -) — সব বাদ। "
        "হিন্দি শব্দ দেবনাগরী লিপিতেই থাকবে, কখনো রোমান অক্ষরে নয়। English শব্দ English "
        "অক্ষরেই থাকবে, দেবনাগরীতে বদলাবে না। প্রতিটা বাক্য শেষে দাঁড়ি (।) বা full stop দাও। "
        "শুধু পরিষ্কার narration text দাও, আর কিছু না।\n\n" + script
    )
    return gen_text(client, model, prompt)


narration = with_models(TEXT_MODELS, make_narration).strip()
narration = re.sub(r"[*#_`>]+", "", narration)


# ---------- 3. Voiceover ----------
STYLE = (
    "You are the narrator of a premium Hindi documentary, in the style of "
    "Discovery or National Geographic India. Voice: a deep, warm, authoritative adult male. "
    "Pitch: low and steady. Pace: slow to medium, about 130 words per minute. "
    "Tone: serious, calm and cinematic, with gentle emphasis on key words and natural pauses "
    "at commas and full stops. Keep exactly the same voice, pitch, energy, loudness and pace "
    "from start to finish. Never become cheerful, rushed, whispery or theatrical. "
    "The text is Hindi in Devanagari script with some English words in Latin script. "
    "Pronounce Hindi words in clear standard Hindi and English words in a natural Indian accent. "
    "Read only the text after the colon, do not read these instructions.\nText: "
)


def chunks(text, limit=2200):
    parts, cur = [], ""
    for s in re.split(r"(?<=[।\.\?\!])\s+", text):
        if len(cur) + len(s) > limit and cur:
            parts.append(cur.strip())
            cur = ""
        cur += s + " "
    if cur.strip():
        parts.append(cur.strip())
    return parts


def tts(client, model, text):
    r = client.models.generate_content(
        model=model,
        contents=STYLE + text,
        config=types.GenerateContentConfig(
            temperature=0.2,
            response_modalities=["AUDIO"],
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=VOICE)
                )
            ),
        ),
    )
    return r.candidates[0].content.parts[0].inline_data.data


def normalize(pcm, target=3500):
    rms = audioop.rms(pcm, 2)
    if rms == 0:
        return pcm
    return audioop.mul(pcm, 2, min(target / rms, 4.0))


SILENCE = b"\x00\x00" * int(24000 * 0.5)  # ০.৫ সেকেন্ড বিরতি

pcm = b""
parts = chunks(narration)
print("মোট voice chunk:", len(parts))
for n, part in enumerate(parts, 1):
    print("Voice chunk", n, "/", len(parts))
    pcm += normalize(with_models(TTS_MODELS, lambda c, m, p=part: tts(c, m, p))) + SILENCE

stamp = datetime.datetime.utcnow().strftime("%Y%m%d_%H%M%S")
base = re.sub(r"[^\w]+", "_", TOPIC)[:40] or "anidoc"
script_file = f"{base}_{stamp}_script.txt"
wav_file = f"{base}_{stamp}_voice.wav"
mp3_file = f"{base}_{stamp}_voice.mp3"

open(script_file, "w", encoding="utf-8").write(script)
with wave.open(wav_file, "wb") as w:
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(24000)
    w.writeframes(pcm)
subprocess.run(
    ["ffmpeg", "-y", "-i", wav_file, "-af", "loudnorm=I=-16:TP=-1.5:LRA=7",
     "-b:a", "192k", mp3_file],
    check=True,
)

# ---------- 4. Drive upload ----------
for f, mime in [(script_file, "text/plain"), (mp3_file, "audio/mpeg")]:
    drive.files().create(
        body={"name": f, "parents": [os.environ["GDRIVE_FOLDER_ID"]]},
        media_body=MediaFileUpload(f, mimetype=mime, resumable=True),
        fields="id",
    ).execute()
    print("Uploaded:", f)
