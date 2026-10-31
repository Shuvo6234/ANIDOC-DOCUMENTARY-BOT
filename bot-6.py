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

MODE = (os.getenv("MODE") or "watch").strip()   # watch | prompts | video
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
            fields="nextPageToken, files(id,name,mimeType,createdTime)",
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
        for d in sc["dialogue"]:
            nm = cmap.get(d["speaker"], {}).get("name") or d["speaker"] or "the character"
            v = f" Voice: {d['voice']}." if d.get("voice") else ""
            said.append(f'{nm} says in Hindi: "{d["line"]}".{v}')
        parts.append("DIALOGUE (spoken on camera, lips perfectly synced, clear natural voice, the line must finish "
                     "before the clip ends): " + " ".join(said) + " No background music.")
    else:
        parts.append("DIALOGUE: none. No one speaks, no lip movement, no voice-over, no music; only soft natural ambient sound.")
    parts.append("CONSISTENCY: temporally stable, no morphing or warping of faces, hands or objects, no sudden outfit or "
                 "background change, no flicker, no extra people, no on-screen text, subtitles, captions, logos or "
                 "watermarks, sharp focus, natural motion, realistic physics.")
    return " ".join(parts)


def scene_image_text(sc, total):
    """শুধু IMAGE prompt (video prompt এখানে নেই)।"""
    tag = f"{sc['n']:03d}"
    return "\n".join([
        f"FILENAME: {tag}",
        f"  -> Save the generated image as {tag}.png (or .jpg). The file name must be ONLY this number.",
        f"SCENE: {sc['n']} of {total}",
        "",
        "=== IMAGE PROMPT ===",
        sc["image_prompt"],
        ""])


