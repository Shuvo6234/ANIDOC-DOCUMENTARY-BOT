import os, re, io, sys, json, wave, time, hashlib, datetime, subprocess, audioop, statistics, threading, zipfile
from google import genai
from google.genai import types
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload

# log সঙ্গে সঙ্গে দেখানোর জন্য (GitHub Actions-এ নইলে output আটকে থাকে)
sys.stdout.reconfigure(line_buffering=True)
MAX_RUN_MIN = int(os.getenv("MAX_RUN_MIN", "345"))


def _watchdog():
    print(f"!!! {MAX_RUN_MIN} মিনিটে শেষ হয়নি, বন্ধ করা হলো (Gemini key/quota বা network সমস্যা হতে পারে)")
    os._exit(2)


_wd = threading.Timer(MAX_RUN_MIN * 60, _watchdog)
_wd.daemon = True
_wd.start()

MODE = (os.getenv("MODE") or "watch").strip()   # once | watch | prompts | video
TOPIC = (os.getenv("TOPIC") or "").strip()
PROJECT = (os.getenv("PROJECT") or "").strip()
OUT_ID = os.environ["GDRIVE_FOLDER_ID"]

# প্রতি scene ≈ 4-6 সেকেন্ড। ~12 অক্ষর/সেকেন্ড ধরে 60 অক্ষর ≈ 5 সেকেন্ড।
SCENE_CHARS = int(os.getenv("SCENE_CHARS", "60"))
IMAGE_ASPECT = os.getenv("IMAGE_ASPECT", "16:9")

# প্রতি TTS block-এ সর্বোচ্চ কত অক্ষর। ছোট = sync বেশি নিখুঁত (API call বেশি)।
# BLOCK_CHARS=1 দিলে প্রতিটা scene আলাদা TTS হবে (100% exact sync)।
BLOCK_CHARS = int(os.getenv("BLOCK_CHARS", "240"))
# প্রথম কয়েকটা block-এর গতিকে মাপকাঠি ধরে সব scene সেই গতিতে আনা হয়; <1 = আরও ধীর (documentary-সুলভ)
VOICE_SPEED = float(os.getenv("VOICE_SPEED", "0.95"))

# ---- music / mix সেটিং ----
BGM_MOOD = os.getenv("BGM_MOOD", "cinematic")   # cinematic | dark | calm | epic
BGM_VOL = float(os.getenv("BGM_VOL", "0.30"))   # 0.2 (খুব মৃদু) - 0.8 (জোরালো)
LUFS = os.getenv("LUFS", "-14")                 # YouTube-এর জন্য -14
# ---- sound effects ----
SFX_ON = os.getenv("SFX", "1") == "1"
TRANSITION_SEC = float(os.getenv("TRANSITION_SEC", "0.5"))          # scene বদলের crossfade (0 = বন্ধ)
TRANSITION = [x.strip() for x in os.getenv("TRANSITION", "fade").split(",") if x.strip()] or ["fade"]   # fade | fadeblack | dissolve ... (comma দিলে পালা করে)
TRANS_SFX = os.getenv("TRANS_SFX", "1") == "1"                      # scene বদলের সময় হালকা swish sound
TRANS_SFX_VOL = float(os.getenv("TRANS_SFX_VOL", "0.55"))
CLIP_AUDIO_VOL = float(os.getenv("CLIP_AUDIO_VOL", "0.5"))          # Flow/Veo clip-এর নিজের sound (ambient/SFX) narration-এর নিচে মেশানো (0 = বন্ধ)
SFX_VOL = float(os.getenv("SFX_VOL", "0.5"))
# ---- নীরবতা কমানো ----
MAX_PAUSE = float(os.getenv("MAX_PAUSE", "0.18"))
MIN_PAUSE_RUN = float(os.getenv("MIN_PAUSE_RUN", "0.25"))
# ---- watch mode: Drive output folder বারবার check ----
POLL_SEC = int(os.getenv("POLL_SEC", "120"))              # 2 মিনিট পর পর
WATCH_MAX_MIN = int(os.getenv("WATCH_MAX_MIN", "330"))    # এত মিনিট পর watcher নিজে থামে (cron আবার শুরু করে)
QUOTA_WAIT_MIN = int(os.getenv("QUOTA_WAIT_MIN", "60"))   # সব key-এর quota শেষ হলে কতক্ষণ পর আবার চেষ্টা
# ---- dialogue clip ধরা ----
# plan = শুধু যে scene-এ dialogue plan করা আছে সেখানে clip-এর audio দেখে; any = সব video clip-এর audio দেখে
DIALOGUE_DETECT = os.getenv("DIALOGUE_DETECT", "plan")
DIALOGUE_MIN_SEC = float(os.getenv("DIALOGUE_MIN_SEC", "0.8"))
# ---- auto topic ----
# video শেষ (mp4 আছে) হলে bot নিজে Google trending থেকে নতুন topic নিয়ে prompt পাঠায়
AUTO_TOPIC = os.getenv("AUTO_TOPIC", "1") == "1"
TREND_GEO = os.getenv("TREND_GEO", "IN")                  # কোন দেশের trending (IN = ভারত)
ARCHIVE_NAME = os.getenv("ARCHIVE_FOLDER", "Finished_Videos")   # পুরনো mp4 জমা রাখার একমাত্র folder

PDF_PATH = "ANIDOC MAGICAL PROMPT v4.pdf"
TEXT_MODELS = os.getenv(
    "TEXT_MODELS", "gemini-3.1-flash-lite,gemini-3.5-flash-lite"
).split(",")

TTS_MODELS = os.getenv("TTS_MODELS", "gemini-3.8-flash-tts").split(",")
VOICE = os.getenv("VOICE", "Charon")

KEYS = [k.strip() for k in os.environ["GEMINI_API_KEYS"].split(",") if k.strip()]
if not KEYS:
    sys.exit("GEMINI_API_KEYS খালি")
TOTAL = len(KEYS)

RETRY_WORDS = ("429", "quota", "resource_exhausted", "rate", "503",
               "unavailable", "overloaded", "deadline", "disconnected",
               "timeout", "timed out", "remoteprotocol", "readerror",
               "connecterror", "500", "internal")
MODEL_GONE = ("404", "not_found", "no longer available", "not found")
KEY_DEAD = ("permission_denied", "api key not valid", "api_key_invalid")

# ---------- Drive ----------
creds = Credentials(
    None,
    refresh_token=os.environ["GDRIVE_REFRESH_TOKEN"],
    client_id=os.environ["GDRIVE_CLIENT_ID"],
    client_secret=os.environ["GDRIVE_CLIENT_SECRET"],
    token_uri="https://oauth2.googleapis.com/token",
    scopes=["https://www.googleapis.com/auth/drive"],
)
creds.refresh(Request())
drive = build("drive", "v3", credentials=creds, cache_discovery=False)
print("Drive login OK")

FOLDER_MIME = "application/vnd.google-apps.folder"


def d_list(parent, extra=""):
    res, tok = [], None
    while True:
        r = drive.files().list(
            q=f"'{parent}' in parents and trashed=false {extra}",
            fields="nextPageToken, files(id,name,mimeType,createdTime,size)",
            pageSize=200, pageToken=tok, orderBy="createdTime desc",
        ).execute()
        res += r["files"]
        tok = r.get("nextPageToken")
        if not tok:
            return res


def d_mkdir(name, parent):
    return drive.files().create(
        body={"name": name, "mimeType": FOLDER_MIME, "parents": [parent]}, fields="id"
    ).execute()["id"]


def d_upload(path, name, parent, mime):
    media = MediaFileUpload(path, mimetype=mime, resumable=True, chunksize=16 * 1024 * 1024)
    req = drive.files().create(body={"name": name, "parents": [parent]},
                               media_body=media, fields="id")
    resp = None
    while resp is None:
        _, resp = req.next_chunk()
    return resp["id"]


def d_download(fid, path):
    req = drive.files().get_media(fileId=fid)
    with open(path, "wb") as f:
        dl = MediaIoBaseDownload(f, req, chunksize=16 * 1024 * 1024)
        done = False
        while not done:
            _, done = dl.next_chunk()


def d_delete(fid):
    drive.files().delete(fileId=fid).execute()


# ---------- Gemini key scheduler ----------
class AllGeminiKeysExhausted(RuntimeError):
    """সব configured key-এর quota শেষ; retry না করে run বন্ধ করতে হবে।"""


class Rotator:
    """প্রতি key-তে এক সময়ে একটি request; 429 হলে cooldown, invalid হলে disable."""
    def __init__(self, keys):
        self.keys = [
            {"no": i, "key": key, "state": "FREE", "until": 0.0, "last_error": ""}
            for i, key in enumerate(keys, 1)
        ]
        self.cv = threading.Condition()
        self.cursor = 0

    def _acquire(self):
        with self.cv:
            while True:
                now = time.monotonic()
                available = [x for x in self.keys if x["state"] == "FREE" or
                             (x["state"] == "COOLDOWN" and x["until"] <= now)]
                if available:
                    available.sort(key=lambda x: ((x["no"] - 1 - self.cursor) % max(len(self.keys), 1)))
                    item = available[0]
                    item["state"] = "BUSY"
                    self.cursor = item["no"] % max(len(self.keys), 1)
                    return item
                usable = [x for x in self.keys if x["state"] not in ("DISABLED", "EXHAUSTED")]
                if not usable:
                    exhausted = sum(x["state"] == "EXHAUSTED" for x in self.keys)
                    if exhausted:
                        raise AllGeminiKeysExhausted(
                            f"সব {exhausted}/{len(self.keys)}টি Gemini key-এর quota শেষ। "
                            "আর retry করা হবে না; quota reset/billing ঠিক করে আবার চালান।"
                        )
                    raise RuntimeError("সব Gemini API key invalid/disabled")
                cooldowns = [x["until"] for x in usable if x["state"] == "COOLDOWN"]
                if cooldowns:
                    wait = max(0.2, min(30.0, min(cooldowns) - now))
                    print(f"সব Gemini key BUSY/COOLDOWN; {wait:.1f}s অপেক্ষা...")
                    self.cv.wait(timeout=wait)
                else:
                    self.cv.wait(timeout=30)

    def _release(self, item, state="FREE", cooldown=0, error=""):
        with self.cv:
            item["state"] = state
            item["until"] = time.monotonic() + cooldown if state == "COOLDOWN" else 0.0
            item["last_error"] = error[:300]
            self.cv.notify_all()

    @staticmethod
    def _cooldown_seconds(msg):
        m = re.search(r"retry(?:\s+in|after)[^0-9]{0,12}(\d+(?:\.\d+)?)\s*s", msg, re.I)
        if m:
            return max(5.0, min(float(m.group(1)) + 2.0, 900.0))
        if "429" in msg or "quota" in msg or "resource_exhausted" in msg or "rate" in msg:
            return float(os.getenv("GEMINI_QUOTA_COOLDOWN_SEC", "60"))
        return float(os.getenv("GEMINI_TRANSIENT_COOLDOWN_SEC", "15"))

    @staticmethod
    def _is_hard_quota_exhaustion(msg):
        hard_markers = (
            "you exceeded your current quota",
            "quota exceeded",
            "daily quota",
            "per day limit",
            "requests per day",
            "check your plan and billing details",
            "billing details",
        )
        return any(marker in msg for marker in hard_markers)

    def call(self, fn, rounds=int(os.getenv("GEMINI_ROUNDS", "3"))):
        last = None
        max_attempts = max(1, rounds) * max(1, len(self.keys))
        for attempt in range(max_attempts):
            item = self._acquire()
            no, key = item["no"], item["key"]
            print(f"→ Gemini call, key {no}/{len(self.keys)} [BUSY]")
            try:
                client = genai.Client(
                    api_key=key,
                    http_options=types.HttpOptions(timeout=int(os.getenv("GEMINI_TIMEOUT_MS", "120000")))
                )
                result = fn(client)
                self._release(item, "FREE")
                print(f"✓ Gemini key {no} FREE")
                return result
            except Exception as e:
                msg = str(e).lower()
                last = e
                print(f"[key {no}] {type(e).__name__}: {str(e)[:160]}")
                if any(w in msg for w in MODEL_GONE):
                    self._release(item, "FREE", error=msg)
                    raise
                if any(w in msg for w in KEY_DEAD):
                    self._release(item, "DISABLED", error=msg)
                    print(f"Key {no} DISABLED (invalid/permission সমস্যা)")
                    continue
                if self._is_hard_quota_exhaustion(msg):
                    self._release(item, "EXHAUSTED", error=msg)
                    exhausted = sum(x["state"] == "EXHAUSTED" for x in self.keys)
                    print(f"Key {no} QUOTA-EXHAUSTED ({exhausted}/{len(self.keys)}); এই run-এ আর ব্যবহার হবে না")
                    if exhausted == len(self.keys):
                        raise AllGeminiKeysExhausted(
                            f"সব {len(self.keys)}টি Gemini API key-এর quota শেষ। "
                            "Bot বন্ধ করা হচ্ছে—আর কোনো retry হবে না।"
                        ) from e
                    continue
                if any(w in msg for w in RETRY_WORDS):
                    delay = self._cooldown_seconds(msg)
                    self._release(item, "COOLDOWN", cooldown=delay, error=msg)
                    print(f"Key {no} COOLDOWN {delay:.0f}s; অন্য FREE key চেষ্টা হবে")
                    continue
                self._release(item, "FREE", error=msg)
                raise
        exhausted = sum(x["state"] == "EXHAUSTED" for x in self.keys)
        if exhausted == len(self.keys):
            raise AllGeminiKeysExhausted(
                f"সব {len(self.keys)}টি Gemini API key-এর quota শেষ। Bot বন্ধ করা হচ্ছে।"
            ) from last
        raise RuntimeError("Gemini-এর সব key-এ retry শেষ; কাজ অসম্পূর্ণ") from last


rot = Rotator(KEYS)


def with_models(models, fn):
    last = None
    for m in list(models):
        print(f"Model: {m}")
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


def gen_text(client, model, contents, json_mode=False):
    cfg = types.GenerateContentConfig(response_mime_type="application/json") if json_mode else None
    out = []
    for ch in client.models.generate_content_stream(model=model, contents=contents, config=cfg):
        if ch.text:
            out.append(ch.text)
    return "".join(out)


def strip_tags(t):
    t = re.sub(r"<[^<>\n]{1,40}>", "", t)
    return re.sub(r"[ \t]{2,}", " ", t)


def clean_script(t):
    """PDF-এর workflow থেকে আসা অতিরিক্ত লেখা (intro, word count, State/next, [TAG]) ছেঁটে ফেলে।"""
    t = re.split(r"\n\s*(?:-{3,}|\**\s*Final Word Count|\(State)", t, maxsplit=1)[0]
    t = re.sub(r"\[[^\]\n]{1,60}\]", "", t)
    drop = re.compile(r"(AniDoc|ANIDOC|Video Engine|Topic\s*:|Word Count|\bwpm\b|State\s*\d|"
                      r"reply\s+['\"]?next|Approx\.?\s*video|^\s*#)", re.I)
    t = "\n".join(l for l in t.splitlines() if not drop.search(l))
    t = re.sub(r"[*#_`]+", "", t)
    t = re.sub(r"[ \t]{2,}", " ", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


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
    "বিরতি বোঝাতে শুধু দাঁড়ি (।), কমা আর ... ব্যবহার করো। "
    "আউটপুট হবে শুধুই narrator-এর বলার পূর্ণ script। কোনো অভিবাদন/নিজের পরিচয় (যেমন 'আমি AniDoc AI Video Engine'), "
    "Topic লাইন, [TENSION PEAK] জাতীয় bracket tag, '---', Word Count, video duration/wpm, "
    "'State complete', 'reply next' বা অন্য কোনো মন্তব্য লিখবে না। PDF-এর State/step workflow এখানে প্রযোজ্য নয়; "
    "প্রথম লাইন থেকেই সরাসরি narration শুরু করো আর শেষ narration লাইনেই থামো।"
)

SFX_NAMES = ("whoosh", "boom", "riser", "wind", "heartbeat")


def parse_json(text):
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    return json.loads(text)


def split_scenes(text, limit):
    """narration-কে ~limit অক্ষরের (≈4-6 সেকেন্ড) scene-এ ভাগ করে।"""
    max_len, min_len = int(limit * 1.2), int(limit * 0.7)
    sents = [s.strip() for s in re.split(r"(?<=[।\.\?\!])\s+", text) if s.strip()]
    pieces = []
    for s in sents:
        if len(s) <= max_len:
            pieces.append(s)
            continue
        for p in [x.strip() for x in re.split(r"(?<=[,;:])\s+", s) if x.strip()]:
            if len(p) <= max_len:
                pieces.append(p)
                continue
            cur = ""
            for w in p.split():
                if cur and len(cur) + len(w) + 1 > max_len:
                    pieces.append(cur)
                    cur = w
                else:
                    cur = (cur + " " + w).strip()
            if cur:
                pieces.append(cur)
    scenes, cur = [], ""
    for p in pieces:
        if cur and (len(cur) >= limit or len(cur) + len(p) + 1 > max_len):
            scenes.append(cur)
            cur = p
        else:
            cur = (cur + " " + p).strip()
    if cur:
        if scenes and len(cur) < min_len and len(scenes[-1]) + len(cur) + 1 <= max_len * 1.3:
            scenes[-1] += " " + cur
        else:
            scenes.append(cur)
    return scenes


