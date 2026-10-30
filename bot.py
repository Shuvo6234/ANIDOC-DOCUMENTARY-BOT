import os, re, io, sys, wave, time, datetime, subprocess, audioop, statistics
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
    "gemini-3.1-flash-tts-preview,gemini-2.5-pro-preview-tts",
).split(",")
VOICE = os.getenv("VOICE", "Charon")  # Charon / Rasalgethi = Informative, Algenib = Gravelly
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

# ---------- 0. Drive আগে check ----------
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
            models.insert(0, m)
            return result
        except Exception as e:
            if any(w in str(e).lower() for w in MODEL_GONE):
                print(f"Model {m} পাওয়া যায়নি, পরেরটা...")
                last = e
                continue
            raise
    raise RuntimeError("কোনো model কাজ করেনি") from last


def strip_tags(t):
    """<...> audio tag মুছে ফেলে, বাড়তি space ঠিক করে।"""
    t = re.sub(r"<[^<>\n]{1,40}>", "", t)
    t = re.sub(r"[ \t]{2,}", " ", t)
    return t


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

NO_TAG_RULE = (
    "Script-এ কোনো audio tag, sound/music নির্দেশ বা < > দিয়ে লেখা কিছু রাখবে না। "
    "বিরতি বোঝাতে শুধু দাঁড়ি (।), কমা আর ... ব্যবহার করো।"
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
        f"এই বিষয়ে পূর্ণ documentary script লেখো: {TOPIC}\n\n" + LANG_RULE + "\n\n" + NO_TAG_RULE
    )
    return gen_text(client, model, [
        types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf"), prompt])


script = strip_tags(with_models(TEXT_MODELS, make_script))
print("Script তৈরি:", len(script), "chars")


# ---------- 2. শুধু narration ----------
def make_narration(client, model):
    prompt = (
        "নিচের script থেকে শুধু narrator-এর বলার লাইনগুলো বের করো। "
        "Scene heading, timestamp, [ ] বা < > দিয়ে লেখা নির্দেশ, visual/music notes, "
        "markdown চিহ্ন — সব বাদ। "
        "হিন্দি শব্দ দেবনাগরী লিপিতেই থাকবে, English শব্দ English অক্ষরেই থাকবে। "
        "প্রতিটা বাক্য শেষে দাঁড়ি (।) বা full stop দাও। "
        "শুধু পরিষ্কার narration text দাও, আর কিছু না।\n\n" + script
    )
    return gen_text(client, model, prompt)


narration = with_models(TEXT_MODELS, make_narration).strip()
narration = re.sub(r"[*#_`]+", "", narration)
narration = re.sub(r"\[[^\]\n]{1,60}\]", "", narration)  # [ ] নির্দেশ
narration = strip_tags(narration).strip()
print("Narration:", len(narration), "chars")

# ---------- 3. Voiceover ----------
# ছোট, স্থির style — প্রতিটা call-এ হুবহু একই
STYLE = ("calm, deep and authoritative documentary narrator, "
         "speaking slowly at a steady, measured pace with a serious cinematic tone")
LEGACY_PREFIX = (
    "Read the following text aloud as a deep, calm, authoritative documentary narrator, "
    "slow and steady pace, serious tone. Do not read these instructions:\n"
)


def to_pcm(data):
    """3.8 model WAV (header-সহ) দেয়, পুরনো model raw PCM দেয় — দুটোই সামলাই।"""
    if data[:4] == b"RIFF":
        with wave.open(io.BytesIO(data), "rb") as w:
            return w.readframes(w.getnframes())
    return data


def tts(client, model, text):
    if model.startswith("gemini-3.8"):
        try:
            r = client.models.generate_content(
                model=model,
                contents=[{"role": "user", "parts": [
                    {"text": text, "speech_metadata": {"style": STYLE}}]}],
                config={
                    "response_modalities": ["AUDIO"],
                    "speech_config": {"voice_config": {"voice": VOICE}},
                },
            )
            return to_pcm(r.candidates[0].content.parts[0].inline_data.data)
        except Exception as e:
            if any(w in str(e).lower() for w in
                   ("validation", "extra_forbidden", "speech_metadata", "voice_config", "unexpected")):
                print("SDK নতুন schema বোঝেনি, legacy format-এ চেষ্টা...")
            else:
                raise
    prefix = "" if model.startswith("gemini-3.8") else LEGACY_PREFIX
    r = client.models.generate_content(
        model=model,
        contents=prefix + text,
        config=types.GenerateContentConfig(
            response_modalities=["AUDIO"],
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=VOICE)
                )
            ),
        ),
    )
    return to_pcm(r.candidates[0].content.parts[0].inline_data.data)


def chunks(text, limit=1000):
    parts, cur = [], ""
    for s in re.split(r"(?<=[।\.\?\!])\s+", text):
        if len(cur) + len(s) > limit and cur:
            parts.append(cur.strip())
            cur = ""
        cur += s + " "
    if cur.strip():
        parts.append(cur.strip())
    return parts


def normalize(pcm, target=3500):
    rms = audioop.rms(pcm, 2)
    if rms == 0:
        return pcm
    return audioop.mul(pcm, 2, min(target / rms, 4.0))


def active_seconds(pcm, thr=300):
    fl = 480 * 2  # 20ms @ 24kHz, 16-bit
    n = 0
    for i in range(0, len(pcm) - fl + 1, fl):
        if audioop.rms(pcm[i:i + fl], 2) > thr:
            n += 1
    return max(n * 0.02, 0.1)


def speech_chars(text):
    return len(re.sub(r"\s+", "", text))


def change_tempo(pcm, factor):
    open("t_in.raw", "wb").write(pcm)
    subprocess.run(
        ["ffmpeg", "-y", "-f", "s16le", "-ar", "24000", "-ac", "1", "-i", "t_in.raw",
         "-filter:a", f"atempo={factor:.4f}", "-f", "s16le", "-ar", "24000", "-ac", "1",
         "t_out.raw"],
        check=True, capture_output=True,
    )
    return open("t_out.raw", "rb").read()


parts = chunks(narration)
print("মোট voice chunk:", len(parts))
raw = []
for n, part in enumerate(parts, 1):
    print("Voice chunk", n, "/", len(parts))
    pcm = with_models(TTS_MODELS, lambda c, m, p=part: tts(c, m, p))
    raw.append(normalize(pcm))

# প্রতিটা টুকরোর কথা বলার গতি মেপে মাঝামাঝি গতিতে মেলানো
rates = [speech_chars(p) / active_seconds(a) for p, a in zip(parts, raw)]
target = statistics.median(rates)
print("গতি (অক্ষর/সেকেন্ড):", [round(r, 1) for r in rates], "target:", round(target, 1))

SILENCE = b"\x00\x00" * int(24000 * 0.4)
final = b""
for r, a in zip(rates, raw):
    factor = max(0.8, min(1.25, target / r))
    if abs(factor - 1) > 0.03:
        a = change_tempo(a, factor)
    final += a + SILENCE

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
    w.writeframes(final)
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
