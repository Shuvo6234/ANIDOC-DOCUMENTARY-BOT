import os, re, sys, wave, time, datetime, subprocess
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
VOICE = os.getenv("VOICE", "Charon")  # deep male voice
TOPIC = os.environ["TOPIC"]

KEYS = [k.strip() for k in os.environ["GEMINI_API_KEYS"].split(",") if k.strip()]
if not KEYS:
    sys.exit("GEMINI_API_KEYS খালি")

RETRY_WORDS = ("429", "quota", "resource_exhausted", "rate", "503",
               "unavailable", "overloaded", "deadline")
MODEL_GONE = ("404", "not_found", "no longer available", "not found")

class Rotator:
    def __init__(self, keys):
        self.keys, self.i = keys, 0

    def call(self, fn):
        last = None
        for _ in range(len(self.keys)):
            client = genai.Client(api_key=self.keys[self.i])
            try:
                return fn(client)
            except Exception as e:
                msg = str(e).lower()
                if any(w in msg for w in MODEL_GONE):
                    raise  # model সমস্যা, key বদলে লাভ নেই
                if any(w in msg for w in RETRY_WORDS):
                    print(f"Key #{self.i + 1} fail ({type(e).__name__}), পরের key...")
                    last = e
                    self.i = (self.i + 1) % len(self.keys)
                    time.sleep(2)
                    continue
                raise
        raise RuntimeError("সব Gemini key ব্যর্থ হয়েছে") from last

rot = Rotator(KEYS)

def with_models(models, fn):
    """প্রথম যে model কাজ করে সেটা সামনে এনে রাখে।"""
    last = None
    for m in list(models):
        try:
            result = rot.call(lambda c: fn(c, m))
            models.remove(m); models.insert(0, m)
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

def make_script(client, model):
    prompt = (
        "সংযুক্ত PDF-এ ANIDOC-এর নির্দেশনা আছে। সেটা হুবহু অনুসরণ করে "
        f"এই বিষয়ে পূর্ণ Hindi documentary script লেখো: {TOPIC}\n"
        "Script অবশ্যই দেবনাগরী হিন্দিতে হবে।"
    )
    r = client.models.generate_content(
        model=model,
        contents=[types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf"), prompt],
    )
    return r.text

script = with_models(TEXT_MODELS, make_script)
print("Script তৈরি:", len(script), "chars")

# ---------- 2. শুধু voiceover-এর text ----------
def make_narration(client, model):
    prompt = (
        "নিচের script থেকে শুধু narrator-এর বলার লাইনগুলো বের করো, দেবনাগরী হিন্দিতে। "
        "Scene heading, timestamp, [brackets], visual/music notes, markdown চিহ্ন — সব বাদ। "
        "শুধু পরিষ্কার narration text দাও।\n\n" + script
    )
    return client.models.generate_content(model=model, contents=prompt).text

narration = with_models(TEXT_MODELS, make_narration).strip()

# ---------- 3. Voiceover ----------
def chunks(text, limit=1200):
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
        contents="Read in Hindi with a deep, calm, professional male "
                 "documentary narrator voice, slow and dramatic pacing:\n" + text,
        config=types.GenerateContentConfig(
            response_modalities=["AUDIO"],
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=VOICE)
                )
            ),
        ),
    )
    return r.candidates[0].content.parts[0].inline_data.data

pcm = b""
for n, part in enumerate(chunks(narration), 1):
    print("Voice chunk", n)
    pcm += with_models(TTS_MODELS, lambda c, m, p=part: tts(c, m, p))

stamp = datetime.datetime.utcnow().strftime("%Y%m%d_%H%M%S")
base = re.sub(r"[^\w]+", "_", TOPIC)[:40] or "anidoc"
script_file = f"{base}_{stamp}_script.txt"
wav_file = f"{base}_{stamp}_voice.wav"
mp3_file = f"{base}_{stamp}_voice.mp3"

open(script_file, "w", encoding="utf-8").write(script)
with wave.open(wav_file, "wb") as w:
    w.setnchannels(1); w.setsampwidth(2); w.setframerate(24000)
    w.writeframes(pcm)
subprocess.run(["ffmpeg", "-y", "-i", wav_file, "-b:a", "192k", mp3_file], check=True)

# ---------- 4. Google Drive upload ----------
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

for f, mime in [(script_file, "text/plain"), (mp3_file, "audio/mpeg")]:
    drive.files().create(
        body={"name": f, "parents": [os.environ["GDRIVE_FOLDER_ID"]]},
        media_body=MediaFileUpload(f, mimetype=mime, resumable=True),
        fields="id",
    ).execute()
    print("Uploaded:", f)