# =====================================================================
# Visual bible: character / clothing / shop / style consistency
# =====================================================================
def make_bible(pdf_part, narration):
    ask = (
        f"Documentary topic: {TOPIC}\n"
        "সংযুক্ত PDF-এ ANIDOC-এর নির্দেশনা আছে (style/visual নিয়ম থাকলে মানো)। নিচের narration পড়ে "
        "পুরো ভিডিওর জন্য একটা VISUAL BIBLE বানাও, যাতে সব image-এ একই চরিত্র, পোশাক, জায়গা ও style থাকে।\n"
        "নিয়ম:\n"
        "- সব description ইংরেজিতে, নির্দিষ্ট ও স্থির (পরে হুবহু copy-paste হবে)।\n"
        "- character desc (40-70 শব্দ): বয়স, skin tone, মুখের গড়ন, চুল/দাড়ি, গড়ন, পোশাকের প্রতিটা অংশ "
        "রঙ + কাপড়সহ, accessories (টুপি, ঘড়ি, চশমা...), জুতো। এক চরিত্র সব scene-এ একই পোশাকে।\n"
        "- location desc (40-70 শব্দ): স্থাপত্য, দেয়াল/মেঝের রঙ, দোকান হলে সামনের অংশ, awning, shelf, "
        "ভেতরের সাজ, জিনিসপত্র, আলো। readable লেখা/signboard text রাখবে না।\n"
        "- props: বারবার আসা গুরুত্বপূর্ণ জিনিস (যেমন একটা নির্দিষ্ট পোশাক, গাড়ি, বই)। desc 20-40 শব্দ।\n"
        "- style: একটা লাইনে পুরো ভিডিওর visual style, lens, lighting, color grade।\n"
        "- narration-এ মানুষ/দোকান না থাকলে (যেমন শুধু বিজ্ঞান/ইতিহাস) characters/locations "
        "খালি বা কম রাখো, বানিয়ে বাড়াবে না। সর্বোচ্চ 6 character, 5 location, 6 prop।\n"
        'শুধু JSON: {"style": "...", "palette": "...", "characters": [{"id": "C1", "name": "...", "desc": "..."}], '
        '"locations": [{"id": "L1", "name": "...", "desc": "..."}], "props": [{"id": "P1", "name": "...", "desc": "..."}]}\n\n'
        "NARRATION:\n" + narration[:15000])
    for attempt in range(3):
        try:
            data = with_models(TEXT_MODELS, lambda c, m: gen_text(c, m, [pdf_part, ask], json_mode=True))
            b = parse_json(data)
            b.setdefault("style", "cinematic photorealistic documentary, 35mm lens, natural light")
            for k in ("characters", "locations", "props"):
                b[k] = [x for x in b.get(k, []) if x.get("id") and x.get("desc")]
            return b
        except Exception as e:
            print("Visual bible সমস্যা, আবার চেষ্টা:", str(e)[:100])
    print("সতর্কতা: visual bible বানানো যায়নি, default style ব্যবহার হবে")
    return {"style": "cinematic photorealistic documentary, 35mm lens, natural light, rich color grade",
            "palette": "", "characters": [], "locations": [], "props": []}


def compose_prompt(bible, sc):
    cmap = {x["id"]: x for x in bible["characters"]}
    lmap = {x["id"]: x for x in bible["locations"]}
    pmap = {x["id"]: x for x in bible["props"]}
    parts = [sc["shot"].strip().rstrip(".") + "."]
    chars = [cmap[i] for i in sc.get("characters", []) if i in cmap]
    if chars:
        parts.append("CHARACTERS (keep exactly identical in every image): " + " ".join(
            f"{c.get('name', c['id'])}: {c['desc'].strip().rstrip('.')}." for c in chars))
    loc = lmap.get(sc.get("location"))
    if loc:
        parts.append(f"SETTING (identical in every image): {loc.get('name', '')}: {loc['desc'].strip().rstrip('.')}.")
    props = [pmap[i] for i in sc.get("props", []) if i in pmap]
    if props:
        parts.append("PROPS: " + " ".join(f"{p.get('name', p['id'])}: {p['desc'].strip().rstrip('.')}." for p in props))
    if sc.get("outfit"):
        parts.append("OUTFIT FOR THIS IMAGE (overrides the outfit above): " + sc["outfit"].strip().rstrip(".") + ".")
    style = bible["style"].strip().rstrip(".") + "."
    if bible.get("palette"):
        style += f" Color palette: {bible['palette'].strip().rstrip('.')}."
    parts.append("STYLE: " + style)
    parts.append(f"{IMAGE_ASPECT} widescreen, ultra detailed, sharp focus, consistent character faces and outfits, "
                 "no text, no captions, no watermark, no logo, no extra fingers, no distorted faces.")
    return " ".join(parts)


# =====================================================================
# MODE 1: script + visual bible + image prompts + youtube package
# =====================================================================
GEN_FILES = {"scenes.json", "image_map.json", "script.txt", "youtube_package.txt",
             "all_prompts.txt", "image_prompts.txt",
             "all_image_prompts.txt", "all_video_prompts.txt"}
SCENE_FILE_RE = re.compile(r"^scene_\d{3,4}_(prompt|image|video)\.txt$")


def clip_len(sc):
    """Flow/Veo-র জন্য clip-এর দৈর্ঘ্য: 4, 6 বা 8 সেকেন্ড (বেশি হলে bot ছেঁটে নেয়)।"""
    if sc.get("dialogue"):
        words = sum(len(d["line"].split()) for d in sc["dialogue"])
        need = words / 2.3 + 1.0
    else:
        need = len(sc["narration"]) / 12.0
    for L in (4, 6, 8):
        if need <= L:
            return L
    return 8


def compose_video_prompt(bible, sc):
    """Flow-তে image-to-video বানানোর বিস্তারিত prompt: ছবি যেন নষ্ট/বদলে না যায়।"""
    L = sc["duration"]
    cmap = {x["id"]: x for x in bible["characters"]}
    lmap = {x["id"]: x for x in bible["locations"]}
    parts = [
        f"IMAGE-TO-VIDEO. Exactly {L} seconds, {IMAGE_ASPECT} widescreen, one continuous shot, no cuts, no scene change.",
        "FIRST FRAME: use the provided image exactly as the opening frame. Keep every face, hairstyle, outfit, object, "
        "background and color identical to the image; do not redraw, restyle or re-imagine it.",
        "STARTING SCENE: " + sc["shot"].strip().rstrip(".") + ".",
        f"WHAT CHANGES DURING THE {L} SECONDS (beginning to end): "
        + (sc.get("motion") or "slow cinematic camera push-in with subtle natural movement of the subject").strip().rstrip(".") + ".",
    ]
    chars = [cmap[i] for i in sc.get("characters", []) if i in cmap]
    if chars:
        parts.append("CHARACTERS (identical to the image in every frame): " + " ".join(
            f"{c.get('name', c['id'])}: {c['desc'].strip().rstrip('.')}." for c in chars))
    if sc.get("outfit"):
        parts.append("CLOTHING CHANGE IN THIS SCENE: " + sc["outfit"].strip().rstrip(".")
                     + ". This overrides the outfit in the character description; keep it unchanged for the whole clip.")
    else:
        parts.append("CLOTHING: exactly as in the image for the whole clip, no outfit change.")
    loc = lmap.get(sc.get("location"))
    if loc:
        parts.append(f"SETTING (identical to the image): {loc.get('name', '')}: {loc['desc'].strip().rstrip('.')}.")
    style = bible["style"].strip().rstrip(".") + "."
    if bible.get("palette"):
        style += f" Color palette: {bible['palette'].strip().rstrip('.')}."
    parts.append("STYLE: " + style)
    if sc.get("dialogue"):
        said = []
        names = []
        for d in sc["dialogue"]:
            nm = cmap.get(d["speaker"], {}).get("name") or d["speaker"] or "the character"
            names.append(nm)
            v = f" Voice: {d['voice']}." if d.get("voice") else ""
            said.append(f'{nm} says in Hindi: "{d["line"]}".{v}')
        parts.append(
            "DIALOGUE (spoken on camera): " + " ".join(said) + " "
            f"Only {' and '.join(dict.fromkeys(names))} speaks, no other voice. The face stays clearly visible and turned toward the "
            "camera with the mouth fully visible; lips, jaw and cheeks move precisely in sync with every Hindi syllable, "
            "with natural blinks and small head movement. Speech starts about 0.3 seconds after the clip begins and "
            "finishes at least 0.5 seconds before it ends. Clear natural voice, no echo, no background music, no subtitles.")
    else:
        snd = [x["desc"] for x in (sc.get("sounds") or []) if x.get("desc")]
        if snd:
            when = {"start": "at the beginning", "mid": "in the middle", "end": "near the end"}
            seq = "; ".join(f"{x['desc']} ({when.get(x.get('at'), 'at the beginning')})"
                            for x in sc["sounds"] if x.get("desc"))
            parts.append("AUDIO (generated together with the clip, realistic, perfectly in sync with the visible action, "
                         f"natural level): {seq}; plus soft natural room tone. No speech, no voice-over, no music.")
        else:
            parts.append("AUDIO: only soft natural ambient sound that fits the place. No speech, no voice-over, no music.")
    parts.append("CONSISTENCY: temporally stable, no morphing or warping of faces, hands or objects, no sudden outfit or "
                 "background change, no flicker, no extra people, no on-screen text, subtitles, captions, logos or "
                 "watermarks, sharp focus, natural motion, realistic physics.")
    return " ".join(parts)


FLOW_BATCH = int(os.getenv("FLOW_BATCH", "20"))     # Flow agent এক বারে কত scene করতে পারে (limit 24, তাই 20)


def flow_instructions(total, kind):
    """all_image_prompts / all_video_prompts ফাইলের মাথায় Flow agent-এর জন্য নির্দেশ।"""
    ranges = ", ".join(f"{a:03d}-{min(a + FLOW_BATCH - 1, total):03d}" for a in range(1, total + 1, FLOW_BATCH))
    if kind == "image":
        return [
            "INSTRUCTIONS FOR THE FLOW AGENT (read first)",
            f"This file has {total} scenes. For EVERY scene generate exactly ONE 16:9 image from that scene's IMAGE PROMPT,",
            "and immediately rename the generated image to the exact IMAGE NAME written in that scene's block.",
            "- The name is ONLY the three-digit scene number: 001, 002, 003 ... (no words, no scene_, no extension, no extra text).",
            "- One image per scene. Do not make extra variations, do not skip a scene, do not reuse a number.",
            f"- Work in batches of {FLOW_BATCH} scenes: {ranges}. Finish a batch, then list 'name -> scene' to confirm before the next batch.",
            "- If one image fails, retry only that scene and give it the same name.",
            "- Never draw text, captions, logos or watermarks inside the images.",
            "",
            "MESSAGE TO SEND TO THE FLOW AGENT (copy-paste):",
            f"  Use all_image_prompts.txt. Generate scene 001 to {min(FLOW_BATCH, total):03d}: one image per scene, "
            "and rename each image to its scene number (001, 002, ...).",
            "=" * 60, ""]
    return [
        "INSTRUCTIONS FOR THE FLOW AGENT (read first)",
        f"This file has {total} scenes. For EVERY scene make exactly ONE video clip (image-to-video) from that scene's VIDEO PROMPT,",
        "using the image that is already named with the same scene number as the first frame,",
        "and immediately rename the generated clip to the exact VIDEO NAME written in that scene's block.",
        "- The name is ONLY the three-digit scene number: 001, 002, 003 ... (no words, no extension, no extra text).",
        "- One clip per scene, with exactly the CLIP LENGTH written in the block. Do not skip a scene or reuse a number.",
        f"- Work in batches of {FLOW_BATCH} scenes: {ranges}. Finish a batch, then list 'name -> scene' to confirm.",
        "- If one clip fails, retry only that scene and give it the same name.",
        "",
        "MESSAGE TO SEND TO THE FLOW AGENT (copy-paste):",
        f"  Use all_video_prompts.txt. Make scene 001 to {min(FLOW_BATCH, total):03d} as image-to-video from the images with the same numbers, "
        "and rename each clip to its scene number (001, 002, ...).",
        "=" * 60, ""]


def scene_image_text(sc, total):
    """শুধু IMAGE prompt (video prompt এখানে নেই)।"""
    tag = f"{sc['n']:03d}"
    return "\n".join([
        f"=== SCENE {tag} of {total} ===",
        f"IMAGE NAME (rename the generated image to exactly this, nothing else): {tag}",
        "IMAGE PROMPT:",
        sc["image_prompt"],
        ""])


def scene_video_text(sc, total):
    """শুধু VIDEO prompt (image prompt এখানে নেই)।"""
    tag = f"{sc['n']:03d}"
    has = bool(sc.get("dialogue"))
    return "\n".join([
        f"=== SCENE {tag} of {total} ===",
        f"VIDEO NAME (rename the generated clip to exactly this, nothing else): {tag}",
        f"FIRST FRAME: the image already named {tag}",
        f"CLIP LENGTH: {sc['duration']} seconds",
        "HAS DIALOGUE: " + ("YES - the clip's own audio is used and the narration is muted for this scene"
                            if has else "NO - the narration voice plays over this clip"),
        "NARRATION (reference only; do NOT speak it in the clip"
        + (", the dialogue below replaces it" if has else "") + "):",
        sc["narration"],
        "VIDEO PROMPT:",
        sc["video_prompt"],
        ""])


def archive_folder_id(create=False):
    for f in d_list(OUT_ID, f"and mimeType='{FOLDER_MIME}'"):
        if f["name"] == ARCHIVE_NAME:
            return f["id"]
    return d_mkdir(ARCHIVE_NAME, OUT_ID) if create else None


def read_used_topics():
    arch = archive_folder_id()
    if not arch:
        return []
    for f in d_list(arch, "and name='topics_used.txt'"):
        d_download(f["id"], "topics_used.txt")
        return [l.strip() for l in open("topics_used.txt", encoding="utf-8") if l.strip()]
    return []


def add_used_topic(topic):
    try:
        used = read_used_topics()
        used.append(topic)
        arch = archive_folder_id(create=True)
        for f in d_list(arch, "and name='topics_used.txt'"):
            d_delete(f["id"])
        open("topics_used.txt", "w", encoding="utf-8").write("\n".join(used))
        d_upload("topics_used.txt", "topics_used.txt", arch, "text/plain")
    except Exception as e:
        print("topic তালিকা সেভ করা যায়নি:", str(e)[:100])


def trends_rss_titles():
    import urllib.request
    url = f"https://trends.google.com/trending/rss?geo={TREND_GEO}"
    xml = urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}),
                                 timeout=30).read().decode("utf-8", "ignore")
    return [re.sub(r"<[^>]+>", "", t).strip() for t in re.findall(r"<item>.*?<title>(.*?)</title>", xml, re.S)]


def _json_array(txt):
    m = re.search(r"\[.*\]", txt, re.S)
    return json.loads(m.group(0)) if m else []


def pick_trending_topic():
    """Google trending থেকে (কোনো category বাদ না দিয়ে) নতুন topic বাছে; আগে ব্যবহার হওয়া topic বাদ।"""
    used = read_used_topics()
    used_l = [u.lower() for u in used]
    today = datetime.date.today().strftime("%d %B %Y")
    avoid = ("\nAlready used, do NOT repeat: " + " | ".join(used[-40:])) if used else ""
    shape = ('Reply ONLY with a JSON array: [{"topic": "clear documentary topic, max 14 words, '
             'Hindi+English mix is fine", "why": "one line"}]')
    ask = (f"Today is {today}. Use Google Search to find what is trending RIGHT NOW in region {TREND_GEO} "
           "(Google Trends, top news, viral topics). Collect 8 different trending topics from ANY category "
           "(news, tech, science, history, business, crime, sports, entertainment, world events, anything). "
           "Prefer topics with a real story behind them that can be told as an 8-12 minute documentary. "
           "Do not filter by sensitivity; include whatever is genuinely trending. " + shape + avoid)

    def search(client, model):
        cfg = types.GenerateContentConfig(tools=[types.Tool(google_search=types.GoogleSearch())])
        return client.models.generate_content(model=model, contents=ask, config=cfg).text or ""

    cands = []
    try:
        cands = [str(x["topic"]).strip() for x in _json_array(with_models(TEXT_MODELS, search)) if x.get("topic")]
    except AllGeminiKeysExhausted:
        raise
    except Exception as e:
        print("Google Search দিয়ে trending পাওয়া যায়নি, Google Trends RSS চেষ্টা:", str(e)[:150])
    if not cands:
        try:
            qs = trends_rss_titles()
            ask2 = ("Google Trends-এ এখন এই searchগুলো trending: " + " | ".join(qs[:25]) +
                    "\nএর মধ্য থেকে যেগুলো থেকে documentary বানানো যায় (কোনো category বাদ দিও না) সেগুলো থেকে ৮টা documentary topic বানাও। "
                    + shape + avoid)
            cands = [str(x["topic"]).strip() for x in
                     _json_array(with_models(TEXT_MODELS, lambda c, m: gen_text(c, m, ask2))) if x.get("topic")]
        except AllGeminiKeysExhausted:
            raise
        except Exception as e:
            print("Trends RSS-ও কাজ করেনি:", str(e)[:150])
    for c in cands:
        cl = c.lower()
        if not any(cl == u or cl in u or u in cl for u in used_l):
            print("Trending topic বাছা হলো:", c)
            return c
    raise BotError("নতুন trending topic পাওয়া যায়নি")


def archive_and_clean():
    """নতুন prompt পাঠানোর আগে: folder-এ mp4 থাকলে সেটা একমাত্র archive folder-এ সরিয়ে বাকি সব
    স্থায়ীভাবে (trash নয়) মোছে। mp4 না থাকলে শুধু আগের bot-বানানো prompt ফাইল মোছে।
    (নিজের bgm.* গান, pack*.mp3 sound pack, Sounds folder, sfxlib_index.json আর archive folder কখনো মোছা হয় না।)"""
    items = d_list(OUT_ID)
    mp4s = [f for f in items if f["name"].lower().endswith(".mp4") and f["mimeType"] != FOLDER_MIME]
    if not mp4s:
        n = 0
        for f in items:
            gen = f["name"] in GEN_FILES or SCENE_FILE_RE.match(f["name"])
            cache = f["name"] == "_audio_cache" and f["mimeType"] == FOLDER_MIME
            if gen or cache:
                try:
                    d_delete(f["id"])
                    n += 1
                except Exception as e:
                    print("পুরনো ফাইল মোছা যায়নি:", f["name"], str(e)[:80])
        print(f"mp4 নেই; আগের prompt ফাইল পরিষ্কার: {n}টা")
        return
    arch = archive_folder_id(create=True)
    for f in mp4s:   # আগে সরানো; সরাতে ব্যর্থ হলে exception, কিছু মোছা হয় না
        drive.files().update(fileId=f["id"], addParents=arch, removeParents=OUT_ID, fields="id").execute()
        print("mp4 সরানো হলো:", f["name"], "->", ARCHIVE_NAME)
    keep = {f["id"] for f in mp4s} | {arch}
    n = 0
    for f in items:
        if f["id"] in keep or (f["name"] == ARCHIVE_NAME and f["mimeType"] == FOLDER_MIME):
            continue
        if f["name"].lower().startswith(("bgm", "sfx_", "pack", "sfxlib")):
            continue
        if f["mimeType"] == FOLDER_MIME and f["name"].lower() in LIB_FOLDERS:
            continue
        try:
            d_delete(f["id"])
            n += 1
        except Exception as e:
            print("মোছা যায়নি:", f["name"], str(e)[:80])
    print(f"স্থায়ীভাবে মোছা হলো: {n}টা ফাইল/folder")