def scene_video_text(sc, total):
    """শুধু VIDEO prompt (image prompt এখানে নেই)।"""
    tag = f"{sc['n']:03d}"
    has = bool(sc.get("dialogue"))
    return "\n".join([
        f"FILENAME: {tag}",
        f"  -> Save the generated clip as {tag}.mp4. The file name must be ONLY this number.",
        f"  -> Extra takes of the same scene: {tag}_b.mp4, {tag}_c.mp4. Put all files in one .zip and upload it to the output folder.",
        f"CLIP LENGTH: {sc['duration']} seconds",
        f"SCENE: {sc['n']} of {total}",
        "HAS DIALOGUE: " + ("YES - the clip's own audio is used and the narration is muted for this scene"
                            if has else "NO - the narration voice plays over this clip"),
        "",
        "NARRATION (reference only; do NOT speak it in the clip"
        + (", the dialogue below replaces it" if has else "") + "):",
        sc["narration"],
        "",
        "=== VIDEO PROMPT ===",
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
    (নিজের bgm.* গান আর archive folder কখনো মোছা হয় না।)"""
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
        if f["name"].lower().startswith("bgm"):
            continue
        try:
            d_delete(f["id"])
            n += 1
        except Exception as e:
            print("মোছা যায়নি:", f["name"], str(e)[:80])
    print(f"স্থায়ীভাবে মোছা হলো: {n}টা ফাইল/folder")


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
            "তাই line-ই পুরো কথা বলবে। প্রতি ১০ scene-এ সর্বোচ্চ ১টা; speaker অবশ্যই character ID।\n"
            '- "characters": এই shot-এ যে character ID-গুলো দেখা যাবে\n'
            '- "location": location ID (না থাকলে "")\n'
            '- "props": দৃশ্যমান prop ID-র তালিকা\n'
            '- "sfx": whoosh (transition) | boom (নাটকীয় মুহূর্ত) | riser (revelation-এর আগে) | '
            'wind (নির্জন/space) | heartbeat (suspense) | "" । প্রতি 8 scene-এ সর্বোচ্চ 1টা, বেশিরভাগ ""।\n'
            f"Characters: {cids}\nLocations: {lids}\nProps: {pids}\n"
            "শুধু ঐ ID-গুলোই ব্যবহার করো, নতুন বানাবে না।\n"
            'শুধু JSON array: [{"n": 1, "shot": "...", "motion": "...", "outfit": "", "dialogue": [], '
            '"characters": ["C1"], "location": "L1", "props": [], "sfx": ""}] '
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
              "sfx": "" if dl else (sfx if sfx in SFX_NAMES else "")}
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
        "নিয়ম: প্রতিটা scene-এর IMAGE prompt আলাদা ফাইলে (scene_001_image.txt ...) আর VIDEO prompt আলাদা ফাইলে (scene_001_video.txt ...)। Flow-তে বানানো প্রতিটা clip/ছবির নাম হবে "
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

    all_img = list(head)
    all_vid = list(head)
    scene_texts = {}
    for sc in scenes:
        n3 = f"{sc['n']:03d}"
        ti = scene_image_text(sc, total)
        tv = scene_video_text(sc, total)
        scene_texts[f"scene_{n3}_image.txt"] = ti
        scene_texts[f"scene_{n3}_video.txt"] = tv
        all_img += [f"##### scene_{n3}_image.txt #####", ti, ""]
        all_vid += [f"##### scene_{n3}_video.txt #####", tv, ""]
    open("all_image_prompts.txt", "w", encoding="utf-8").write("\n".join(all_img))
    open("all_video_prompts.txt", "w", encoding="utf-8").write("\n".join(all_vid))
    open("script.txt", "w", encoding="utf-8").write(script)
    open("youtube_package.txt", "w", encoding="utf-8").write(pkg)
    json.dump({"topic": TOPIC, "bible": bible, "scenes": scenes},
              open("scenes.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    for name, t in scene_texts.items():
        open(os.path.join("prompt_files", name), "w", encoding="utf-8").write(t)

    archive_and_clean()
    for f, mime in [("all_image_prompts.txt", "text/plain"), ("all_video_prompts.txt", "text/plain"), ("script.txt", "text/plain"),
                    ("youtube_package.txt", "text/plain"), ("scenes.json", "application/json")]:
        d_upload(f, f, OUT_ID, mime)
        print("Uploaded:", f)
    for k, name in enumerate(scene_texts, 1):
        d_upload(os.path.join("prompt_files", name), name, OUT_ID, "text/plain")
        if k % 10 == 0 or k == total:
            print(f"Uploaded scene prompt: {k}/{total}")
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
        print("AI দিয়ে মেলানো হচ্ছে:", len(todo), "টা ছবি/clip")
        desc = "\n".join(
            f"SCENE {s['n']}: {(s.get('shot') or s['image_prompt'])[:200]} | narration: {s['narration'][:60]}"
            for s in scenes)
        os.makedirs("thumbs", exist_ok=True)
        B = 8
        for i in range(0, len(todo), B):
            batch = todo[i:i + B]
            ask = (
                "নিচে একটা documentary-র scene তালিকা (প্রতিটার shot বর্ণনা সহ) আছে। তারপর কিছু ছবি "
                "(IMAGE 1, 2, ...) দেওয়া আছে, যেগুলো ওই prompt থেকেই AI দিয়ে বানানো। প্রতিটা ছবি "
                "কোন scene-এর সাথে সবচেয়ে বেশি মেলে সেটা বলো। ছবিগুলো সাধারণত scene-এর ক্রমেই "
                "বানানো ও দেওয়া হয়, তাই দুটো scene সমান মিললে ক্রমকে হালকা ইঙ্গিত ধরো। "
                "ছবিটা যদি YouTube thumbnail-এর মতো হয় (বড় text/title সহ) বা কোনো scene-এর সাথেই না মেলে, "
                'scene = 0 দাও। শুধু JSON array দাও: [{"image": 1, "scene": 5}, ...]\n\n' + desc)
            parts = [ask]
            for k, f in enumerate(batch, 1):
                inf = info[f["id"]]
                tp = f"thumbs/{f['id'][:8]}.jpg"
                make_thumb(inf["path"], inf["video"], tp)
                parts += [f"IMAGE {k}:", types.Part.from_bytes(data=open(tp, "rb").read(),
                                                               mime_type="image/jpeg")]
            got = {}
            for attempt in range(3):
                try:
                    data = with_models(TEXT_MODELS, lambda c, m: gen_text(c, m, parts, json_mode=True))
                    got = {int(x["image"]): int(x["scene"]) for x in parse_json(data)}
                    if all(k in got for k in range(1, len(batch) + 1)):
                        break
                except Exception as e:
                    print("মেলানোতে সমস্যা, আবার চেষ্টা:", str(e)[:100])
            for k, f in enumerate(batch, 1):
                if k in got:
                    mapping[f["id"]] = max(0, min(got[k], n_scenes))
                else:
                    pos = media.index(f)
                    mapping[f["id"]] = min(pos + 1, n_scenes)
                    print("AI মেলাতে পারেনি, ক্রম অনুযায়ী ধরা হলো:", f["name"])
            time.sleep(3)

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
    "whoosh": "anoisesrc=d=1.4:c=pink:r=44100:a=0.6,highpass=f=300,lowpass=f=4500,"
              "afade=t=in:d=0.7:curve=qsin,afade=t=out:st=0.7:d=0.7:curve=qsin,aecho=0.8:0.7:60:0.3",
    "boom": "aevalsrc='0.9*sin(2*PI*(38+90*exp(-9*t))*t)*exp(-1.8*t)+0.25*(random(0)*2-1)*exp(-18*t)':s=44100:d=3,lowpass=f=900",
    "riser": "aevalsrc='0.6*(random(0)*2-1)*pow(min(1,t/3),2)':s=44100:d=3,highpass=f=500,lowpass=f=6000,afade=t=out:st=2.8:d=0.2",
    "wind": "anoisesrc=d=7:c=brown:r=44100:a=0.5,lowpass=f=600,afade=t=in:d=2,afade=t=out:st=5:d=2",
    "heartbeat": "aevalsrc='0.9*sin(2*PI*52*t)*exp(-12*t)+0.7*gt(t,0.3)*sin(2*PI*46*t)*exp(-12*(t-0.3))':s=44100:d=1.1,"
                 "aloop=loop=2:size=48510,lowpass=f=300",
}
# scene শুরুর আগে/পরে কত সেকেন্ডে effect বসবে (riser আগে শুরু হয়ে scene-এ পৌঁছে চূড়ায় ওঠে)
SFX_OFF = {"whoosh": -0.15, "boom": 0.0, "riser": -2.6, "wind": 0.0, "heartbeat": 0.0}


def sfx_file(name):
    p = f"sfx_{name}.wav"
    if not os.path.exists(p):
        subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", SFX_SRC[name], "-ar", "44100", "-ac", "2", p],
                       check=True, capture_output=True)
    return p


def build_sfx(scenes, scene_audio, total):
    tags = [s.get("sfx", "") for s in scenes]
    if not any(tags):  # পুরনো project: auto
        tags = [""] * len(scenes)
        if tags:
            tags[0] = "boom"
        for i in range(6, len(tags), 7):
            tags[i] = "whoosh"
    starts, t = [], 0.0
    for seg in scene_audio:
        starts.append(t)
        t += len(seg) / (2 * SR)
    items = [(max(0.0, starts[i] + SFX_OFF[g]), g)
             for i, g in enumerate(tags[:len(starts)]) if g in SFX_SRC]
    if not items:
        return None
    print("SFX:", ", ".join(f"{g}@{at:.0f}s" for at, g in items[:30]), "..." if len(items) > 30 else "")
    cmd, parts = ["ffmpeg", "-y"], []
    for k, (at, g) in enumerate(items):
        cmd += ["-i", sfx_file(g)]
        ms = int(at * 1000)
        parts.append(f"[{k}:a]adelay={ms}|{ms}[s{k}]")
    lab = "".join(f"[s{k}]" for k in range(len(items)))
    parts.append(f"{lab}amix=inputs={len(items)}:normalize=0:duration=longest,"
                 f"apad=whole_dur={total:.2f},atrim=0:{total:.2f}[o]")
    cmd += ["-filter_complex", ";".join(parts), "-map", "[o]", "-ar", "44100", "sfx_track.wav"]
    subprocess.run(cmd, check=True, capture_output=True)
    return "sfx_track.wav"


def final_mix(total, bgm, sfx=None):
    """voice পরিষ্কার করে, music duck করে, sfx যোগ করে YouTube loudness-এ আনে।"""
    fx = (f"[3:a]volume={SFX_VOL},aformat=sample_rates=44100:channel_layouts=stereo[fx];"
          if sfx else "")
    mixin = "[v1][duck][fx]amix=inputs=3" if sfx else "[v1][duck]amix=inputs=2"
    fc = (
        # voice: low-cut + warmth + presence + de-harsh + হালকা compression
        "[1:a]highpass=f=80,equalizer=f=180:t=q:w=1:g=2,equalizer=f=3000:t=q:w=1.2:g=2.5,"
        "equalizer=f=7500:t=q:w=2:g=-3,"
        "acompressor=threshold=-22dB:ratio=2.5:attack=15:release=200:makeup=1.5,"
        "aformat=sample_rates=44100:channel_layouts=stereo,asplit=2[v1][v2];"
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
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest",
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
    clip_files, t_acc, f_acc, gi = [], 0.0, 0, 0
    for si in range(N):
        t_acc += len(scene_audio[si]) / (2 * SR)
        end_f = round(t_acc * FPS)
        frames = max(end_f - f_acc, 12)
        f_acc += frames
        grp = [dlg[si]["info"]] if si in dlg else media_groups[si]
        k = len(grp)
        for j, inf in enumerate(grp):
            part = max(frames // k + (1 if j < frames % k else 0), 12)
            out = f"clips/c{gi:05d}.mp4"
            make_clip(inf["path"], inf["video"], part, out, variant=gi,
                      fade_in=(gi == 0),
                      fade_out=(si == N - 1 and j == k - 1),
                      src_dur=(probe_duration(inf["path"]) if inf["video"] else None))
            clip_files.append(out)
            gi += 1
        if (si + 1) % 5 == 0 or si + 1 == N:
            print(f"Clip তৈরি: {si + 1}/{N} scene")

    with open("list.txt", "w") as lf:
        for c in clip_files:
            lf.write(f"file '{c}'\n")
    subprocess.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", "list.txt",
                    "-c", "copy", "video_noaudio.mp4"], check=True, capture_output=True)

    # ---- music + sfx + mix ----
    subprocess.run(["ffmpeg", "-y", "-i", "voice.wav", "-af", "loudnorm=I=-16:TP=-1.5:LRA=7",
                    "-b:a", "192k", "voiceover.mp3"], check=True, capture_output=True)
    bgm = make_bgm(total + 1, files)
    sfx_scenes = [dict(s, sfx="") if i in dlg else s for i, s in enumerate(scenes)]
    sfx = build_sfx(sfx_scenes, scene_audio, total) if SFX_ON else None
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
                continue
            if zips:
                print("zip আছে কিন্তু scenes.json নেই (আগে prompts mode চালাও); অপেক্ষা...")
            elif AUTO_TOPIC and has_mp4:
                print("আগের video তৈরি আছে → mp4 archive করে নতুন trending topic-এর prompt পাঠানো হচ্ছে")
                TOPIC = ""
                run_prompts()
                fails = 0
                continue
            else:
                print("zip নেই (শুধু prompt আছে); অপেক্ষা...")
        except AllGeminiKeysExhausted as e:
            print(e)
            print(f"{QUOTA_WAIT_MIN} মিনিট পর আবার চেষ্টা (cache-এ রাখা audio নষ্ট হবে না)")
            rot = Rotator(KEYS)
            time.sleep(QUOTA_WAIT_MIN * 60)
            continue
        except Exception as e:
            fails += 1
            print(f"কাজ ব্যর্থ ({fails}/3): {type(e).__name__}: {str(e)[:200]}")
            if fails >= 3:
                sys.exit("৩ বার ব্যর্থ; log দেখে ঠিক করে আবার চালাও")
            rot = Rotator(KEYS)
            time.sleep(300)
            continue
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