def _clean_sounds(raw, avail):
    out = []
    for x in (raw or []):
        if not isinstance(x, dict) or not str(x.get("desc", "")).strip():
            continue
        nm = str(x.get("name", "")).strip().lower()
        at = str(x.get("at", "start")).strip().lower()
        out.append({"name": nm if nm in avail else "", "desc": str(x["desc"]).strip().rstrip("."),
                    "at": at if at in ("start", "mid", "end") else "start"})
    return out[:2]


def run_prompts():
    global TOPIC
    if not TOPIC:
        if not AUTO_TOPIC:
            raise BotError("prompts mode-এ topic দিতে হবে")
        TOPIC = pick_trending_topic()      # topic ফাঁকা থাকলে Google trending থেকে
    pdf_part = types.Part.from_bytes(data=open(PDF_PATH, "rb").read(), mime_type="application/pdf")

    def make_script(client, model):
        prompt = (
            "সংযুক্ত PDF-এ ANIDOC-এর নির্দেশনা আছে। সেটা অনুসরণ করে "
            f"এই বিষয়ে পূর্ণ documentary script লেখো: {TOPIC}\n\n" + LANG_RULE + "\n\n" + NO_TAG_RULE)
        return gen_text(client, model, [pdf_part, prompt])

    script = clean_script(strip_tags(with_models(TEXT_MODELS, make_script)))
    print("Script:", len(script), "chars")

    def make_narration(client, model):
        prompt = (
            "নিচের script থেকে শুধু narrator-এর বলার লাইনগুলো বের করো। Scene heading, timestamp, "
            "[ ] বা < > নির্দেশ, visual/music notes, markdown চিহ্ন, অভিবাদন/নিজের পরিচয়, Topic লাইন, Word Count, duration, State/next বার্তা — সব বাদ। হিন্দি শব্দ দেবনাগরীতে, "
            "English শব্দ English অক্ষরে থাকবে। প্রতিটা বাক্য শেষে দাঁড়ি (।) বা full stop দাও। "
            "শুধু পরিষ্কার narration text দাও।\n\n" + script)
        return gen_text(client, model, prompt)

    narration = with_models(TEXT_MODELS, make_narration).strip()
    narration = re.sub(r"[*#_`]+", "", narration)
    narration = clean_script(strip_tags(re.sub(r"\[[^\]\n]{1,60}\]", "", narration)))

    texts = split_scenes(narration, SCENE_CHARS)
    print("মোট scene (= ছবি/clip লাগবে):", len(texts), f"(≈ {len(texts) * 5 / 60:.1f} মিনিট, 5 সেকেন্ড/scene ধরে)")

    bible = make_bible(pdf_part, narration)
    print("Visual bible:", len(bible["characters"]), "character,",
          len(bible["locations"]), "location,", len(bible["props"]), "prop")

    cids = ", ".join(x["id"] + "=" + x.get("name", "") for x in bible["characters"]) or "(নেই)"
    lids = ", ".join(x["id"] + "=" + x.get("name", "") for x in bible["locations"]) or "(নেই)"
    pids = ", ".join(x["id"] + "=" + x.get("name", "") for x in bible["props"]) or "(নেই)"

    try:
        usr = sorted(user_sfx_names(d_list(OUT_ID)))
    except Exception:
        usr = []
    try:
        _lib = lib_load(d_list(OUT_ID), cut=False)
    except Exception as e:
        print("Sound library পড়া যায়নি:", type(e).__name__, str(e)[:100])
        _lib = []
    libtags = sorted({s["tag"] for s in _lib if s["kind"] in ("scene", "ambient", "impact") and s["tag"] != "other"})
    avail_sounds = list(dict.fromkeys(list(SOUND_NAMES) + usr + libtags))
    print("Sound library:", ", ".join(avail_sounds))
    shots = {}
    B = 10
    for i in range(0, len(texts), B):
        batch = [(i + j + 1, t) for j, t in enumerate(texts[i:i + B])]
        listing = "\n".join(f"SCENE {n}: {t}" for n, t in batch)
        prev = ""
        if i > 0 and i in shots:
            prev = "আগের scene-এর shot (পুনরাবৃত্তি এড়াতে): " + shots[i]["shot"][:200] + "\n"
        ask = (
            f"Documentary topic: {TOPIC}\n"
            "সংযুক্ত PDF-এ ANIDOC-এর নির্দেশনা আছে (image/video-র নিয়ম থাকলে মানো)। "
            "প্রতিটা scene মাত্র ৪-৬ সেকেন্ডের একটা আলাদা shot (পরে Flow-তে image→video clip হবে)। প্রতিটার জন্য লেখো:\n"
            '- "shot": ইংরেজিতে 50-80 শব্দের বিস্তারিত বর্ণনা — clip-এর প্রথম frame: narration-এর সাথে মেলে এমন action/ঘটনা, '
            "shot type (wide/medium/close-up/over-the-shoulder/aerial/low-angle...), camera angle, lens, "
            "lighting, mood, foreground ও background, composition। পাশের scene-এর চেয়ে angle বা subject আলাদা হবে। "
            "চরিত্রদের পোশাক/চেহারা এখানে লিখবে না (সেটা আমি নিজে বসাব); চরিত্রকে শুধু নাম দিয়ে উল্লেখ করো।\n"
            '- "motion": ইংরেজিতে 40-70 শব্দ — ওই clip-এর সময়ে শুরু থেকে শেষ পর্যন্ত ধাপে ধাপে কী বদলাবে: চরিত্রের action, '
            "camera movement (slow push-in/pan/tilt/orbit...), আলো/আবহাওয়া/পরিবেশের পরিবর্তন। শুরুর frame-এর চেহারা, "
            "পোশাক, background ঠিক থাকবে, শুধু নড়াচড়া ও camera বদলাবে।\n"
            '- "outfit": সাধারণত "" (bible-এর পোশাকই থাকবে)। গল্পের দরকারে কোনো চরিত্র এই scene-এ অন্য পোশাক পরলে '
            "ইংরেজিতে নতুন পোশাকের পূর্ণ বর্ণনা (রঙ + কাপড়সহ) লেখো।\n"
            '- "dialogue": সাধারণত []। যদি narration-এ কারো সরাসরি উক্তি/কথোপকথন থাকে বা চরিত্রের মুখে একটা ছোট লাইন '
            "বসালে scene জমে, তবেই: "
            '[{"speaker": "C1", "line": "...", "voice": "calm deep male voice"}]। line হবে narration-এর একই তথ্য বহন করা, '
            "সর্বোচ্চ ১২ শব্দ, হিন্দি দেবনাগরীতে (English শব্দ English অক্ষরে)। dialogue থাকলে ওই scene-এ narration বাজবে না, "
            "তাই line-ই পুরো কথা বলবে। প্রতি ১০ scene-এ সর্বোচ্চ ১টা; speaker অবশ্যই character ID। "
            "dialogue থাকলে ওই scene-এর shot অবশ্যই medium close-up বা close-up হবে: speaker-এর পুরো মুখ স্পষ্ট ও ক্যামেরার দিকে "
            "ফেরানো (মুখ হাত/ছায়া/অন্য কিছুতে ঢাকা নয়), frame-এ একজনই কথা বলবে, চোখ-ঠোঁট-চোয়াল পরিষ্কার দেখা যাবে।\n"
            '- "characters": এই shot-এ যে character ID-গুলো দেখা যাবে\n'
            '- "location": location ID (না থাকলে "")\n'
            '- "props": দৃশ্যমান prop ID-র তালিকা\n'
            '- "sfx": whoosh (transition) | boom (নাটকীয় মুহূর্ত) | riser (revelation-এর আগে) | '
            'wind (নির্জন/space) | heartbeat (suspense) | "" । প্রতি 8 scene-এ সর্বোচ্চ 1টা, বেশিরভাগ ""।\n'
            '- "transition": এই scene-এ ঢোকার transition। fade (সাধারণ, ডিফল্ট) | fadeblack (সময়ের লাফ/অধ্যায় শেষ/অন্ধকারে ডুবে যাওয়া) | '
            'flash (আঘাত/বিস্ফোরণ/চমকপ্রদ revelation) | zoom (মনোযোগ তীব্র হচ্ছে/গুরুত্বপূর্ণ কিছুর ভেতরে ঢোকা) | blur (স্মৃতি/স্বপ্ন/অতীতে ফেরা) | '
            'glitch (প্রযুক্তি/রহস্য/অস্বাভাবিকতা) | whip (দ্রুত গতি/তাড়া/অ্যাকশন) | dissolve (শান্ত সময়ের প্রবাহ)। '
            'বেশিরভাগ scene-এ fade; প্রতি ১০ scene-এ সর্বোচ্চ ৩টায় অন্য ধরন, আর পরপর দুই scene-এ একই বিশেষ ধরন নয়।\n'
            '- "mood": এই অংশের background music-এর মেজাজ: calm | emotional | mysterious | tense | dark | epic | action | uplifting। '
            'গল্পের সুর সত্যিই বদলালে তবেই বদলাও; পরপর অনেক scene-এ একই mood রাখো (গান বারবার বদলানো চলবে না)।\n'
            '- "sounds": সাধারণত []। শুধু যখন narration বা দৃশ্যে স্পষ্টভাবে কোনো শব্দ আছে (কুকুরের ডাক, ঘণ্টা, পায়ের শব্দ, বৃষ্টি, ভিড়, '
            'গুলি, রেডিও, দরজায় টোকা...) তখন সর্বোচ্চ ২টা: [{"name": "<Available sound names থেকে একটা; না মিললে ফাঁকা string>", '
            '"desc": "ইংরেজিতে ছোট বর্ণনা, যেমন distant temple bell tolling twice", "at": "start|mid|end"}]। '
            'at = scene-এর কোন অংশে শব্দটা হবে। প্রতি ৩ scene-এ সর্বোচ্চ ১টা scene-এ sounds থাকবে; dialogue থাকলে []।\n'
            f"Available sound names: {', '.join(avail_sounds)}\n"
            f"Characters: {cids}\nLocations: {lids}\nProps: {pids}\n"
            "শুধু ঐ ID-গুলোই ব্যবহার করো, নতুন বানাবে না।\n"
            'শুধু JSON array: [{"n": 1, "shot": "...", "motion": "...", "outfit": "", "dialogue": [], '
            '"characters": ["C1"], "location": "L1", "props": [], "sfx": "", "transition": "fade", "mood": "calm", "sounds": []}] '
            "— ঠিক এই scene নম্বরগুলো দিয়ে।\n" + prev + "\n" + listing)
        got = {}
        for attempt in range(3):
            try:
                data = with_models(TEXT_MODELS, lambda c, m: gen_text(c, m, [pdf_part, ask], json_mode=True))
                arr = parse_json(data)
                got = {int(x["n"]): x for x in arr if x.get("shot")}
                if all(n in got for n, _ in batch):
                    break
            except Exception as e:
                print("Shot parse সমস্যা, আবার চেষ্টা:", str(e)[:100])
        for n, _ in batch:
            if n not in got:
                raise BotError(f"Scene {n}-এর prompt পাওয়া যায়নি, আবার run করো")
            shots[n] = got[n]
        print("Prompt তৈরি:", min(i + B, len(texts)), "/", len(texts))

    scenes = []
    for n, t in enumerate(texts, 1):
        s = shots[n]
        sfx = str(s.get("sfx") or "").strip().lower()
        dl = []
        for d in (s.get("dialogue") or []):
            if isinstance(d, dict) and str(d.get("line", "")).strip():
                dl.append({"speaker": str(d.get("speaker", "")).strip(),
                           "line": str(d["line"]).strip(),
                           "voice": str(d.get("voice", "")).strip()})
        sc = {"n": n, "narration": t, "shot": str(s["shot"]).strip(),
              "motion": str(s.get("motion") or "").strip(),
              "outfit": str(s.get("outfit") or "").strip(),
              "dialogue": dl,
              "characters": [str(x) for x in (s.get("characters") or [])],
              "location": str(s.get("location") or ""),
              "props": [str(x) for x in (s.get("props") or [])],
              "sfx": "" if dl else (sfx if sfx in SFX_NAMES else ""),
              "mood": (str(s.get("mood") or "").strip().lower()
                       if str(s.get("mood") or "").strip().lower() in BGM_MOODS else ""),
              "transition": (str(s.get("transition") or "").strip().lower()
                             if str(s.get("transition") or "").strip().lower() in TRANS_STYLES else ""),
              "sounds": [] if dl else _clean_sounds(s.get("sounds"), avail_sounds)}
        sc["duration"] = clip_len(sc)
        sc["image_prompt"] = compose_prompt(bible, sc)
        sc["video_prompt"] = compose_video_prompt(bible, sc)
        scenes.append(sc)
    print("Dialogue scene:", sum(1 for x in scenes if x["dialogue"]), "টা")

    def make_pkg(client, model):
        ask = (
            f"Documentary topic: {TOPIC}\nনিচের script-এর ভিত্তিতে YouTube-এর জন্য একটা plain text package বানাও "
            "(PDF-এ title/thumbnail/SEO-র নিয়ম থাকলে মানো)। এই অংশগুলো, এই heading দিয়ে:\n"
            "THUMBNAIL IMAGE PROMPT (English, 16:9, ৩টা আলাদা idea; ছবির ওপর ছোট দেবনাগরী text থাকলে সেটাও লেখো)\n"
            "TITLES (৫টা, ৭০ অক্ষরের মধ্যে, হিন্দি+English) আর BEST TITLE\n"
            "DESCRIPTION (প্রথম ২ লাইনে hook, ৩০০-৪০০ শব্দ, keyword স্বাভাবিকভাবে)\n"
            "TAGS (comma দিয়ে, মোট ৪৫০ অক্ষরের মধ্যে)\nHASHTAGS (৩-৫টা)\n"
            "SEO KEYWORDS (primary আর secondary)\nPINNED COMMENT\n\nSCRIPT:\n" + script[:12000])
        return gen_text(client, model, [pdf_part, ask])

    pkg = with_models(TEXT_MODELS, make_pkg).strip()

    # ---- ফাইল তৈরি (সব output folder-এর ভেতরেই যাবে, কোনো নতুন folder নয়) ----
    os.makedirs("prompt_files", exist_ok=True)
    total = len(scenes)
    head = [
        f"TOPIC: {TOPIC}",
        f"মোট scene: {total}",
        "নিয়ম: সব IMAGE prompt আছে all_image_prompts.txt-এ আর সব VIDEO prompt আছে all_video_prompts.txt-এ। Flow-তে বানানো প্রতিটা clip/ছবির নাম হবে "
        "শুধু ওই নম্বর (001.mp4 / 001.png)। সব ফাইল একটা .zip করে এই output folder-এ দাও।",
        "bot প্রতি 2 মিনিটে folder দেখে; zip পেলে নিজে video বানাবে।",
        "যে scene-এ dialogue আছে, সেই clip-এ কথা থাকলে bot narration চুপ রেখে clip-এর কথা বাজাবে।",
        "নিজের background music চাইলে 'bgm.mp3' নামে এই folder-এ দাও (না দিলে bot নিজে বানাবে)।",
        "=" * 60, "", "VISUAL BIBLE (সব prompt-এ এটা আপনা-আপনি বসানো আছে)",
        "STYLE: " + bible["style"], "PALETTE: " + str(bible.get("palette", "")), "",
    ]
    for kind in ("characters", "locations", "props"):
        for x in bible[kind]:
            head.append(f"[{x['id']}] {x.get('name', '')}: {x['desc']}")
    head += ["", "=" * 60, ""]

    all_img = flow_instructions(total, "image") + list(head)
    all_vid = flow_instructions(total, "video") + list(head)
    scene_texts = {}
    for sc in scenes:
        n3 = f"{sc['n']:03d}"
        ti = scene_image_text(sc, total)
        tv = scene_video_text(sc, total)
        scene_texts[f"scene_{n3}_image.txt"] = ti
        scene_texts[f"scene_{n3}_video.txt"] = tv
        all_img += [ti]
        all_vid += [tv]
    open("all_image_prompts.txt", "w", encoding="utf-8").write("\n".join(all_img))
    open("all_video_prompts.txt", "w", encoding="utf-8").write("\n".join(all_vid))
    open("script.txt", "w", encoding="utf-8").write(script)
    open("youtube_package.txt", "w", encoding="utf-8").write(pkg)
    json.dump({"topic": TOPIC, "bible": bible, "scenes": scenes},
              open("scenes.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    archive_and_clean()
    for f, mime in [("all_image_prompts.txt", "text/plain"), ("all_video_prompts.txt", "text/plain"), ("script.txt", "text/plain"),
                    ("youtube_package.txt", "text/plain"), ("scenes.json", "application/json")]:
        d_upload(f, f, OUT_ID, mime)
        print("Uploaded:", f)
    print("\nসব prompt output folder-এই দেওয়া হলো। এবার Flow-তে clip বানিয়ে (নাম 001, 002 ...) zip করে ওই folder-এ দাও।")
    add_used_topic(TOPIC)


# =====================================================================
# MODE 2: audio + sync + edit + music + sfx → final_video.mp4
# =====================================================================
SR, FPS, W, H = 24000, 25, 1920, 1080
STYLE = ("professional premium documentary narrator, like a BBC or Netflix history and science documentary: "
         "deep, calm, warm and authoritative voice, slow steady measured pace that never speeds up, "
         "exactly the same tone, pitch and energy from the first word to the last, "
         "controlled dramatic emphasis on key words, short natural pauses only at sentence ends, "
         "no excitement, no casual conversational tone, not robotic")
LEGACY_PREFIX = (
    "Read the following text aloud as a professional premium documentary narrator (BBC / Netflix style): "
    "deep, calm, warm, authoritative; slow, steady, measured pace that never speeds up; "
    "identical tone and energy from start to finish; dramatic emphasis on key words; "
    "short pauses only at sentence ends. Do not read these instructions:\n")
_LEGACY = False
IMG_EXT = (".png", ".jpg", ".jpeg", ".webp")
VID_EXT = (".mp4", ".mov", ".mkv", ".webm")
AUD_EXT = (".mp3", ".wav", ".m4a", ".aac")
NUM_RE = re.compile(r"^(?:scene[_\- ]?)?(\d{1,4})(?=\D|$)", re.I)


def to_pcm(data):
    if data[:4] == b"RIFF":
        with wave.open(io.BytesIO(data), "rb") as w:
            return w.readframes(w.getnframes())
    return data


def tts(client, model, text):
    global _LEGACY
    if model.startswith("gemini-3.8") and not _LEGACY:
        try:
            r = client.models.generate_content(
                model=model,
                contents=[{"role": "user", "parts": [
                    {"text": text, "speech_metadata": {"style": STYLE}}]}],
                config={"response_modalities": ["AUDIO"],
                        "speech_config": {"voice_config": {"voice": VOICE}}},
            )
            return to_pcm(r.candidates[0].content.parts[0].inline_data.data)
        except Exception as e:
            if any(w in str(e).lower() for w in
                   ("validation", "extra_forbidden", "speech_metadata", "voice_config", "unexpected")):
                print("SDK নতুন schema বোঝেনি; এরপর সব block legacy format-এ (tone এক রাখতে)")
                _LEGACY = True
            else:
                raise
    # legacy: style সবসময় prefix-এ থাকে (আগে 3.8-এর fallback-এ style বাদ পড়ত)
    r = client.models.generate_content(
        model=model, contents=LEGACY_PREFIX + text,
        config=types.GenerateContentConfig(
            response_modalities=["AUDIO"],
            temperature=0.3, seed=7,
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=VOICE)))),
    )
    return to_pcm(r.candidates[0].content.parts[0].inline_data.data)


def normalize(pcm, target=3500):
    rms = audioop.rms(pcm, 2)
    if rms == 0:
        return pcm
    g = min(target / rms, 4.0, 30000 / max(audioop.max(pcm, 2), 1))  # clipping হবে না
    return audioop.mul(pcm, 2, g)


def frame_rms(pcm):
    fl = int(SR * 0.02) * 2
    return [audioop.rms(pcm[i:i + fl], 2) for i in range(0, len(pcm) - fl + 1, fl)]


def speech_thr(pcm):
    """কথার জোর অনুযায়ী নিজে থেকে নীরবতার threshold ঠিক করে (fixed 300 নয়)।"""
    r = frame_rms(pcm)
    if not r:
        return 150
    loud = sorted(r)[int(len(r) * 0.7):]
    ref = statistics.median(loud) if loud else 0
    return max(120, 0.12 * ref)


def active_seconds(pcm, thr=None):
    thr = speech_thr(pcm) if thr is None else thr
    return max(sum(1 for v in frame_rms(pcm) if v > thr) * 0.02, 0.1)


def speech_chars(t):
    return len(re.sub(r"\s+", "", t))


def trim_pauses(pcm, thr=None, min_run=None, keep=None):
    thr = speech_thr(pcm) if thr is None else thr
    min_run = MIN_PAUSE_RUN if min_run is None else min_run
    keep = MAX_PAUSE if keep is None else keep
    fl = int(SR * 0.02) * 2
    n = len(pcm) // fl
    sil = [audioop.rms(pcm[i * fl:(i + 1) * fl], 2) <= thr for i in range(n)]
    out, i = [], 0
    min_f, keep_f = int(min_run / 0.02), int(keep / 0.02)
    while i < n:
        if not sil[i]:
            j = i
            while j < n and not sil[j]:
                j += 1
            out.append(pcm[i * fl:j * fl])
            i = j
            continue
        j = i
        while j < n and sil[j]:
            j += 1
        run = j - i
        edge = i == 0 or j == n
        if edge:
            k = min(run, int(0.06 / 0.02))
            seg = pcm[i * fl:(i + k) * fl] if i == 0 else pcm[(j - k) * fl:j * fl]
        elif run > min_f:
            h = max(keep_f // 2, 1)
            seg = pcm[i * fl:(i + h) * fl] + pcm[(j - h) * fl:j * fl]
        else:
            seg = pcm[i * fl:j * fl]
        out.append(seg)
        i = j
    out.append(pcm[n * fl:])
    return b"".join(out)


def change_tempo(pcm, factor):
    open("t_in.raw", "wb").write(pcm)
    subprocess.run(
        ["ffmpeg", "-y", "-f", "s16le", "-ar", str(SR), "-ac", "1", "-i", "t_in.raw",
         "-filter:a", f"atempo={factor:.4f}", "-f", "s16le", "-ar", str(SR), "-ac", "1", "t_out.raw"],
        check=True, capture_output=True)
    return open("t_out.raw", "rb").read()


def split_block(pcm, texts):
    """এক block-এর audio-কে scene অনুযায়ী কাটে: অক্ষর-অনুপাতে আন্দাজ করে,
    তারপর DP দিয়ে আসল নীরবতার জায়গাগুলোর সাথে সবচেয়ে ভালো মিলিয়ে কাটে।"""
    if len(texts) == 1:
        return [pcm]
    r = frame_rms(pcm)
    thr = speech_thr(pcm)
    fl = int(SR * 0.02) * 2
    n = len(r)
    act = [i for i, v in enumerate(r) if v > thr]
    K = len(texts) - 1
    if not act:
        cuts = [n * (k + 1) // len(texts) for k in range(K)]
    else:
        a, b = act[0], act[-1] + 1
        runs, i = [], a
        while i < b:
            if r[i] <= thr:
                j = i
                while j < b and r[j] <= thr:
                    j += 1
                if j - i >= 3:
                    runs.append(((i + j) // 2, j - i))
                i = j
            else:
                i += 1
        chars = [speech_chars(t) for t in texts]
        total, cum, est = sum(chars), 0, []
        for c in chars[:-1]:
            cum += c
            est.append(a + (b - a) * cum / total)
        if len(runs) < K:
            cuts = [int(e) for e in est]
        else:
            M, INF = len(runs), 1e18
            cost = lambda k, m: abs(runs[m][0] - est[k]) / 25.0 - min(runs[m][1], 20) / 4.0
            dp = [[INF] * M for _ in range(K)]
            par = [[-1] * M for _ in range(K)]
            for m in range(M):
                dp[0][m] = cost(0, m)
            for k in range(1, K):
                for m in range(M):
                    for p in range(m):
                        v = dp[k - 1][p] + cost(k, m)
                        if v < dp[k][m]:
                            dp[k][m], par[k][m] = v, p
            m = min(range(M), key=lambda x: dp[K - 1][x])
            cuts = []
            for k in range(K - 1, -1, -1):
                cuts.append(runs[m][0])
                m = par[k][m]
            cuts.reverse()
    bounds = [0] + [c * fl for c in cuts] + [len(pcm)]
    return [pcm[bounds[k]:bounds[k + 1]] for k in range(len(texts))]


class BotError(RuntimeError):
    """video কাজ এগোনো যাচ্ছে না (watch mode-এ ধরে আবার চেষ্টা হয়)।"""


def find_zips(files):
    return [f for f in files if f["name"].lower().endswith(".zip")
            and not f["name"].lower().startswith("done_")]


def d_rename(fid, new_name):
    drive.files().update(fileId=fid, body={"name": new_name}, fields="id").execute()


def probe_duration(path):
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                            "-of", "csv=p=0", path], capture_output=True, text=True, check=True)
        return float(r.stdout.strip())
    except Exception:
        return 0.0


def clip_audio_pcm(path):
    """clip-এর audio 24kHz mono PCM হিসেবে; audio না থাকলে b''।"""
    r = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-vn", "-ac", "1", "-ar", str(SR),
                        "-f", "s16le", "-"], capture_output=True)
    return r.stdout if r.returncode == 0 else b""


def dialogue_audio(inf):
    """clip-এ কথা (dialogue) থাকলে তার audio রিটার্ন করে, নইলে None।
    ফাইলের নামে 'dialogue' বা 'dlg' থাকলে জোর করে dialogue ধরা হয়।"""
    force = bool(re.search(r"dialog|dlg", inf["name"], re.I))
    pcm = clip_audio_pcm(inf["path"])
    if not pcm:
        return None
    fr = frame_rms(pcm)
    if not fr:
        return None
    loud = sorted(fr)[int(len(fr) * 0.9):]
    ref = statistics.median(loud) if loud else 0
    act = sum(1 for v in fr if v > max(200, 0.2 * ref)) * 0.02
    if (ref >= 400 and act >= DIALOGUE_MIN_SEC) or (force and ref >= 150):
        return pcm
    return None


def make_thumb(path, is_video, out):
    pre = ["-ss", "0.5"] if is_video else []
    subprocess.run(["ffmpeg", "-y", *pre, "-i", path, "-frames:v", "1",
                    "-vf", "scale=512:-2", "-q:v", "4", out],
                   check=True, capture_output=True)


def hungarian(cost):
    """cost: n x m matrix (n <= m). প্রতিটা row-কে আলাদা column দিয়ে মোট cost সবচেয়ে কম করে; column index-এর list ফেরত দেয়।"""
    n, m = len(cost), len(cost[0])
    INF = float("inf")
    u, v, p, way = [0.0] * (n + 1), [0.0] * (m + 1), [0] * (m + 1), [0] * (m + 1)
    for i in range(1, n + 1):
        p[0] = i
        j0 = 0
        minv, used = [INF] * (m + 1), [False] * (m + 1)
        while True:
            used[j0] = True
            i0, delta, j1 = p[j0], INF, 0
            for j in range(1, m + 1):
                if not used[j]:
                    cur = cost[i0 - 1][j - 1] - u[i0] - v[j]
                    if cur < minv[j]:
                        minv[j], way[j] = cur, j0
                    if minv[j] < delta:
                        delta, j1 = minv[j], j
            for j in range(m + 1):
                if used[j]:
                    u[p[j]] += delta
                    v[j] -= delta
                else:
                    minv[j] -= delta
            j0 = j1
            if p[j0] == 0:
                break
        while True:
            j1 = way[j0]
            p[j0] = p[j1]
            j0 = j1
            if j0 == 0:
                break
    res = [0] * n
    for j in range(1, m + 1):
        if p[j]:
            res[p[j] - 1] = j - 1
    return res


def ai_rank_images(batch, info, pool):
    """একটা batch-এর প্রতিটা ছবির জন্য সবচেয়ে মানানসই ৩টা scene (সেরা আগে)। ফেরত: {file_id: [scene,...]}; [] = thumbnail/না-মেলা; ব্যর্থ হলে key থাকে না।"""
    valid = {s["n"] for s in pool}
    desc = "\n".join(
        f"SCENE {s['n']}: {(s.get('shot') or s['image_prompt'])[:200]} | narration: {s['narration'][:60]}"
        for s in pool)
    ask = (
        "নিচে একটা documentary-র scene তালিকা (প্রতিটার shot বর্ণনা সহ) আছে। তারপর কিছু ছবি "
        "(IMAGE 1, 2, ...) দেওয়া আছে, যেগুলো ওই prompt থেকেই AI দিয়ে বানানো। প্রতিটা ছবির জন্য সবচেয়ে "
        "মানানসই ৩টা scene নম্বর দাও, সেরাটা আগে। প্রতিটা ছবি সাধারণত আলাদা একটা scene-এর জন্য। "
        "ছবিগুলো মোটামুটি scene-এর ক্রমেই বানানো হয়, তাই সমান মিললে ক্রমকে হালকা ইঙ্গিত ধরো। "
        "ছবিটা যদি YouTube thumbnail-এর মতো হয় (বড় text/title সহ) বা কোনো scene-এর সাথেই না মেলে, "
        'scenes = [] দাও। শুধু JSON array দাও: [{"image": 1, "scenes": [5, 6, 2]}, ...]\n\n' + desc)
    parts = [ask]
    os.makedirs("thumbs", exist_ok=True)
    for k, f in enumerate(batch, 1):
        inf = info[f["id"]]
        tp = f"thumbs/{f['id'][:8]}.jpg"
        make_thumb(inf["path"], inf["video"], tp)
        parts += [f"IMAGE {k}:", types.Part.from_bytes(data=open(tp, "rb").read(), mime_type="image/jpeg")]
    got = {}
    for attempt in range(3):
        try:
            data = with_models(TEXT_MODELS, lambda c, m: gen_text(c, m, parts, json_mode=True))
            got = {}
            for x in parse_json(data):
                lst = x.get("scenes")
                if lst is None:
                    lst = [x.get("scene")] if x.get("scene") else []
                got[int(x["image"])] = [int(t) for t in lst if str(t).lstrip("-").isdigit() and int(t) in valid][:3]
            if all(k in got for k in range(1, len(batch) + 1)):
                break
        except AllGeminiKeysExhausted:
            raise
        except Exception as e:
            print("মেলানোতে সমস্যা, আবার চেষ্টা:", str(e)[:100])
    return {f["id"]: got[k] for k, f in enumerate(batch, 1) if k in got}


def match_with_ai(todo, media, info, scenes, mapping):
    """ছবির নাম নম্বর না হলে AI দিয়ে মেলায়, কিন্তু প্রতিটা scene একটাই ছবি পায় (এক scene-এ ৩টা ছবি আর ৯টা scene ফাঁকা - এমন হয় না)।"""
    n_scenes = len(scenes)
    taken = set(mapping.values())
    free = [n for n in range(1, n_scenes + 1) if n not in taken]
    sc_by_n = {s["n"]: s for s in scenes}
    pending = list(todo)
    pos = {f["id"]: i for i, f in enumerate(media)}
    print(f"AI দিয়ে মেলানো হচ্ছে: {len(pending)}টা ছবি, ফাঁকা scene {len(free)}টা (এক scene = এক ছবি)")
    first_ranks, sure, guessed = {}, 0, 0
    B = 8
    for rnd in range(3):
        if not pending or not free:
            break
        pool = [sc_by_n[n] for n in free]
        ranks = {}
        for i in range(0, len(pending), B):
            ranks.update(ai_rank_images(pending[i:i + B], info, pool))
            time.sleep(2)
        if rnd == 0:
            first_ranks = dict(ranks)
            surplus = len(pending) - len(free)       # ছবি বেশি থাকলে শুধু ততটা "thumbnail/বাদ" ধরা হবে
            for f in [f for f in pending if ranks.get(f["id"]) == []][:max(surplus, 0)]:
                mapping[f["id"]] = 0
                print("  বাদ (thumbnail/মেলেনি):", info[f["id"]]["name"])
                pending.remove(f)
            if not pending:
                break
        cols = [(n, 0.0) for n in free]
        if len(pending) > len(free):                  # একই scene-এর একাধিক take থাকতে পারে
            cols += [(n, 3.0) for n in free]
        cost = []
        for i, f in enumerate(pending):
            r = ranks.get(f["id"]) or []
            row = []
            for j, (n, extra) in enumerate(cols):
                c = float(r.index(n)) if n in r else 4.0
                row.append(c + extra + 0.001 * abs(i - j))     # সমান হলে ক্রম অনুযায়ী
            cost.append(row)
        res = hungarian(cost)
        last = (rnd == 2)
        still = []
        for i, f in enumerate(pending):
            n, extra = cols[res[i]]
            c = cost[i][res[i]]
            if c < 2.5 or last:
                mapping[f["id"]] = n
                if c < 2.5:
                    sure += 1
                else:
                    guessed += 1
                if n in free:
                    free.remove(n)
            else:
                still.append(f)
        if len(still) == len(pending):                # কোনো অগ্রগতি নেই, বাকিগুলো ক্রমে বসাও
            for i, f in enumerate(pending):
                n, extra = cols[res[i]]
                mapping[f["id"]] = n
                guessed += 1
                if n in free:
                    free.remove(n)
            still = []
        pending = still
    for f in pending:                                 # scene ফাঁকা নেই (বাড়তি ছবি): সবচেয়ে মানানসই scene-এর extra take
        r = first_ranks.get(f["id"]) or []
        mapping[f["id"]] = r[0] if r else 0
    print(f"মেলানো শেষ: নিশ্চিত {sure}টা, আন্দাজে {guessed}টা")


def match_media(files, zips, scenes):
    """ছবি/clip নামিয়ে AI দিয়ে scene-এর সাথে মেলায়।"""
    n_scenes = len(scenes)

    def ok_name(nm):
        low = nm.lower()
        return low.endswith(IMG_EXT + VID_EXT) and not low.startswith(("thumb", "final", "bgm", "."))

    def nat(nm):
        return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", nm)]

    os.makedirs("media", exist_ok=True)
    media = []   # শুধু zip-এর ভেতরের ফাইল ব্যবহার হয়
    info = {}
    for f in media:
        safe = re.sub(r"[^\w.]+", "_", f["name"])
        p = os.path.join("media", f"{f['id'][:8]}_{safe}")
        d_download(f["id"], p)
        info[f["id"]] = {"name": f["name"], "path": p,
                         "video": f["name"].lower().endswith(VID_EXT)}

    # ---- zip ফাইল ----
    seen_names = {f["name"] for f in media}
    for z in zips:
        zp = f"media_{z['id'][:8]}.zip"
        d_download(z["id"], zp)
        added = 0
        with zipfile.ZipFile(zp) as zf:
            entries = sorted((e for e in zf.infolist() if not e.is_dir()
                              and "__MACOSX" not in e.filename
                              and ok_name(os.path.basename(e.filename))),
                             key=lambda e: nat(e.filename))
            for k, e in enumerate(entries):
                base = os.path.basename(e.filename)
                nm = base
                if nm in seen_names:
                    stem, ext = os.path.splitext(base)
                    nm = f"{stem}_{z['id'][:4]}{k}{ext}"
                seen_names.add(nm)
                fid = f"{z['id'][:8]}z{k:05d}"
                p = os.path.join("media", fid + "_" + re.sub(r"[^A-Za-z0-9.]+", "_", nm))
                with zf.open(e) as src, open(p, "wb") as dst:
                    dst.write(src.read())
                media.append({"id": fid, "name": nm, "createdTime": z["createdTime"]})
                info[fid] = {"name": nm, "path": p, "video": nm.lower().endswith(VID_EXT)}
                added += 1
        print(f"zip খোলা হলো: {z['name']} -> {added}টা ছবি/clip")
        os.remove(zp)

    if not media:
        raise BotError("zip-এর ভেতরে কোনো ছবি/video পাওয়া যায়নি")
    media.sort(key=lambda x: (x["createdTime"], nat(x["name"])))
    print("মিডিয়া download:", len(media), "টা ফাইল")

    old, old_id = {}, next((x["id"] for x in files if x["name"] == "image_map.json"), None)
    if old_id:
        d_download(old_id, "image_map_old.json")
        old = json.load(open("image_map_old.json", encoding="utf-8"))

    mapping, todo = {}, []
    for f in media:
        nm = f["name"]
        m = NUM_RE.match(os.path.splitext(nm)[0])
        if nm in old:
            mapping[f["id"]] = int(old[nm])
        elif m and 1 <= int(m.group(1)) <= n_scenes:
            mapping[f["id"]] = int(m.group(1))
        else:
            todo.append(f)

    if todo:
        match_with_ai(todo, media, info, scenes, mapping)

    out_map = {info[f["id"]]["name"]: mapping[f["id"]] for f in media}
    for nm, sc in out_map.items():
        print(f"  {nm}  ->  scene {sc if sc else '(বাদ)'}")
    json.dump(out_map, open("image_map.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    return media, info, mapping, old_id


def build_groups(media, info, mapping, n_scenes):
    groups = {}
    for f in media:
        sc = mapping[f["id"]]
        if sc >= 1:
            groups.setdefault(sc, []).append(info[f["id"]])
    if not groups:
        raise BotError("কোনো ছবি কোনো scene-এর সাথে মেলেনি")
    result, last = [], groups[min(groups)]
    missing = 0
    for n in range(1, n_scenes + 1):
        if n in groups:
            last = groups[n]
        else:
            missing += 1
        result.append(last)
    if missing:
        print(f"সতর্কতা: {missing}টা scene-এ আলাদা ছবি নেই, আগের ছবিটাই চলবে")
    return result, set(groups)


# ---------------------------------------------------------------------
# Video clip: Ken Burns (4 রকম move) — high quality encode
# ---------------------------------------------------------------------
ENC = ["-c:v", "libx264", "-preset", "faster", "-crf", "16", "-profile:v", "high",
       "-pix_fmt", "yuv420p", "-r", str(FPS)]


def make_clip(src, is_video, frames, out, variant, fade_in=False, fade_out=False, src_dur=None):
    dur = frames / FPS
    fx = []
    if fade_in:
        fx.append("fade=t=in:st=0:d=0.8")
    if fade_out:
        fx.append(f"fade=t=out:st={max(dur - 1.5, 0):.2f}:d=1.5")
    fxs = ("," + ",".join(fx)) if fx else ""
    if is_video:
        # clip ছোট হলে সর্বোচ্চ 1.25x ধীর করে, তারপরও কম পড়লে শেষ frame ধরে রাখে; বড় হলে ছেঁটে দেয়
        pre, eff = "", float(src_dur or 0.0)
        if src_dur and src_dur < dur:
            f = min(1.25, dur / src_dur)
            pre, eff = f"setpts={f:.4f}*PTS,", src_dur * f
        hold = (max(dur - eff, 0) + 1.0) if src_dur else dur + 1.0
        vf = (f"{pre}scale={W}:{H}:flags=lanczos:force_original_aspect_ratio=increase,"
              f"crop={W}:{H},fps={FPS},setsar=1,tpad=stop_mode=clone:stop_duration={hold:.2f}{fxs}")
        cmd = ["ffmpeg", "-y", "-i", src, "-vf", vf,
               "-frames:v", str(frames), "-an", *ENC, out]
    else:
        v = variant % 4
        px = "iw/2-(iw/zoom/2)"
        py = "ih/2-(ih/zoom/2)"
        if v == 0:      # slow zoom in
            z, x, y = f"1+0.12*on/{frames}", px, py
        elif v == 1:    # slow zoom out
            z, x, y = f"1.12-0.12*on/{frames}", px, py
        elif v == 2:    # pan বাঁ → ডান
            z, x, y = "1.10", f"(iw-iw/zoom)*on/{frames}", py
        else:           # pan ডান → বাঁ
            z, x, y = "1.10", f"(iw-iw/zoom)*(1-on/{frames})", py
        vf = ("scale=3200:1800:flags=lanczos:force_original_aspect_ratio=increase,crop=3200:1800,setsar=1,"
              f"zoompan=z='{z}':x='{x}':y='{y}':d={frames}:s={W}x{H}:fps={FPS}{fxs}")
        cmd = ["ffmpeg", "-y", "-i", src, "-vf", vf, "-frames:v", str(frames), "-an", *ENC, out]
    subprocess.run(cmd, check=True, capture_output=True)


def trans_frames(min_nom, n_clips):
    """crossfade কত frame (জোড় সংখ্যা)। clip খুব ছোট হলে কমিয়ে দেয়, 0 মানে transition নেই।"""
    if TRANSITION_SEC <= 0 or n_clips < 2:
        return 0
    tf = 2 * round(TRANSITION_SEC * FPS / 2)
    tf = min(tf, 2 * ((min_nom - 2) // 2))
    return tf if tf >= 4 else 0


def make_clip_parts(src, is_video, nom, base, variant, first, last, TF, src_dur=None):
    """একটা clip বানায় আর তিন ভাগে ভাগ করে: head (আগের scene-এর সাথে মিশবে), mid, tail (পরের scene-এর সাথে মিশবে)।
    শুধু head/tail অংশ আবার encode হয়, তাই পুরো video আবার render করতে হয় না।
    clip-এর আসল বিষয়বস্তু scene শুরুর সঠিক মুহূর্ত থেকেই চলে (lip-sync ঠিক থাকে)।"""
    hf = 0 if first else TF // 2
    tl = 0 if last else TF // 2
    L = nom + hf + tl
    dur = L / FPS
    fx = []
    if first:
        fx.append("fade=t=in:st=0:d=0.8")
    if last:
        fx.append(f"fade=t=out:st={max(dur - 1.5, 0):.2f}:d=1.5")
    fxs = (",".join([""] + fx)) if fx else ""
    if is_video:
        d_nom = nom / FPS
        pre, eff = "", float(src_dur or 0.0)
        if src_dur and src_dur < d_nom:
            f = min(1.25, d_nom / src_dur)
            pre, eff = f"setpts={f:.4f}*PTS,", src_dur * f
        hold = (max(dur - hf / FPS - eff, 0) + 1.0) if src_dur else dur + 1.0
        tp = (f"tpad=start_mode=clone:start_duration={hf / FPS:.3f}:stop_mode=clone:stop_duration={hold:.2f}"
              if hf else f"tpad=stop_mode=clone:stop_duration={hold:.2f}")
        vf = (f"{pre}scale={W}:{H}:flags=lanczos:force_original_aspect_ratio=increase,"
              f"crop={W}:{H},fps={FPS},setsar=1,{tp}{fxs}")
    else:
        v = variant % 4
        px, py = "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"
        if v == 0:
            z, x, y = f"1+0.12*on/{L}", px, py
        elif v == 1:
            z, x, y = f"1.12-0.12*on/{L}", px, py
        elif v == 2:
            z, x, y = "1.10", f"(iw-iw/zoom)*on/{L}", py
        else:
            z, x, y = "1.10", f"(iw-iw/zoom)*(1-on/{L})", py
        vf = ("scale=3200:1800:flags=lanczos:force_original_aspect_ratio=increase,crop=3200:1800,setsar=1,"
              f"zoompan=z='{z}':x='{x}':y='{y}':d={L}:s={W}x{H}:fps={FPS}{fxs}")
    cuts = []
    if not first:
        cuts.append(("head", 0, TF))
    cuts.append(("mid", 0 if first else TF, L if last else L - TF))
    if not last:
        cuts.append(("tail", L - TF, L))
    if len(cuts) == 1:
        fc = f"[0:v]{vf}[o0]"
    else:
        fc = (f"[0:v]{vf},split={len(cuts)}" + "".join(f"[s{k}]" for k in range(len(cuts))) + ";"
              + ";".join(f"[s{k}]trim=start_frame={a}:end_frame={b},setpts=PTS-STARTPTS[o{k}]"
                         for k, (_, a, b) in enumerate(cuts)))
    cmd = ["ffmpeg", "-y", "-i", src, "-filter_complex", fc]
    parts = {}
    for k, (name, a, b) in enumerate(cuts):
        path = f"{base}_{name}.mp4"
        cmd += ["-map", f"[o{k}]", "-frames:v", str(b - a), "-an", *ENC, path]
        parts[name] = path
    subprocess.run(cmd, check=True, capture_output=True)
    return parts


# ---------------------------------------------------------------------
# Sound library: Drive-এ রাখা লম্বা mp3 "pack" থেকে ছোট ছোট effect নিজে কেটে, AI দিয়ে চিনে, scene-এ বসানো
#   • pack ফাইল: Drive output folder-এ 'pack*.mp3' (যেমন pack_1.mp3, pack_transition.mp3) অথবা 'Sounds' folder-এর ভেতরের যেকোনো audio
#   • প্রথমবার: নীরবতা দেখে কাটে → Gemini শুনে tag/kind/style বসায় → 'sfxlib_index.json' হিসেবে Drive-এ জমা (পরের বার আর AI লাগে না)
#   • effect-এর সবচেয়ে জোরালো মুহূর্ত (peak) transition-এর ঠিক মাঝখানে বসে, তাই শব্দ আর ছবি একসাথে "ধাক্কা" দেয়
# ---------------------------------------------------------------------
LIB_ON = os.getenv("SOUND_LIB", "1") == "1"
LIB_DIR = "soundlib"
LIB_INDEX_NAME = "sfxlib_index.json"
LIB_FOLDERS = ("sounds", "sound", "sfx")
LIB_SIL_DB = [x.strip() for x in os.getenv("LIB_SILENCE_DB", "-38,-32,-26,-20").split(",") if x.strip()]
LIB_MIN_SEC = float(os.getenv("LIB_MIN_SEC", "0.15"))
LIB_MERGE_GAP = float(os.getenv("LIB_MERGE_GAP", "0.18"))
LIB_MAX_SEC = float(os.getenv("LIB_MAX_SEC", "15"))
LIB_KINDS = ("transition", "impact", "ambient", "scene", "music")
SFX_TAGS = ("whoosh", "swish", "flash", "impact", "boom", "riser", "glitch", "zoom", "blur", "shutter",
            "heartbeat", "wind", "rain", "thunder", "crowd", "footsteps", "door", "bell", "clock", "gunshot",
            "explosion", "fire", "siren", "static", "animal", "stinger", "other")
TRANS_STYLES = ("fade", "fadeblack", "flash", "zoom", "blur", "glitch", "whip", "dissolve")
# প্রতিটা kind কত জোরে বাজবে (সব effect আগেই peak -3 dB-এ আনা হয়, তাই এটাই আপেক্ষিক volume)
LIB_GAIN = {"transition": None, "impact": 0.9, "scene": 0.85, "ambient": 0.45, "music": 0.7}
_STOP = {"the", "and", "with", "sound", "sfx", "effect", "loud", "soft", "distant", "short", "long", "single", "heavy"}


def _words(s):
    return {w for w in re.findall(r"[a-z]+", str(s).lower()) if len(w) > 2 and w not in _STOP}


def lib_pack_files(files):
    out = []
    for f in files:
        n = f["name"].lower()
        if f["mimeType"] != FOLDER_MIME and n.endswith(AUD_EXT) and n.startswith("pack"):
            out.append(f)
    for fo in files:
        if fo["mimeType"] == FOLDER_MIME and fo["name"].lower() in LIB_FOLDERS:
            try:
                for f in d_list(fo["id"]):
                    n = f["name"].lower()
                    if f["mimeType"] != FOLDER_MIME and n.endswith(AUD_EXT) and not n.startswith("bgm"):
                        out.append(f)
            except Exception as e:
                print("Sounds folder পড়া যায়নি:", str(e)[:100])
    return out


def _silences(path, db, total):
    r = subprocess.run(["ffmpeg", "-hide_banner", "-i", path, "-af", f"silencedetect=noise={db}dB:d=0.12",
                        "-f", "null", "-"], capture_output=True, text=True)
    sil, st = [], None
    for line in r.stderr.splitlines():
        m = re.search(r"silence_start:\s*(-?[\d.]+)", line)
        if m:
            st = max(float(m.group(1)), 0.0)
        m = re.search(r"silence_end:\s*([\d.]+)", line)
        if m and st is not None:
            sil.append((st, float(m.group(1))))
            st = None
    if st is not None:
        sil.append((st, total))
    segs, cur = [], 0.0
    for a, b in sil:
        if a - cur >= LIB_MIN_SEC:
            segs.append([cur, a])
        cur = b
    if total - cur >= LIB_MIN_SEC:
        segs.append([cur, total])
    merged = []
    for s in segs:                          # খুব কাছের টুকরো জোড়া লাগে, যাতে echo/reverb-এর লেজ কেটে না যায়
        if merged and s[0] - merged[-1][1] < LIB_MERGE_GAP:
            merged[-1][1] = s[1]
        else:
            merged.append(s)
    out = []
    for a, b in merged:
        a, b = max(a - 0.03, 0.0), min(b + 0.12, total)
        if b - a > LIB_MAX_SEC:
            b = a + LIB_MAX_SEC
        out.append((round(a, 3), round(b, 3)))
    return out


def lib_segments(path):
    """লম্বা ফাইলে effect-গুলোর (start, end) বের করে। নীরবতা কম থাকলে ধাপে ধাপে threshold বাড়িয়ে দেখে।"""
    total = probe_duration(path)
    best = []
    for db in LIB_SIL_DB:
        segs = _silences(path, db, total)
        if len(segs) > len(best):
            best = segs
        if len(segs) >= max(3, total / 25):
            break
    return best or [(0.0, min(total, LIB_MAX_SEC))]


def _peak_time(path):
    r = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-f", "s16le", "-ac", "1", "-ar", "8000", "-"],
                       capture_output=True)
    raw, win = r.stdout, 160                  # 20ms window
    best, bt = 0, 0.0
    for k in range(0, max(len(raw) - win * 2, 0), win * 2):
        v = audioop.rms(raw[k:k + win * 2], 2)
        if v > best:
            best, bt = v, k / 2 / 8000
    return bt


def lib_cut(pack, a, b, out):
    """pack থেকে [a,b] কেটে 24-bit wav বানায়: click এড়াতে খুব ছোট fade, আর peak -3 dB-এ normalize। quality কমে না।"""
    d = b - a
    raw = out + ".raw.wav"
    subprocess.run(["ffmpeg", "-y", "-ss", f"{a:.3f}", "-t", f"{d:.3f}", "-i", pack, "-af",
                    f"afade=t=in:d=0.008,afade=t=out:st={max(d - 0.05, 0):.3f}:d=0.05",
                    "-ar", "44100", "-ac", "2", "-c:a", "pcm_s24le", raw], check=True, capture_output=True)
    r = subprocess.run(["ffmpeg", "-i", raw, "-af", "volumedetect", "-f", "null", "-"], capture_output=True, text=True)
    mv = re.search(r"max_volume:\s*(-?[\d.]+) dB", r.stderr)
    gain = (-3.0 - float(mv.group(1))) if mv else 0.0
    gain = max(min(gain, 30.0), -30.0)
    subprocess.run(["ffmpeg", "-y", "-i", raw, "-af", f"volume={gain:.2f}dB", "-c:a", "pcm_s24le", out],
                   check=True, capture_output=True)
    os.remove(raw)


def lib_file(seg):
    """segment-কে (দরকার হলে) ফাইল বানিয়ে (path, peak_সেকেন্ড) দেয়।"""
    if "path" not in seg:
        os.makedirs(LIB_DIR, exist_ok=True)
        p = f"{LIB_DIR}/s{seg['id']:04d}.wav"
        lib_cut(seg["pack"], seg["a"], seg["b"], p)
        seg["path"], seg["peak"] = p, _peak_time(p)
    return seg["path"], seg["peak"]


def _guess_kind(dur):
    if dur <= 2.6:
        return {"tag": "whoosh", "kind": "transition", "style": "whip", "desc": "short sweep"}
    if dur >= 7:
        return {"tag": "other", "kind": "ambient", "style": "", "desc": "long ambience"}
    return {"tag": "other", "kind": "scene", "style": "", "desc": "sound"}


def lib_classify(segs_wav):
    """segs_wav: [(i, wavpath, dur)] → {i: {tag, kind, style, desc}}। Gemini শুনে চেনে। ব্যর্থ হলে None।"""
    res = {}
    for k in range(0, len(segs_wav), 10):
        batch = segs_wav[k:k + 10]
        parts = []
        for n, (_, p, d) in enumerate(batch, 1):
            mp3 = p + ".c.mp3"
            subprocess.run(["ffmpeg", "-y", "-i", p, "-t", "10", "-ac", "1", "-ar", "22050", "-b:a", "64k", mp3],
                           check=True, capture_output=True)
            parts.append(f"CLIP {n} (duration {d:.1f}s):")
            parts.append(types.Part.from_bytes(data=open(mp3, "rb").read(), mime_type="audio/mpeg"))
        parts.append(
            "You label short sound-effect clips for a documentary video editor. Listen to each CLIP and return ONLY a JSON array, "
            'one object per clip: {"n": 1, "tag": "...", "kind": "...", "style": "...", "desc": "..."}.\n'
            f"tag must be one of: {', '.join(SFX_TAGS)}.\n"
            "kind: transition = whoosh/swish/flash/glitch/zoom-type sound used when the scene changes (no real-world source); "
            "impact = hit/boom/stinger/riser/heartbeat for drama; ambient = continuous background bed (wind, rain, crowd, night, fire); "
            "scene = a real-world event sound (door, bell, footsteps, gunshot, animal, clock...); music = a musical sting.\n"
            f"style (only for kind=transition, else empty string): the visual transition it fits best, one of: {', '.join(TRANS_STYLES)} "
            "(flash = bright camera-flash/pop, zoom = rushing push-in, blur = soft airy, glitch = digital/static, whip = fast swish, "
            "fadeblack = deep low swell, fade = gentle soft, dissolve = shimmering).\n"
            "desc: max 8 English words describing exactly what you hear.")
        data = with_models(TEXT_MODELS, lambda c, m: gen_text(c, m, parts, json_mode=True))
        for x in parse_json(data):
            try:
                n = int(x["n"])
                i = batch[n - 1][0]
            except Exception:
                continue
            tag = str(x.get("tag", "other")).strip().lower()
            kind = str(x.get("kind", "scene")).strip().lower()
            style = str(x.get("style", "")).strip().lower()
            res[i] = {"tag": tag if tag in SFX_TAGS else "other",
                      "kind": kind if kind in LIB_KINDS else "scene",
                      "style": style if style in TRANS_STYLES and kind == "transition" else "",
                      "desc": str(x.get("desc", "")).strip()[:80]}
    return res


def lib_load(files, cut=True):
    """Drive-এর pack ফাইল → [{id, pack, a, b, dur, tag, kind, style, desc}]। কাজ না থাকলে খালি list।"""
    if not LIB_ON:
        return []
    packs = lib_pack_files(files)
    if not packs:
        return []
    cache, idx_f = {}, next((f for f in files if f["name"] == LIB_INDEX_NAME and f["mimeType"] != FOLDER_MIME), None)
    if idx_f:
        try:
            d_download(idx_f["id"], LIB_INDEX_NAME)
            cache = json.load(open(LIB_INDEX_NAME, encoding="utf-8"))
        except Exception as e:
            print("sfxlib_index পড়া যায়নি, নতুন করে:", str(e)[:80])
    lib, new_cache, dirty = [], {}, False
    for pi, pf in enumerate(packs):
        key = f"{pf['id']}:{pf.get('size', '')}"
        local = f"pack_{pi}" + os.path.splitext(pf["name"])[1].lower()
        d_download(pf["id"], local)
        entry = cache.get(key)
        if entry:
            rows = entry["segs"]
        else:
            print(f"Sound pack নতুন: {pf['name']} → effect কাটা হচ্ছে")
            spans = lib_segments(local)
            os.makedirs(LIB_DIR, exist_ok=True)
            tmp = []
            for si, (a, b) in enumerate(spans):
                p = f"{LIB_DIR}/tmp_{pi}_{si}.wav"
                lib_cut(local, a, b, p)
                tmp.append((si, p, b - a))
            try:
                lab = lib_classify(tmp)
                ai = True
            except Exception as e:                       # quota/অন্য সমস্যা: এবারের মতো আন্দাজে চলে, জমা হয় না
                print("AI দিয়ে sound চেনা যায়নি, আন্দাজে চলছে:", type(e).__name__, str(e)[:100])
                lab, ai = {}, False
            rows = []
            for si, (a, b) in enumerate(spans):
                g = lab.get(si) or _guess_kind(b - a)
                rows.append({"a": a, "b": b, **g})
            for _, p, _ in tmp:
                for q in (p, p + ".c.mp3"):
                    if os.path.exists(q):
                        os.remove(q)
            if ai:
                dirty = True
                new_cache[key] = {"name": pf["name"], "segs": rows}
            kinds = {}
            for r in rows:
                kinds[r["kind"]] = kinds.get(r["kind"], 0) + 1
            print(f"  {len(rows)}টা effect পাওয়া গেছে:", ", ".join(f"{k}={v}" for k, v in kinds.items()),
                  "" if ai else "(AI ছাড়া)")
        if entry:
            new_cache[key] = entry
        for r in rows:
            lib.append({"id": len(lib), "pack": local, "a": r["a"], "b": r["b"], "dur": r["b"] - r["a"],
                        "tag": r.get("tag", "other"), "kind": r.get("kind", "scene"),
                        "style": r.get("style", ""), "desc": r.get("desc", "")})
    if dirty or (cache and set(cache) != set(new_cache)):
        try:
            json.dump(new_cache, open(LIB_INDEX_NAME, "w", encoding="utf-8"), ensure_ascii=False)
            if idx_f:
                d_delete(idx_f["id"])
            d_upload(LIB_INDEX_NAME, LIB_INDEX_NAME, OUT_ID, "application/json")
        except Exception as e:
            print("sfxlib_index জমা রাখা যায়নি:", str(e)[:100])
    print(f"Sound library: {len(lib)}টা effect ({len(packs)}টা pack)")
    return lib


class SoundPicker:
    """scene-এর বর্ণনা/transition-এর ধরন দেখে library থেকে সবচেয়ে মানানসই effect বাছে; একই effect বারবার নয়।"""
    def __init__(self, lib):
        self.lib, self.used = lib, {}

    def pick(self, tags=(), kinds=None, style="", desc="", need=3.0):
        dl = str(desc).lower()
        dw = _words(desc)
        best, bs = None, -1e9
        for s in self.lib:
            if kinds and s["kind"] not in kinds:
                continue
            base = 0.0
            if s["tag"] in tags:
                base += 6
            elif s["tag"] != "other" and any(t and (t in s["tag"] or s["tag"] in t) for t in tags):
                base += 3
            if style and s["style"] == style:
                base += 5
            if s["tag"] != "other" and s["tag"] in dl:
                base += 4
            base += 1.5 * len(dw & _words(s["desc"] + " " + s["tag"]))
            if base < need:
                continue
            sc = base - 2.5 * self.used.get(s["id"], 0) + ((s["id"] * 7919) % 100) / 1000
            if sc > bs:
                best, bs = s, sc
        if best:
            self.used[best["id"]] = self.used.get(best["id"], 0) + 1
        return best


def lib_item(seg, anchor, vol, align_peak=True, bed=False, dur=None):
    """effect-কে এমনভাবে বসায় যাতে তার peak ঠিক anchor সময়ে পড়ে। শুরুর আগে পড়লে সামনের অংশ ছেঁটে দেয়।"""
    path, pk = lib_file(seg)
    at = anchor - (pk if align_peak else 0.0)
    it = {"file": path, "vol": vol}
    if at < 0:
        it["ss"] = -at
        at = 0.0
    it["at"] = at
    if bed:
        it.update({"bed": True, "dur": dur, "fade_in": 0.4, "fade_out": 0.6})
    return it


# ---------------------------------------------------------------------
# Transition style → ffmpeg xfade (ছবির অংশ); শব্দ build_sfx-এ একই style দেখে বসে
# ---------------------------------------------------------------------
XF_MAP = {"fade": "fade", "fadeblack": "fadeblack", "flash": "fadewhite", "zoom": "zoomin",
          "blur": "hblur", "glitch": "pixelize", "whip": "slideleft", "dissolve": "dissolve"}


def make_xfade(a, b, TF, style, out):
    """a-র শেষ TF frame আর b-র প্রথম TF frame মিলিয়ে style অনুযায়ী transition বানায়।"""
    d = TF / FPS

    def run(st):
        kind = XF_MAP.get(st, st)
        post = f",eq=brightness='0.28*sin(PI*t/{d:.4f})':eval=frame" if st == "flash" else ""
        fc = (f"[0:v][1:v]xfade=transition={kind}:duration={d:.4f}:offset=0{post},"
              f"fps={FPS},format=yuv420p[v]")
        subprocess.run(["ffmpeg", "-y", "-i", a, "-i", b, "-filter_complex", fc, "-map", "[v]",
                        "-frames:v", str(TF), "-an", *ENC, out], check=True, capture_output=True)
    try:
        run(style)
    except subprocess.CalledProcessError:
        run("fade")                          # এই ffmpeg-এ style না থাকলে সাধারণ fade


# ---------------------------------------------------------------------
# Background music
# ---------------------------------------------------------------------
MOODS = {
    "cinematic": [(57, "m"), (53, "M"), (48, "M"), (55, "M")],
    "dark":      [(48, "m"), (44, "M"), (51, "M"), (46, "M")],
    "calm":      [(48, "M"), (55, "M"), (57, "m"), (53, "M")],
    "epic":      [(50, "m"), (46, "M"), (53, "M"), (48, "M")],
}
CHORD_S, OVL = 12.0, 3.0


def _mf(n):
    return 440.0 * 2 ** ((n - 69) / 12)


def synth_loop(mood, out):
    prog = MOODS.get(mood, MOODS["cinematic"])
    srcs, delays = [], []

    def add(expr, dur, at):
        srcs.append(f"aevalsrc='{expr}':s=44100:d={dur:.2f}")
        delays.append(int(at * 1000))

    total = CHORD_S * len(prog)
    for ci, (root, q) in enumerate(prog):
        at = ci * CHORD_S
        notes = [root, root + (3 if q == "m" else 4), root + 7, root + 12]
        D = CHORD_S + OVL
        env = f"min(1,t/{OVL})*min(1,({D}-t)/{OVL})"
        for n in notes:
            f = _mf(n)
            for det in (0.996, 1.004):
                add(f"0.05*sin(2*PI*{f * det:.3f}*t)*{env}", D, at)
            add(f"0.015*sin(2*PI*{f * 2:.3f}*t)*{env}", D, at)
        add(f"0.16*sin(2*PI*{_mf(root - 12):.3f}*t)*min(1,t/2)*min(1,({D}-t)/2)", D, at)
        add("0.55*sin(2*PI*(42+70*exp(-9*t))*t)*exp(-1.6*t)", 4.0, at)
        arp = [notes[0] + 12, notes[1] + 12, notes[2] + 12, notes[3] + 12, notes[2] + 12, notes[1] + 12]
        for k, n in enumerate(arp):
            tt = at + 1.5 + k * 1.8
            if tt + 3 > total + OVL:
                continue
            add(f"0.07*sin(2*PI*{_mf(n):.3f}*t)*exp(-1.8*t)*min(1,t/0.01)", 3.0, tt)

    cmd = ["ffmpeg", "-y"]
    for s in srcs:
        cmd += ["-f", "lavfi", "-i", s]
    parts, labels = [], []
    for i, dl in enumerate(delays):
        parts.append(f"[{i}:a]adelay={dl}|{dl},apad=whole_dur={total + OVL:.1f}[a{i}]")
        labels.append(f"[a{i}]")
    parts.append(
        "".join(labels) + f"amix=inputs={len(labels)}:normalize=0:duration=longest,"
        f"atrim=0:{total:.2f},lowpass=f=2200,highpass=f=30,"
        "aecho=0.8:0.9:900|1700:0.35|0.25,asplit[l][r];"
        "[r]adelay=22[r2];[l][r2]amerge=inputs=2,alimiter=limit=0.9[outa]")
    cmd += ["-filter_complex", ";".join(parts), "-map", "[outa]", "-ar", "44100", out]
    subprocess.run(cmd, check=True, capture_output=True)


# ---------------------------------------------------------------------
# Mood অনুযায়ী background music: bgm*.mp3 ফাইলে আলাদা আলাদা গান থাকলে নিজে কেটে (নীরবতা দেখে), AI দিয়ে mood চিনে,
# video-র অংশ (scene-এর "mood") অনুযায়ী মানানসই গান বসায়। অংশ বদলালে ৩ সেকেন্ডে crossfade।
# ---------------------------------------------------------------------
BGM_MOODS = ("calm", "emotional", "mysterious", "tense", "dark", "epic", "action", "uplifting")
BGM_NEAR = {"calm": ("emotional", "uplifting"), "emotional": ("calm", "mysterious"),
            "mysterious": ("dark", "tense"), "tense": ("dark", "action", "mysterious"),
            "dark": ("tense", "mysterious"), "epic": ("action", "uplifting", "tense"),
            "action": ("epic", "tense"), "uplifting": ("epic", "calm")}
BGM_INDEX_NAME = "bgmlib_index.json"
BGM_MIN_TRACK = float(os.getenv("BGM_MIN_TRACK", "25"))        # এর চেয়ে ছোট টুকরো আলাদা গান ধরা হয় না
BGM_SECTION_MIN = float(os.getenv("BGM_SECTION_MIN", "30"))    # এর চেয়ে ছোট অংশে গান বদলায় না
BGM_XFADE = 3.0


def bgm_segments(path):
    """bgm ফাইলে গানগুলোর (start, end)। গানের মাঝের নীরবতা দেখে কাটে; নীরবতা না থাকলে পুরোটা এক গান।"""
    total = probe_duration(path)
    for db in ("-45", "-40", "-35", "-30"):
        r = subprocess.run(["ffmpeg", "-hide_banner", "-i", path, "-af", f"silencedetect=noise={db}dB:d=0.8",
                            "-f", "null", "-"], capture_output=True, text=True)
        sil, st = [], None
        for line in r.stderr.splitlines():
            m = re.search(r"silence_start:\s*(-?[\d.]+)", line)
            if m:
                st = max(float(m.group(1)), 0.0)
            m = re.search(r"silence_end:\s*([\d.]+)", line)
            if m and st is not None:
                sil.append((st, float(m.group(1))))
                st = None
        segs, cur = [], 0.0
        for a, b in sil:
            if a - cur > 1.0:
                segs.append([cur, a])
            cur = b
        if total - cur > 1.0:
            segs.append([cur, total])
        out = []
        for s in segs:                       # ছোট টুকরো পাশের গানের সাথে জোড়া লাগে
            if out and (out[-1][1] - out[-1][0] < BGM_MIN_TRACK or s[1] - s[0] < BGM_MIN_TRACK):
                out[-1][1] = s[1]
            else:
                out.append(list(s))
        if len(out) >= 2:
            return [(round(a, 2), round(b, 2)) for a, b in out]
    return [(0.0, total)]


def bgm_classify(path, spans):
    """প্রতিটা গানের মাঝখান থেকে ৪০ সেকেন্ড Gemini-কে শুনিয়ে mood জানে। {i: {mood, desc}}"""
    res = {}
    for k in range(0, len(spans), 6):
        batch = list(enumerate(spans))[k:k + 6]
        parts = []
        for n, (i, (a, b)) in enumerate(batch, 1):
            mp3 = f"bgm_c{i}.mp3"
            ss = max(a, (a + b) / 2 - 20)
            subprocess.run(["ffmpeg", "-y", "-ss", f"{ss:.2f}", "-t", "40", "-i", path, "-ac", "1", "-ar", "22050",
                            "-b:a", "64k", mp3], check=True, capture_output=True)
            parts.append(f"TRACK {n}:")
            parts.append(types.Part.from_bytes(data=open(mp3, "rb").read(), mime_type="audio/mpeg"))
            os.remove(mp3)
        parts.append(
            "You pick background music for a documentary. Listen to each TRACK and return ONLY a JSON array, one object per track: "
            '{"n": 1, "mood": "...", "desc": "..."}.\n'
            f"mood must be one of: {', '.join(BGM_MOODS)} "
            "(calm = peaceful/ambient, emotional = sad/touching, mysterious = eerie/curious, tense = suspense/thriller, dark = ominous/heavy, "
            "epic = grand/heroic/orchestral, action = fast/driving, uplifting = hopeful/inspiring).\n"
            "desc: max 8 English words (instrument, feeling).")
        data = with_models(TEXT_MODELS, lambda c, m: gen_text(c, m, parts, json_mode=True))
        for x in parse_json(data):
            try:
                i = batch[int(x["n"]) - 1][0]
            except Exception:
                continue
            md = str(x.get("mood", "")).strip().lower()
            res[i] = {"mood": md if md in BGM_MOODS else "calm", "desc": str(x.get("desc", "")).strip()[:80]}
    return res


def bgm_load(files):
    """bgm*.mp3 ফাইল(গুলো) থেকে গান-তালিকা [{id, src, a, b, dur, mood, desc}]। ২টার কম গান বা AI ব্যর্থ হলে [] (পুরনো নিয়মে চলবে)।"""
    bfs = [f for f in files if f["mimeType"] != FOLDER_MIME and f["name"].lower().startswith("bgm")
           and f["name"].lower().endswith(AUD_EXT)]
    if not bfs:
        return []
    cache, idx_f = {}, next((f for f in files if f["name"] == BGM_INDEX_NAME and f["mimeType"] != FOLDER_MIME), None)
    if idx_f:
        try:
            d_download(idx_f["id"], BGM_INDEX_NAME)
            cache = json.load(open(BGM_INDEX_NAME, encoding="utf-8"))
        except Exception as e:
            print("bgmlib_index পড়া যায়নি, নতুন করে:", str(e)[:80])
    tracks, new_cache, dirty = [], {}, False
    for fi, bf in enumerate(bfs):
        key = f"{bf['id']}:{bf.get('size', '')}"
        src = f"bgm_src{fi}" + os.path.splitext(bf["name"])[1].lower()
        d_download(bf["id"], src)
        entry = cache.get(key)
        if entry:
            rows = entry["tracks"]
            new_cache[key] = entry
        else:
            spans = bgm_segments(src)
            print(f"bgm '{bf['name']}': {len(spans)}টা গান পাওয়া গেছে")
            try:
                lab = bgm_classify(src, spans) if len(spans) >= 2 else {}
                ai = len(spans) >= 2
            except Exception as e:
                print("bgm-র mood চেনা যায়নি:", type(e).__name__, str(e)[:100])
                lab, ai = {}, False
            rows = [{"a": a, "b": b, "mood": (lab.get(i) or {}).get("mood", ""), "desc": (lab.get(i) or {}).get("desc", "")}
                    for i, (a, b) in enumerate(spans)]
            if ai:
                dirty = True
                new_cache[key] = {"name": bf["name"], "tracks": rows}
        for r in rows:
            tracks.append({"id": len(tracks), "src": src, "a": r["a"], "b": r["b"], "dur": r["b"] - r["a"],
                           "mood": r.get("mood", ""), "desc": r.get("desc", "")})
    if dirty or (cache and set(cache) != set(new_cache)):
        try:
            json.dump(new_cache, open(BGM_INDEX_NAME, "w", encoding="utf-8"), ensure_ascii=False)
            if idx_f:
                d_delete(idx_f["id"])
            d_upload(BGM_INDEX_NAME, BGM_INDEX_NAME, OUT_ID, "application/json")
        except Exception as e:
            print("bgmlib_index জমা রাখা যায়নি:", str(e)[:100])
    if len(tracks) < 2 or any(not t["mood"] for t in tracks):
        return []
    print("bgm moods:", ", ".join(f"{t['mood']}({t['dur']:.0f}s)" for t in tracks))
    return tracks


def bgm_sections(scenes, scene_audio):
    """scene-এর "mood" থেকে video-র অংশ [start, end, mood]। কোনো scene-এ mood না থাকলে None।"""
    n = min(len(scenes), len(scene_audio))
    starts, durs, t = [], [], 0.0
    for seg in scene_audio[:n]:
        starts.append(t)
        durs.append(len(seg) / (2 * SR))
        t += durs[-1]
    moods = []
    for s in scenes[:n]:
        m = str(s.get("mood") or "").strip().lower()
        moods.append(m if m in BGM_MOODS else "")
    first = next((m for m in moods if m), "")
    if not first:
        return None
    last = first
    for i in range(n):                         # ফাঁকা mood আগেরটাই ধরে
        moods[i] = moods[i] or last
        last = moods[i]
    secs = []
    for i in range(n):
        if secs and secs[-1][2] == moods[i]:
            secs[-1][1] = starts[i] + durs[i]
        else:
            secs.append([starts[i], starts[i] + durs[i], moods[i]])
    while len(secs) > 1:                       # খুব ছোট অংশে গান বদলায় না: পাশের অংশে মিশে যায়
        k = next((j for j, s in enumerate(secs) if s[1] - s[0] < BGM_SECTION_MIN), None)
        if k is None:
            break
        if k > 0:
            secs[k - 1][1] = secs[k][1]
        else:
            secs[1][0] = secs[0][0]
        del secs[k]
    return secs


def bgm_prepare(t):
    """গানটা কেটে loudness সমান করে (সব গান একই জোরে শুরু হয়) wav বানায়।"""
    p = f"bgm_t{t['id']:03d}.wav"
    if not os.path.exists(p):
        subprocess.run(["ffmpeg", "-y", "-ss", f"{t['a']:.2f}", "-t", f"{t['dur']:.2f}", "-i", t["src"], "-af",
                        "loudnorm=I=-18:TP=-2:LRA=11", "-ar", "44100", "-ac", "2", "-c:a", "pcm_s24le", p],
                       check=True, capture_output=True)
    return p


def make_bgm_mood(dur, tracks, secs):
    items, used, last_id = [], {}, None
    for k, (s, e, m) in enumerate(secs):
        cands = ([t for t in tracks if t["mood"] == m]
                 or [t for nm in BGM_NEAR.get(m, ()) for t in tracks if t["mood"] == nm] or tracks)
        t = min(cands, key=lambda x: (used.get(x["id"], 0), x["id"] == last_id, x["id"]))
        used[t["id"]] = used.get(t["id"], 0) + 1
        last_id = t["id"]
        at = 0.0 if k == 0 else max(s - BGM_XFADE / 2, 0.0)
        end = dur if k == len(secs) - 1 else e + BGM_XFADE / 2
        it = {"file": bgm_prepare(t), "at": at, "vol": 1.0, "bed": True, "dur": max(end - at, 1.0)}
        if k > 0:
            it["fade_in"] = BGM_XFADE
        if k < len(secs) - 1:
            it["fade_out"] = BGM_XFADE
        items.append(it)
        print(f"  bgm {s:.0f}s-{e:.0f}s: {m} → গান #{t['id']} ({t['mood']}, {t['desc']})")
    mix_items(items, dur, "bgm_mix.wav")
    subprocess.run(["ffmpeg", "-y", "-i", "bgm_mix.wav", "-af",
                    f"afade=t=in:d=3,afade=t=out:st={max(dur - 5, 0):.2f}:d=5", "-ar", "44100", "bgm_full.wav"],
                   check=True, capture_output=True)
    return "bgm_full.wav"


def make_bgm(dur, files):
    user = next((f for f in files if f["name"].lower().startswith("bgm")
                 and f["name"].lower().endswith(AUD_EXT)), None)
    if user:
        d_download(user["id"], "bgm_src")
        src = "bgm_src"
        print("bgm: নিজের ফাইল ব্যবহার হচ্ছে (" + user["name"] + ")")
    else:
        synth_loop(BGM_MOOD, "bgm_loop.wav")
        src = "bgm_loop.wav"
        print(f"bgm: '{BGM_MOOD}' mood-এ নিজে বানানো হলো")
    subprocess.run(["ffmpeg", "-y", "-stream_loop", "-1", "-i", src, "-t", f"{dur:.2f}",
                    "-af", f"afade=t=in:d=3,afade=t=out:st={max(dur - 5, 0):.2f}:d=5",
                    "-ar", "44100", "bgm_full.wav"], check=True, capture_output=True)
    return "bgm_full.wav"


# ---------------------------------------------------------------------
# Sound effects (ffmpeg দিয়ে synthesize)
# ---------------------------------------------------------------------
SFX_SRC = {
    # --- আগের effect ---
    "whoosh": "anoisesrc=d=1.4:c=pink:r=44100:a=0.6,highpass=f=300,lowpass=f=4500,"
              "afade=t=in:d=0.7:curve=qsin,afade=t=out:st=0.7:d=0.7:curve=qsin,aecho=0.8:0.7:60:0.3",
    "boom": "aevalsrc='0.9*sin(2*PI*(38+90*exp(-9*t))*t)*exp(-1.8*t)+0.25*(random(0)*2-1)*exp(-18*t)':s=44100:d=3,lowpass=f=900",
    "riser": "aevalsrc='0.6*(random(0)*2-1)*pow(min(1,t/3),2)':s=44100:d=3,highpass=f=500,lowpass=f=6000,afade=t=out:st=2.8:d=0.2",
    "wind": "anoisesrc=d=7:c=brown:r=44100:a=0.5,lowpass=f=600,afade=t=in:d=2,afade=t=out:st=5:d=2",
    "heartbeat": "aevalsrc='0.9*sin(2*PI*52*t)*exp(-12*t)+0.7*gt(t,0.3)*sin(2*PI*46*t)*exp(-12*(t-0.3))':s=44100:d=1.1,"
                 "aloop=loop=2:size=48510,lowpass=f=300",
    # --- scene বদলের হালকা শব্দ ---
    "swish": "anoisesrc=d=0.8:c=pink:r=44100:a=0.5,highpass=f=500,lowpass=f=6500,"
             "afade=t=in:d=0.35:curve=qsin,afade=t=out:st=0.35:d=0.45:curve=qsin",
    # --- দৃশ্যের শব্দ (scene-এর "sounds" থেকে) ---
    "footsteps": "aevalsrc='0.9*(random(0)*2-1)*exp(-22*mod(t,0.55))':s=44100:d=2.3,highpass=f=70,lowpass=f=900",
    "bell": "aevalsrc='0.5*sin(2*PI*523*t)*exp(-1.4*t)+0.3*sin(2*PI*1046*t)*exp(-2*t)+0.25*sin(2*PI*1255*t)*exp(-2.6*t)"
            "+0.15*sin(2*PI*1600*t)*exp(-3.4*t)+0.1*sin(2*PI*2330*t)*exp(-5*t)':s=44100:d=5,aecho=0.8:0.6:120:0.3",
    "clock": "aevalsrc='0.8*sin(2*PI*1800*t)*exp(-250*mod(t,1))+0.3*(random(0)*2-1)*exp(-400*mod(t,1))':s=44100:d=4,highpass=f=400",
    "knock": "aevalsrc='(sin(2*PI*140*t)*exp(-28*mod(t,0.35))+0.4*(random(0)*2-1)*exp(-90*mod(t,0.35)))*lt(t,1.0)':s=44100:d=1.4,lowpass=f=1500",
    "gunshot": "aevalsrc='0.9*(random(0)*2-1)*exp(-40*t)+0.5*sin(2*PI*90*t)*exp(-25*t)':s=44100:d=1.6,aecho=0.8:0.6:180:0.35",
    "explosion": "aevalsrc='0.8*(random(0)*2-1)*exp(-2.5*t)+0.8*sin(2*PI*(45+60*exp(-6*t))*t)*exp(-1.6*t)':s=44100:d=4,lowpass=f=1400",
    "rain": "anoisesrc=d=8:c=pink:r=44100:a=0.5,highpass=f=900,lowpass=f=8000",
    "thunder": "anoisesrc=d=5:c=brown:r=44100:a=0.9,lowpass=f=220,afade=t=in:d=0.4,afade=t=out:st=1.0:d=4",
    "crowd": "anoisesrc=d=8:c=pink:r=44100:a=0.5,bandpass=f=900:width_type=h:w=1400,tremolo=f=5:d=0.5",
    "night": "aevalsrc='0.35*sin(2*PI*4600*t)*gt(sin(2*PI*28*t),0.2)*(0.6+0.4*sin(2*PI*0.5*t))':s=44100:d=8",
    "fire": "aevalsrc='(random(0)*2-1)*(0.06+0.9*gt(random(1),0.9985))':s=44100:d=8,lowpass=f=3500",
    "siren": "aevalsrc='0.5*sin(2*PI*(700*t-(300/(2*PI*0.4))*cos(2*PI*0.4*t)))':s=44100:d=6,lowpass=f=2500,"
             "aecho=0.8:0.5:150:0.3,afade=t=in:d=0.6,afade=t=out:st=5:d=1",
    "static": "anoisesrc=d=2.5:c=white:r=44100:a=0.5,highpass=f=800,lowpass=f=5000,tremolo=f=9:d=0.8",
}
SOUND_NAMES = tuple(k for k in SFX_SRC if k not in ("whoosh", "boom", "riser", "wind", "heartbeat", "swish"))
# scene শুরুর আগে/পরে কত সেকেন্ডে effect বসবে (riser আগে শুরু হয়ে scene-এ পৌঁছে চূড়ায় ওঠে)
SFX_OFF = {"whoosh": -0.15, "boom": 0.0, "riser": -2.6, "wind": 0.0, "heartbeat": 0.0}
# প্রতিটা effect-কে একই peak-এ আনা হয়; এই gain ঠিক করে কোনটা কত জোরে বাজবে
SFX_GAIN = {"whoosh": 0.8, "boom": 1.0, "riser": 0.7, "wind": 0.6, "heartbeat": 0.9, "swish": 1.0,
            "footsteps": 0.9, "bell": 0.7, "clock": 1.0, "knock": 0.9, "gunshot": 0.8, "explosion": 0.9,
            "rain": 0.5, "thunder": 0.8, "crowd": 0.45, "night": 0.3, "fire": 0.9, "siren": 0.35, "static": 0.5}
SFX_BEDS = {"wind", "rain", "crowd", "night", "fire"}      # পুরো scene জুড়ে বাজে (loop হয়ে)
_audio_cache = {}


def sfx_file(name):
    p = f"sfx_{name}.wav"
    if not os.path.exists(p):
        raw = f"sfxraw_{name}.wav"
        subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", SFX_SRC[name], "-ar", "44100", "-ac", "2", raw],
                       check=True, capture_output=True)
        # peak -3 dB-এ আনা, যাতে সব effect-এর volume সমান ভিত্তিতে শুরু হয়
        r = subprocess.run(["ffmpeg", "-i", raw, "-af", "volumedetect", "-f", "null", "-"],
                           capture_output=True, text=True)
        mv = re.search(r"max_volume:\s*(-?[\d.]+) dB", r.stderr)
        gain = (-3.0 - float(mv.group(1))) if mv else 0.0
        subprocess.run(["ffmpeg", "-y", "-i", raw, "-af", f"volume={gain:.2f}dB", p],
                       check=True, capture_output=True)
    return p


def has_audio(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries",
                        "stream=codec_type", "-of", "csv=p=0", path], capture_output=True, text=True)
    return "audio" in r.stdout


def user_sfx_names(files):
    """Drive folder-এ নিজের আপলোড করা 'sfx_dog.mp3' ধরনের ফাইল → {'dog': file}।"""
    out = {}
    for f in files:
        n = f["name"].lower()
        if n.startswith("sfx_") and n.endswith(AUD_EXT) and f["mimeType"] != FOLDER_MIME:
            out[n[4:].rsplit(".", 1)[0]] = f
    return out


def mix_items(items, total, out):
    """items: {file, at, vol, dur, bed, fade_in, fade_out} - সবগুলো সময়মতো বসিয়ে একটা track বানায় (৪০টা করে ভাগে)।"""
    outs = []
    for ci in range(0, len(items), 40):
        ch = items[ci:ci + 40]
        cmd, parts = ["ffmpeg", "-y"], []
        for k, it in enumerate(ch):
            if it.get("bed"):
                cmd += ["-stream_loop", "-1"]
            cmd += ["-i", it["file"]]
            chain = "aformat=sample_rates=44100:channel_layouts=stereo"
            ss = float(it.get("ss") or 0.0)
            if it.get("dur"):
                chain += f",atrim={ss:.3f}:{ss + it['dur']:.3f},asetpts=PTS-STARTPTS"
            elif ss:
                chain += f",atrim=start={ss:.3f},asetpts=PTS-STARTPTS"
            if it.get("fade_in"):
                chain += f",afade=t=in:d={it['fade_in']:.2f}"
            if it.get("fade_out") and it.get("dur"):
                chain += f",afade=t=out:st={max(it['dur'] - it['fade_out'], 0):.2f}:d={it['fade_out']:.2f}"
            ms = int(max(it["at"], 0) * 1000)
            chain += f",volume={it.get('vol', 1.0):.3f},adelay={ms}|{ms}"
            parts.append(f"[{k}:a]{chain}[s{k}]")
        lab = "".join(f"[s{k}]" for k in range(len(ch)))
        parts.append(f"{lab}amix=inputs={len(ch)}:normalize=0:duration=longest,"
                     f"apad=whole_dur={total:.2f},atrim=0:{total:.2f}[o]")
        po = f"sfx_part_{ci // 40}.wav"
        cmd += ["-filter_complex", ";".join(parts), "-map", "[o]", "-ar", "44100", "-c:a", "pcm_f32le", po]
        subprocess.run(cmd, check=True, capture_output=True)
        outs.append(po)
    if len(outs) == 1:
        os.replace(outs[0], out)
        return
    cmd = ["ffmpeg", "-y"]
    for o in outs:
        cmd += ["-i", o]
    lab = "".join(f"[{k}:a]" for k in range(len(outs)))
    cmd += ["-filter_complex", f"{lab}amix=inputs={len(outs)}:normalize=0:duration=longest[o]",
            "-map", "[o]", "-ar", "44100", "-c:a", "pcm_f32le", out]
    subprocess.run(cmd, check=True, capture_output=True)


def build_sfx(scenes, scene_audio, total, user_paths=None, dlg=(), clip_native=None, trans_points=None, picker=None):
    """শব্দ বসায়: (১) নাটকীয় effect (২) দৃশ্যের শব্দ (৩) scene বদলের transition-sound (ছবির transition style অনুযায়ী,
    peak ঠিক transition-এর মাঝে) (৪) Flow clip-এর নিজের sound। picker থাকলে আগে নিজের sound library থেকে বাছে, না মিললে ffmpeg-এর বানানো শব্দ।"""
    user_paths = user_paths or {}
    clip_native = clip_native or {}
    starts, durs, t = [], [], 0.0
    for seg in scene_audio:
        starts.append(t)
        durs.append(len(seg) / (2 * SR))
        t += durs[-1]
    n = len(starts)
    items, log = [], []
    tags = [s.get("sfx", "") for s in scenes][:n]
    if SFX_ON:
        legacy = not any("sounds" in s for s in scenes)
        if legacy and not any(tags):      # খুব পুরনো project: নিজে বসানো
            tags = [""] * n
            if tags:
                tags[0] = "boom"
            for i in range(6, n, 7):
                tags[i] = "whoosh"
        for i, g in enumerate(tags):      # (১) নাটকীয় effect
            if g in SFX_OFF and g in SFX_SRC:
                seg = picker.pick(tags=(g,), kinds=("impact", "ambient", "transition"), need=6) if picker else None
                if seg:
                    bed = g in SFX_BEDS
                    it = lib_item(seg, starts[i], LIB_GAIN.get(seg["kind"]) or TRANS_SFX_VOL,
                                  align_peak=not bed and g != "heartbeat", bed=bed, dur=durs[i] + 0.8)
                    items.append(it)
                    log.append(f"{g}*@{it['at']:.0f}s")
                    continue
                at = max(0.0, starts[i] + SFX_OFF[g])
                items.append({"file": sfx_file(g), "at": at, "vol": SFX_GAIN.get(g, 1.0)})
                log.append(f"{g}@{at:.0f}s")
        for i, sc in enumerate(scenes[:n]):   # (২) দৃশ্যের শব্দ
            if i in dlg or i in clip_native:
                continue                       # dialogue বা নিজস্ব sound-ওয়ালা clip-এ আলাদা শব্দ বসে না
            for sn in (sc.get("sounds") or [])[:2]:
                nm = str(sn.get("name", "")).strip().lower()
                ds = durs[i]
                off = {"start": 0.15, "mid": ds * 0.45, "end": max(ds - 1.6, 0.0)}.get(sn.get("at", "start"), 0.15)
                seg = (picker.pick(tags=(nm,) if nm else (), kinds=("scene", "ambient", "impact", "music"),
                                   desc=str(sn.get("desc", "")), need=4) if picker else None)
                if seg:
                    bed = seg["kind"] == "ambient"
                    vol = LIB_GAIN.get(seg["kind"]) or 0.8
                    it = lib_item(seg, starts[i] + (0.0 if bed else off), vol, align_peak=False, bed=bed, dur=ds + 0.8)
                    if not bed and seg["dur"] > ds + 3.0:
                        it.update({"dur": ds + 3.0, "fade_out": 0.6})
                    items.append(it)
                    log.append(f"{seg['tag']}*@{it['at']:.0f}s")
                    continue
                path = user_paths.get(nm) or (sfx_file(nm) if nm in SFX_SRC and nm != "swish" else None)
                if not path:
                    continue
                vol = SFX_GAIN.get(nm, 0.8)
                if nm in SFX_BEDS:
                    it = {"file": path, "at": starts[i], "vol": vol, "bed": True, "dur": ds + 0.8,
                          "fade_in": 0.4, "fade_out": 0.6}
                else:
                    cap = ds + 3.0
                    it = {"file": path, "at": starts[i] + off, "vol": vol}
                    if probe_duration(path) > cap:
                        it.update({"dur": cap, "fade_out": 0.6})
                items.append(it)
                log.append(f"{nm}@{it['at']:.0f}s")
        if TRANS_SFX and trans_points:         # (৩) scene বদলের শব্দ — transition style-এর সাথে মেলানো
            sw, nt = None, 0
            for k, tp in enumerate(trans_points):
                tt, a_, b_ = tp[0], tp[1], tp[2]
                style = tp[3] if len(tp) > 3 else "fade"
                if a_ in dlg or b_ in dlg or (b_ < len(tags) and tags[b_] in SFX_OFF):
                    continue
                if style in ("fade", "dissolve") and k % 3:       # সাধারণ fade-এ প্রতিবার শব্দ নয়; বিশেষ style-এ সবসময়
                    continue
                seg = (picker.pick(tags=(style, "whoosh", "swish"), kinds=("transition",), style=style, need=0)
                       if picker else None)
                if seg:
                    items.append(lib_item(seg, tt, TRANS_SFX_VOL, align_peak=True))
                else:
                    sw = sw or sfx_file("swish")
                    items.append({"file": sw, "at": max(0.0, tt - 0.35), "vol": TRANS_SFX_VOL})
                nt += 1
            log.append(f"transition-sound x{nt}")
    if CLIP_AUDIO_VOL > 0:                     # (৪) clip-এর নিজের sound
        for i, (path, cd) in clip_native.items():
            if i < n:
                items.append({"file": path, "at": starts[i], "vol": CLIP_AUDIO_VOL,
                              "dur": min(durs[i], cd or durs[i]), "fade_in": 0.1, "fade_out": 0.25})
                log.append(f"clip-sound@{starts[i]:.0f}s")
    if not items:
        return None
    print("SFX:", ", ".join(log[:40]), "..." if len(log) > 40 else "")
    mix_items(items, total, "sfx_track.wav")
    return "sfx_track.wav"


def final_mix(total, bgm, sfx=None):
    """voice পরিষ্কার করে, music duck করে, sfx যোগ করে YouTube loudness-এ আনে।"""
    # sfx: voice বললে হালকা (≈-4dB) নিচে নামে যাতে কথা না ঢাকে; কিন্তু ফাঁকা জায়গায় পুরো জোরে বাজে
    fx = (f"[3:a]volume={SFX_VOL},aformat=sample_rates=44100:channel_layouts=stereo[fxr];"
          "[fxr][v3]sidechaincompress=threshold=0.03:ratio=2.5:attack=8:release=250[fx];"
          if sfx else "")
    mixin = "[v1][duck][fx]amix=inputs=3" if sfx else "[v1][duck]amix=inputs=2"
    fc = (
        # voice: low-cut + warmth + presence + de-harsh + হালকা compression
        "[1:a]highpass=f=80,equalizer=f=180:t=q:w=1:g=2,equalizer=f=3000:t=q:w=1.2:g=2.5,"
        "equalizer=f=7500:t=q:w=2:g=-3,"
        "acompressor=threshold=-22dB:ratio=2.5:attack=15:release=200:makeup=1.5,"
        "aformat=sample_rates=44100:channel_layouts=stereo,asplit=3[v1][v2][v3];"
        # music
        f"[2:a]volume={BGM_VOL},equalizer=f=1500:t=q:w=1:g=-4,"
        "aformat=sample_rates=44100:channel_layouts=stereo[m];"
        # voice বললে music নিচে নামে
        "[m][v2]sidechaincompress=threshold=0.02:ratio=6:attack=30:release=600[duck];"
        + fx +
        f"{mixin}:duration=first:normalize=0,"
        f"loudnorm=I={LUFS}:TP=-1.5:LRA=9,afade=t=out:st={max(total - 2, 0):.2f}:d=2[a]")
    cmd = ["ffmpeg", "-y", "-i", "video_noaudio.mp4", "-i", "voice.wav", "-i", bgm]
    if sfx:
        cmd += ["-i", sfx]
    cmd += ["-filter_complex", fc, "-map", "0:v", "-map", "[a]",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "256k", "-shortest",
            "-movflags", "+faststart", "final_video.mp4"]
    subprocess.run(cmd, check=True, capture_output=True)


def run_video(files, zips):
    sid = next(f["id"] for f in files if f["name"] == "scenes.json")
    d_download(sid, "scenes.json")
    scenes = json.load(open("scenes.json", encoding="utf-8"))["scenes"]
    N = len(scenes)
    stamp = datetime.datetime.utcnow().strftime("%Y%m%d_%H%M")
    print("Zip:", ", ".join(z["name"] for z in zips), "| scene:", N)

    # ---- ছবি/clip মেলানো (ফাইলের নম্বর = scene নম্বর) ----
    media, info, mapping, old_map_id = match_media(files, zips, scenes)
    media_groups, own = build_groups(media, info, mapping, N)
    if old_map_id:
        try:
            d_delete(old_map_id)
        except Exception as e:
            print("পুরনো image_map মোছা যায়নি:", str(e)[:100])
    d_upload("image_map.json", "image_map.json", OUT_ID, "application/json")

    # ---- dialogue clip ধরা: clip-এ কথা থাকলে ওই scene-এ narration চুপ, clip-এর audio বাজবে ----
    dlg = {}
    for si, sc in enumerate(scenes):
        if (si + 1) not in own:
            continue
        if not sc.get("dialogue") and DIALOGUE_DETECT != "any":
            continue
        for inf in media_groups[si]:
            if not inf["video"]:
                continue
            pcm = dialogue_audio(inf)
            if pcm:
                dur = probe_duration(inf["path"]) or len(pcm) / (2 * SR)
                nb = int(round(dur * SR)) * 2
                pcm = pcm[:nb] + b"\x00" * max(0, nb - len(pcm))
                dlg[si] = {"info": inf, "pcm": normalize(pcm), "dur": dur}
                print(f"Dialogue clip: scene {si + 1} <- {inf['name']} ({dur:.1f}s) | narration চুপ থাকবে")
                break
    print(f"Dialogue scene: {len(dlg)}টা | narration scene: {N - len(dlg)}টা")

    # ---- narration block (dialogue scene-এ block ভাঙে, তাই কথা মিশে যায় না) ----
    blocks, cur = [], []
    for i, s in enumerate(scenes):
        if i in dlg:
            if cur:
                blocks.append(cur)
                cur = []
            continue
        if cur and sum(len(scenes[k]["narration"]) for k in cur) + len(s["narration"]) > BLOCK_CHARS:
            blocks.append(cur)
            cur = []
        cur.append(i)
    if cur:
        blocks.append(cur)
    print(f"Audio block: {len(blocks)}টা (BLOCK_CHARS={BLOCK_CHARS})")

    cache_id = next((f["id"] for f in files
                     if f["name"] == "_audio_cache" and f["mimeType"] == FOLDER_MIME), None) \
        or d_mkdir("_audio_cache", OUT_ID)
    cache = {f["name"]: f["id"] for f in d_list(cache_id)}
    # আগের (পুরনো) project folder-এর cache-ও খোঁজা, যাতে TTS quota নষ্ট না হয়
    for fo in [f for f in files if f["mimeType"] == FOLDER_MIME
               and f["id"] != cache_id and not f["name"].startswith("_")]:
        try:
            for c in d_list(fo["id"], f"and name='_audio_cache' and mimeType='{FOLDER_MIME}'"):
                for x in d_list(c["id"]):
                    cache.setdefault(x["name"], x["id"])
        except Exception as e:
            print("পুরনো cache দেখা যায়নি:", str(e)[:80])

    raw = []
    for bi, blk in enumerate(blocks):
        text = " ".join(scenes[k]["narration"] for k in blk)
        h = hashlib.md5((text + VOICE + STYLE).encode()).hexdigest()
        keys = [f"blk_{h[:12]}.pcm", f"blk_{bi:03d}_{h[:8]}.pcm"]
        hit = next((k for k in keys if k in cache), None)
        if hit:
            d_download(cache[hit], "blk.pcm")
            pcm = open("blk.pcm", "rb").read()
            print(f"Audio block {bi + 1}/{len(blocks)} (cache থেকে)")
        else:
            print(f"Audio block {bi + 1}/{len(blocks)}")
            pcm = normalize(with_models(TTS_MODELS, lambda c, m, t=text: tts(c, m, t)))
            open("blk.pcm", "wb").write(pcm)
            cache[keys[0]] = d_upload("blk.pcm", keys[0], cache_id, "application/octet-stream")
            time.sleep(1)
        raw.append(pcm)

    before = sum(len(p) for p in raw) / (2 * SR)
    raw = [trim_pauses(p) for p in raw]
    after = sum(len(p) for p in raw) / (2 * SR)
    print(f"নীরবতা ছাঁটা: {before:.0f}s -> {after:.0f}s ({before - after:.0f}s কমেছে)")

    # ---- গতি সমান করা: প্রথম block-এর গতি = মাপকাঠি; block, তারপর scene ধরে ঠিক করা ----
    texts_b = [" ".join(scenes[k]["narration"] for k in blk) for blk in blocks]
    rates = [speech_chars(t) / active_seconds(p) for t, p in zip(texts_b, raw)]
    target = (statistics.median(rates[:min(3, len(rates))]) * VOICE_SPEED) if rates else 1.0
    print("গতি (অক্ষর/সেকেন্ড):", [round(r, 1) for r in rates], "target:", round(target, 1))

    scene_audio = [None] * N
    for blk, pcm, r in zip(blocks, raw, rates):
        factor = max(0.8, min(1.25, target / r))
        if abs(factor - 1) > 0.03:
            pcm = change_tempo(pcm, factor)
        segs = split_block(pcm, [scenes[k]["narration"] for k in blk])
        for k, seg in zip(blk, segs):
            rate = speech_chars(scenes[k]["narration"]) / active_seconds(seg)
            f2 = max(0.9, min(1.1, target / rate))
            if abs(f2 - 1) > 0.04 and len(seg) > SR * 2:
                seg = change_tempo(seg, f2)
            seg = normalize(trim_pauses(seg))       # নীরবতা ছাঁটা + প্রতি scene-এ সমান volume
            scene_audio[k] = seg + b"\x00\x00" * int(SR * 0.05)
    for k, d in dlg.items():
        scene_audio[k] = d["pcm"]                    # dialogue scene: clip-এর নিজের কথা

    durs = [len(a) / (2 * SR) for a in scene_audio]
    print(f"scene দৈর্ঘ্য: min {min(durs):.1f}s, গড় {statistics.mean(durs):.1f}s, max {max(durs):.1f}s")

    full = b"".join(scene_audio)
    with wave.open("voice.wav", "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(SR)
        w.writeframes(full)
    total = len(full) / (2 * SR)
    print(f"মোট audio: {total / 60:.1f} মিনিট")

    # ---- প্রতিটা scene-এর clip ----
    os.makedirs("clips", exist_ok=True)
    for old in os.listdir("clips"):
        os.remove(os.path.join("clips", old))
    plan, t_acc, f_acc = [], 0.0, 0
    for si in range(N):
        t_acc += len(scene_audio[si]) / (2 * SR)
        end_f = round(t_acc * FPS)
        frames = max(end_f - f_acc, 12)
        f_acc += frames
        grp = [dlg[si]["info"]] if si in dlg else media_groups[si]
        k = len(grp)
        for j, inf in enumerate(grp):
            part = max(frames // k + (1 if j < frames % k else 0), 12)
            plan.append({"si": si, "inf": inf, "nom": part})
    TF = trans_frames(min(p["nom"] for p in plan), len(plan))
    print(f"Transition: {'crossfade ' + str(TF) + ' frame (' + format(TF / FPS, '.2f') + 's)' if TF else 'নেই'} | মোট clip {len(plan)}টা")
    # প্রতিটা সীমানার transition style: scene-এর "transition" (AI-র বাছা) → না থাকলে TRANSITION env-এর তালিকা পালা করে।
    # একই scene-এর ভেতরের clip-এ শুধু নরম fade।
    styles = []
    for i in range(len(plan) - 1):
        if plan[i]["si"] == plan[i + 1]["si"]:
            styles.append("fade")
            continue
        st = str(scenes[plan[i + 1]["si"]].get("transition") or "").strip().lower()
        styles.append(st if st in TRANS_STYLES else TRANSITION[plan[i + 1]["si"] % len(TRANSITION)])
    if TF:
        cnt = {}
        for s_ in styles:
            cnt[s_] = cnt.get(s_, 0) + 1
        print("Transition style:", ", ".join(f"{k}={v}" for k, v in cnt.items()))
    pieces, trans_points, cum = [], [], 0
    for i, p in enumerate(plan):
        inf = p["inf"]
        first, last = (i == 0), (i == len(plan) - 1)
        sd = probe_duration(inf["path"]) if inf["video"] else None
        if TF:
            p["parts"] = make_clip_parts(inf["path"], inf["video"], p["nom"], f"clips/c{i:05d}", i,
                                         first, last, TF, sd)
        else:
            out = f"clips/c{i:05d}_mid.mp4"
            make_clip(inf["path"], inf["video"], p["nom"], out, variant=i, fade_in=first, fade_out=last, src_dur=sd)
            p["parts"] = {"mid": out}
        cum += p["nom"]
        if i < len(plan) - 1 and plan[i + 1]["si"] != p["si"]:
            trans_points.append((cum / FPS, p["si"], plan[i + 1]["si"], styles[i]))
        if (i + 1) % 10 == 0 or i + 1 == len(plan):
            print(f"Clip তৈরি: {i + 1}/{len(plan)}")
    for i, p in enumerate(plan):
        pieces.append(p["parts"]["mid"])
        if TF and i < len(plan) - 1:
            xo = f"clips/x{i:05d}.mp4"
            make_xfade(p["parts"]["tail"], plan[i + 1]["parts"]["head"], TF, styles[i], xo)
            pieces.append(xo)

    with open("list.txt", "w") as lf:
        for c in pieces:
            lf.write(f"file '{c}'\n")
    subprocess.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", "list.txt",
                    "-c", "copy", "video_noaudio.mp4"], check=True, capture_output=True)
    vd = probe_duration("video_noaudio.mp4")
    print(f"video দৈর্ঘ্য {vd:.1f}s | audio দৈর্ঘ্য {total:.1f}s")

    # ---- music + sfx + mix ----
    subprocess.run(["ffmpeg", "-y", "-i", "voice.wav", "-af", "loudnorm=I=-16:TP=-1.5:LRA=7",
                    "-b:a", "192k", "voiceover.mp3"], check=True, capture_output=True)
    bgm = None
    try:                                       # একাধিক গান + scene-এর mood থাকলে মানানসই গান; নইলে আগের নিয়ম
        _tr = bgm_load(files)
        _secs = bgm_sections(scenes, scene_audio) if _tr else None
        if _tr and _secs:
            print(f"bgm: mood অনুযায়ী {len(_secs)}টা অংশে গান বসানো হচ্ছে")
            bgm = make_bgm_mood(total + 1, _tr, _secs)
    except Exception as e:
        print("mood-অনুযায়ী bgm হলো না, সাধারণ bgm-এ চলছে:", type(e).__name__, str(e)[:120])
    if not bgm:
        bgm = make_bgm(total + 1, files)
    sfx_scenes = [dict(s, sfx="") if i in dlg else s for i, s in enumerate(scenes)]
    user_paths = {}
    if SFX_ON:                                # Drive folder-এ নিজের 'sfx_<নাম>.mp3' থাকলে সেটা ব্যবহার হবে
        want = {str(x.get("name", "")).strip().lower() for sc_ in scenes for x in (sc_.get("sounds") or [])}
        for nm, f in user_sfx_names(files).items():
            if nm in want:
                lp = "usersfx_" + re.sub(r"[^A-Za-z0-9]+", "_", nm) + os.path.splitext(f["name"])[1]
                d_download(f["id"], lp)
                user_paths[nm] = lp
        if user_paths:
            print("নিজের sfx ফাইল ব্যবহার হচ্ছে:", ", ".join(user_paths))
    clip_native = {}
    if CLIP_AUDIO_VOL > 0:
        for si in range(N):
            if si in dlg or (si + 1) not in own:
                continue
            for inf in media_groups[si]:
                if inf["video"] and has_audio(inf["path"]):
                    clip_native[si] = (inf["path"], probe_duration(inf["path"]))
                    break
        if clip_native:
            print(f"clip-এর নিজের sound মেশানো হবে: {len(clip_native)}টা scene (volume {CLIP_AUDIO_VOL})")
    picker = None
    if SFX_ON:
        try:
            _lib = lib_load(files)
            picker = SoundPicker(_lib) if _lib else None
        except Exception as e:
            print("Sound library ব্যবহার করা গেল না, ffmpeg-এর শব্দে চলছে:", type(e).__name__, str(e)[:120])
    sfx = build_sfx(sfx_scenes, scene_audio, total, user_paths, set(dlg), clip_native, trans_points, picker)
    final_mix(total, bgm, sfx)
    subprocess.run(["ffmpeg", "-y", "-i", bgm, "-b:a", "192k", "background_music.mp3"],
                   check=True, capture_output=True)

    for local, nm, mime in [("final_video.mp4", f"final_video_{stamp}.mp4", "video/mp4"),
                            ("voiceover.mp3", f"voiceover_{stamp}.mp3", "audio/mpeg"),
                            ("background_music.mp3", f"background_music_{stamp}.mp3", "audio/mpeg")]:
        d_upload(local, nm, OUT_ID, mime)
        print("Uploaded:", nm)
    for z in zips:                                   # কাজ শেষ: zip-এর নামে done_ বসে, আর দ্বিতীয়বার হবে না
        try:
            d_rename(z["id"], "done_" + z["name"])
        except Exception as e:
            print("zip rename করা যায়নি:", str(e)[:100])
    try:
        d_delete(cache_id)
    except Exception as e:
        print("cache মোছা যায়নি:", str(e)[:100])
    print("সম্পূর্ণ।")


def watch():
    """প্রতি POLL_SEC সেকেন্ডে output folder দেখে: zip + scenes.json থাকলে video বানায়, নইলে অপেক্ষা।"""
    global rot, TOPIC
    once = (MODE == "once")   # once = শুধু একবার check করে বেরিয়ে যায় (GitHub cron প্রতি ১৫ মিনিটে চালায়)
    t0, fails = time.time(), 0
    print(f"Watch শুরু: প্রতি {POLL_SEC}s পর Drive check, সর্বোচ্চ {WATCH_MAX_MIN} মিনিট")
    while True:
        if time.time() - t0 > WATCH_MAX_MIN * 60:
            print("Watch সময় শেষ; নতুন watcher (cron) বাকিটা দেখবে।")
            return
        try:
            files = d_list(OUT_ID)
            zips = find_zips(files)
            has_scenes = any(f["name"] == "scenes.json" for f in files)
            has_mp4 = any(f["name"].lower().endswith(".mp4") and f["mimeType"] != FOLDER_MIME for f in files)
            if zips and has_scenes:
                print(f"zip পাওয়া গেছে ({len(zips)}টা) → video কাজ শুরু")
                run_video(files, zips)
                fails = 0
                if once:
                    return
                continue
            if zips:
                print("zip আছে কিন্তু scenes.json নেই (আগে prompts mode চালাও); অপেক্ষা...")
            elif AUTO_TOPIC and has_mp4:
                print("আগের video তৈরি আছে → mp4 archive করে নতুন trending topic-এর prompt পাঠানো হচ্ছে")
                TOPIC = ""
                run_prompts()
                fails = 0
                if once:
                    return
                continue
            else:
                print("zip নেই (শুধু prompt আছে); অপেক্ষা...")
        except AllGeminiKeysExhausted as e:
            print(e)
            print(f"{QUOTA_WAIT_MIN} মিনিট পর আবার চেষ্টা (cache-এ রাখা audio নষ্ট হবে না)")
            rot = Rotator(KEYS)
            if once:
                print("এই run বন্ধ; পরের ১৫ মিনিটের run আবার চেষ্টা করবে।")
                return
            time.sleep(QUOTA_WAIT_MIN * 60)
            continue
        except Exception as e:
            fails += 1
            print(f"কাজ ব্যর্থ ({fails}/3): {type(e).__name__}: {str(e)[:200]}")
            if once:
                sys.exit("এই run ব্যর্থ; পরের ১৫ মিনিটের run আবার চেষ্টা করবে")
            if fails >= 3:
                sys.exit("৩ বার ব্যর্থ; log দেখে ঠিক করে আবার চালাও")
            rot = Rotator(KEYS)
            time.sleep(300)
            continue
        if once:
            return
        time.sleep(POLL_SEC)


if MODE == "prompts":
    run_prompts()
elif MODE == "video":
    _files = d_list(OUT_ID)
    _zips = find_zips(_files)
    if not _zips or not any(f["name"] == "scenes.json" for f in _files):
        sys.exit("zip বা scenes.json পাওয়া যায়নি")
    run_video(_files, _zips)
else:
    watch()
