import os, re, io, sys, json, wave, time, hashlib, datetime, subprocess, audioop, statistics, threading, zipfile
from google import genai
from google.genai import types
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload

# log সঙ্গে সঙ্গে দেখানোর জন্য (GitHub Actions-এ নইলে output আটকে থাকে)
sys.stdout.reconfigure(line_buffering=True)
MAX_RUN_MIN = int(os.getenv("MAX_RUN_MIN", "45"))


def _watchdog():
    print(f"!!! {MAX_RUN_MIN} মিনিটে শেষ হয়নি, বন্ধ করা হলো (Gemini key/quota বা network সমস্যা হতে পারে)")
    os._exit(2)


_wd = threading.Timer(MAX_RUN_MIN * 60, _watchdog)
_wd.daemon = True
_wd.start()

MODE = (os.getenv("MODE") or "prompts").strip()
TOPIC = (os.getenv("TOPIC") or "").strip()
PROJECT = (os.getenv("PROJECT") or "").strip()
OUT_ID = os.environ["GDRIVE_FOLDER_ID"]

# প্রতি scene ≈ 4-6 সেকেন্ড। ~12 অক্ষর/সেকেন্ড ধরে 60 অক্ষর ≈ 5 সেকেন্ড।
# video mode-এর log-এ "scene দৈর্ঘ্য" দেখে দরকারে এটা বদলাও (ছোট = দ্রুত scene change)।
SCENE_CHARS = int(os.getenv("SCENE_CHARS", "60"))
IMAGE_ASPECT = os.getenv("IMAGE_ASPECT", "16:9")

# ---- music / mix সেটিং (সব env দিয়ে বদলানো যাবে) ----
BGM_MOOD = os.getenv("BGM_MOOD", "cinematic")   # cinematic | dark | calm | epic
BGM_VOL = float(os.getenv("BGM_VOL", "0.45"))   # 0.2 (খুব মৃদু) - 0.8 (জোরালো)
LUFS = os.getenv("LUFS", "-14")                 # YouTube-এর জন্য -14
# ---- নীরবতা কমানো ----
MAX_PAUSE = float(os.getenv("MAX_PAUSE", "0.30"))        # লম্বা বিরতি ছেঁটে এতটা রাখে (সেকেন্ড)
MIN_PAUSE_RUN = float(os.getenv("MIN_PAUSE_RUN", "0.45"))  # এর চেয়ে লম্বা নীরবতাই শুধু ছাঁটা হবে

PDF_PATH = "ANIDOC MAGICAL PROMPT v4.pdf"
TEXT_MODELS = os.getenv(
    "TEXT_MODELS", "gemini-3.8-flash,gemini-3.7-flash,gemini-2.5-flash"
).split(",")
TTS_MODELS = os.getenv(
    "TTS_MODELS",
    "gemini-3.8-flash-tts,gemini-3.8-flash-lite-tts,"
    "gemini-3.1-flash-tts-preview,gemini-2.5-pro-preview-tts",
).split(",")
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


# ---------- Gemini key rotation ----------
class Rotator:
    def __init__(self, keys):
        self.keys = list(enumerate(keys, 1))
        self.i = 0

    def call(self, fn, rounds=int(os.getenv("GEMINI_ROUNDS", "3"))):
        last = None
        for rnd in range(1, rounds + 1):
            tried = 0
            while self.keys and tried < len(self.keys):
                self.i %= len(self.keys)
                no, key = self.keys[self.i]
                client = genai.Client(
                    api_key=key, http_options=types.HttpOptions(timeout=int(os.getenv("GEMINI_TIMEOUT_MS", "120000"))))
                print(f"→ Gemini call, key {no}/{TOTAL}, round {rnd}")
                try:
                    return fn(client)
                except Exception as e:
                    msg = str(e).lower()
                    print(f"[round {rnd}] Key {no}/{TOTAL}: {type(e).__name__}: {str(e)[:120]}")
                    if any(w in msg for w in MODEL_GONE):
                        raise
                    if any(w in msg for w in KEY_DEAD):
                        print(f"Key {no} বাদ (permission/billing সমস্যা)")
                        self.keys.pop(self.i)
                        last = e
                        continue
                    if any(w in msg for w in RETRY_WORDS):
                        last = e
                        self.i += 1
                        tried += 1
                        time.sleep(2)
                        continue
                    raise
            if not self.keys:
                break
            wait = min(30 * rnd, 120)
            print(f"সব key এখন ব্যস্ত, {wait} সেকেন্ড অপেক্ষা করে আবার চেষ্টা...")
            time.sleep(wait)
        raise RuntimeError("সব Gemini key ব্যর্থ হয়েছে") from last


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


def parse_json(text):
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    return json.loads(text)


def split_scenes(text, limit):
    """narration-কে ~limit অক্ষরের (≈4-6 সেকেন্ড) scene-এ ভাগ করে।
    আগে বাক্য, তারপর কমা, শেষে শব্দ ধরে কাটে — যাতে কাটার জায়গায় স্বাভাবিক বিরতি থাকে।"""
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
    """পুরো story পড়ে একবারই fixed visual description বানায়। পরে প্রতিটা image prompt-এ
    এই description হুবহু বসানো হয়, তাই চেহারা-পোশাক-দোকান সব scene-এ এক থাকে।"""
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
    """shot (scene-এর action) + fixed bible description = পূর্ণ, বিস্তারিত image prompt।"""
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
def run_prompts():
    if not TOPIC:
        sys.exit("prompts mode-এ topic দিতে হবে")
    pdf_part = types.Part.from_bytes(data=open(PDF_PATH, "rb").read(), mime_type="application/pdf")

    def make_script(client, model):
        prompt = (
            "সংযুক্ত PDF-এ ANIDOC-এর নির্দেশনা আছে। সেটা অনুসরণ করে "
            f"এই বিষয়ে পূর্ণ documentary script লেখো: {TOPIC}\n\n" + LANG_RULE + "\n\n" + NO_TAG_RULE)
        return gen_text(client, model, [pdf_part, prompt])

    script = strip_tags(with_models(TEXT_MODELS, make_script))
    print("Script:", len(script), "chars")

    def make_narration(client, model):
        prompt = (
            "নিচের script থেকে শুধু narrator-এর বলার লাইনগুলো বের করো। Scene heading, timestamp, "
            "[ ] বা < > নির্দেশ, visual/music notes, markdown চিহ্ন — সব বাদ। হিন্দি শব্দ দেবনাগরীতে, "
            "English শব্দ English অক্ষরে থাকবে। প্রতিটা বাক্য শেষে দাঁড়ি (।) বা full stop দাও। "
            "শুধু পরিষ্কার narration text দাও।\n\n" + script)
        return gen_text(client, model, prompt)

    narration = with_models(TEXT_MODELS, make_narration).strip()
    narration = re.sub(r"[*#_`]+", "", narration)
    narration = strip_tags(re.sub(r"\[[^\]\n]{1,60}\]", "", narration)).strip()

    texts = split_scenes(narration, SCENE_CHARS)
    print("মোট scene (= ছবি লাগবে):", len(texts), f"(≈ {len(texts) * 5 / 60:.1f} মিনিট, 5 সেকেন্ড/scene ধরে)")

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
            "সংযুক্ত PDF-এ ANIDOC-এর নির্দেশনা আছে (image-এর নিয়ম থাকলে মানো)। "
            "প্রতিটা scene মাত্র ৪-৬ সেকেন্ডের একটা আলাদা shot। প্রতিটার জন্য লেখো:\n"
            '- "shot": ইংরেজিতে 50-80 শব্দের বিস্তারিত বর্ণনা: narration-এর সাথে মেলে এমন action/ঘটনা, '
            "shot type (wide/medium/close-up/over-the-shoulder/aerial/low-angle...), camera angle, lens, "
            "lighting, mood, foreground ও background, composition। পাশের scene-এর চেয়ে angle বা subject আলাদা হবে। "
            "চরিত্রদের পোশাক/চেহারা এখানে লিখবে না (সেটা আমি নিজে বসাব); চরিত্রকে শুধু নাম দিয়ে উল্লেখ করো।\n"
            '- "characters": এই shot-এ যে character ID-গুলো দেখা যাবে\n'
            '- "location": location ID (না থাকলে "")\n'
            '- "props": দৃশ্যমান prop ID-র তালিকা\n'
            f"Characters: {cids}\nLocations: {lids}\nProps: {pids}\n"
            "শুধু ঐ ID-গুলোই ব্যবহার করো, নতুন বানাবে না।\n"
            'শুধু JSON array: [{"n": 1, "shot": "...", "characters": ["C1"], "location": "L1", "props": []}] '
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
                sys.exit(f"Scene {n}-এর image prompt পাওয়া যায়নি, আবার run করো")
            shots[n] = got[n]
        print("Image prompt তৈরি:", min(i + B, len(texts)), "/", len(texts))

    scenes = []
    for n, t in enumerate(texts, 1):
        s = shots[n]
        sc = {"n": n, "narration": t, "shot": str(s["shot"]).strip(),
              "characters": [str(x) for x in (s.get("characters") or [])],
              "location": str(s.get("location") or ""),
              "props": [str(x) for x in (s.get("props") or [])]}
        sc["image_prompt"] = compose_prompt(bible, sc)
        scenes.append(sc)

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

    stamp = datetime.datetime.utcnow().strftime("%Y%m%d_%H%M")
    slug = re.sub(r"[^\w]+", "_", TOPIC)[:30].strip("_") or "anidoc"
    proj_name = f"{stamp}_{slug}"
    proj_id = d_mkdir(proj_name, OUT_ID)

    lines = [
        f"TOPIC: {TOPIC}",
        f"মোট scene: {len(scenes)}",
        "ছবিগুলো এই project folder-এ upload করো: " + proj_name,
        "ছবির নাম বদলানোর দরকার নেই; bot নিজেই বুঝে নেবে কোন ছবি কোন scene-এর।",
        "Thumbnail ছবিও এখানেই দিতে পারো (bot সেটা video থেকে বাদ দেবে)।",
        "নিজের background music চাইলে 'bgm.mp3' নামে এই folder-এ দাও (না দিলে bot নিজে বানাবে)।",
        "=" * 60, "", "VISUAL BIBLE (সব prompt-এ এটা আপনা-আপনি বসানো আছে)",
        "STYLE: " + bible["style"], "PALETTE: " + str(bible.get("palette", "")), "",
    ]
    for kind in ("characters", "locations", "props"):
        for x in bible[kind]:
            lines.append(f"[{x['id']}] {x.get('name', '')}: {x['desc']}")
    lines += ["", "=" * 60, ""]
    for s in scenes:
        lines += [f"SCENE {s['n']:02d}", f"Narration: {s['narration'][:160]}",
                  f"IMAGE PROMPT: {s['image_prompt']}", ""]
    open("image_prompts.txt", "w", encoding="utf-8").write("\n".join(lines))
    open("script.txt", "w", encoding="utf-8").write(script)
    open("youtube_package.txt", "w", encoding="utf-8").write(pkg)
    json.dump({"topic": TOPIC, "bible": bible, "scenes": scenes},
              open("scenes.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    for f, mime in [("image_prompts.txt", "text/plain"), ("script.txt", "text/plain"),
                    ("youtube_package.txt", "text/plain"), ("scenes.json", "application/json")]:
        d_upload(f, f, proj_id, mime)
        print("Uploaded:", f)
    print(f"\nProject folder: {proj_name}\nএবার ছবি বানিয়ে ওই folder-এ upload করো, তারপর mode=video চালাও।")


# =====================================================================
# MODE 2: audio + sync + edit + music → final_video.mp4
# =====================================================================
SR, FPS, W, H = 24000, 25, 1920, 1080
STYLE = ("calm, deep and authoritative documentary narrator, "
         "speaking slowly at a steady, measured pace with a serious cinematic tone")
LEGACY_PREFIX = (
    "Read the following text aloud as a deep, calm, authoritative documentary narrator, "
    "slow and steady pace, serious tone. Do not read these instructions:\n")
IMG_EXT = (".png", ".jpg", ".jpeg", ".webp")
VID_EXT = (".mp4", ".mov", ".mkv", ".webm")
AUD_EXT = (".mp3", ".wav", ".m4a", ".aac")
NUM_RE = re.compile(r"^(?:scene[_\- ]?)?(\d{1,3})(?=\D|$)", re.I)


def to_pcm(data):
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
                config={"response_modalities": ["AUDIO"],
                        "speech_config": {"voice_config": {"voice": VOICE}}},
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
        model=model, contents=prefix + text,
        config=types.GenerateContentConfig(
            response_modalities=["AUDIO"],
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=VOICE)))),
    )
    return to_pcm(r.candidates[0].content.parts[0].inline_data.data)


def normalize(pcm, target=3500):
    rms = audioop.rms(pcm, 2)
    return pcm if rms == 0 else audioop.mul(pcm, 2, min(target / rms, 4.0))


def frame_rms(pcm):
    fl = int(SR * 0.02) * 2
    return [audioop.rms(pcm[i:i + fl], 2) for i in range(0, len(pcm) - fl + 1, fl)]


def active_seconds(pcm, thr=300):
    return max(sum(1 for v in frame_rms(pcm) if v > thr) * 0.02, 0.1)


def speech_chars(t):
    return len(re.sub(r"\s+", "", t))


def trim_pauses(pcm, thr=300, min_run=None, keep=None):
    """TTS-এর লম্বা নীরবতা ছোট করে। min_run সেকেন্ডের বেশি লম্বা নীরবতা কেটে keep সেকেন্ড রাখে
    (দুই পাশে অর্ধেক করে, যাতে কথা কেটে না যায়)। শুরু ও শেষের নীরবতাও ছাঁটে।"""
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
            k = min(run, int(0.08 / 0.02))          # শুরু/শেষে সর্বোচ্চ 0.08 সেকেন্ড
            seg = pcm[i * fl:(i + k) * fl] if i == 0 else pcm[(j - k) * fl:j * fl]
        elif run > min_f:
            h = keep_f // 2
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
    """এক block-এর audio-কে scene অনুযায়ী কাটে: অক্ষর-অনুপাতে আন্দাজ করে, তারপর কাছের নীরবতায় কাটে।"""
    if len(texts) == 1:
        return [pcm]
    r = frame_rms(pcm)
    act = [i for i, v in enumerate(r) if v > 300]
    fl = int(SR * 0.02) * 2
    if not act:
        n = len(r)
        cuts = [n * (k + 1) // len(texts) for k in range(len(texts) - 1)]
    else:
        a, b = act[0], act[-1] + 1
        chars = [speech_chars(t) for t in texts]
        total, cum, cuts, prev = sum(chars), 0, [], 0
        for c in chars[:-1]:
            cum += c
            est = a + (b - a) * cum / total
            # scene ছোট (4-6 সেকেন্ড) তাই খোঁজার জানালা ±1.0 সেকেন্ড
            lo, hi = max(int(est - 50), a + 1, prev + 1), min(int(est + 50), b - 1)
            best, bi = None, int(est)
            for i in range(lo, max(lo + 1, hi)):
                w = r[i:i + 5]
                if not w:
                    break
                v = sum(w) / len(w)
                if best is None or v < best - 1e-9 or (abs(v - best) < 1e-9 and abs(i - est) < abs(bi - est)):
                    best, bi = v, i + 2
            bi = max(bi, prev + 1)
            cuts.append(bi)
            prev = bi
    bounds = [0] + [c * fl for c in cuts] + [len(pcm)]
    return [pcm[bounds[k]:bounds[k + 1]] for k in range(len(texts))]


def find_project():
    for f in d_list(OUT_ID, f"and mimeType='{FOLDER_MIME}'"):  # নতুন আগে
        if PROJECT and PROJECT.lower() not in f["name"].lower():
            continue
        files = d_list(f["id"])
        names = {x["name"] for x in files}
        if "scenes.json" not in names:
            continue
        if not PROJECT and "final_video.mp4" in names:
            continue
        return f, files
    sys.exit("উপযুক্ত project folder পাওয়া যায়নি (scenes.json থাকা folder লাগবে)")


def make_thumb(path, is_video, out):
    pre = ["-ss", "0.5"] if is_video else []
    subprocess.run(["ffmpeg", "-y", *pre, "-i", path, "-frames:v", "1",
                    "-vf", "scale=512:-2", "-q:v", "4", out],
                   check=True, capture_output=True)


def match_media(files, scenes):
    """ছবি/clip নামিয়ে AI দিয়ে scene-এর সাথে মেলায়। রিটার্ন: প্রতি scene-এর জন্য [ {path, video, name} ]"""
    n_scenes = len(scenes)
    def ok_name(nm):
        low = nm.lower()
        return low.endswith(IMG_EXT + VID_EXT) and not low.startswith(("thumb", "final", "bgm", "."))

    def nat(nm):
        return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", nm)]

    os.makedirs("media", exist_ok=True)
    media = [f for f in files if ok_name(f["name"])]
    info = {}
    for f in media:
        safe = re.sub(r"[^\w.]+", "_", f["name"])
        p = os.path.join("media", f"{f['id'][:8]}_{safe}")
        d_download(f["id"], p)
        info[f["id"]] = {"name": f["name"], "path": p,
                         "video": f["name"].lower().endswith(VID_EXT)}

    # ---- zip ফাইল: নামিয়ে খুলে ভেতরের ছবি/clip যোগ করা ----
    seen_names = {f["name"] for f in media}
    for z in [f for f in files if f["name"].lower().endswith(".zip")]:
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
                if nm in seen_names:  # ভিন্ন subfolder-এ একই নাম থাকলে আলাদা করা
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
        sys.exit("project folder-এ কোনো ছবি/video (বা zip-এর ভেতরে) পাওয়া যায়নি")
    media.sort(key=lambda x: (x["createdTime"], nat(x["name"])))  # upload-এর ক্রম, তারপর নামের ক্রম
    print("মিডিয়া download:", len(media), "টা ফাইল")

    # আগের map (হাতে ঠিক করা থাকলে সেটাই)
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
                else:  # AI ব্যর্থ হলে upload-ক্রম অনুযায়ী
                    pos = media.index(f)
                    mapping[f["id"]] = min(pos + 1, n_scenes)
                    print("AI মেলাতে পারেনি, ক্রম অনুযায়ী ধরা হলো:", f["name"])
            time.sleep(3)

    # map সেভ (নিজে সংশোধন করা যাবে) + log-এ দেখানো
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
        sys.exit("কোনো ছবি কোনো scene-এর সাথে মেলেনি")
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
    return result


# ---------------------------------------------------------------------
# Video clip: Ken Burns (4 রকম move) — scene-এর মাঝে hard cut, শুধু শুরু/শেষে fade
# ---------------------------------------------------------------------
ENC = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", "-r", str(FPS)]


def make_clip(src, is_video, frames, out, variant, fade_in=False, fade_out=False):
    dur = frames / FPS
    fx = []
    if fade_in:
        fx.append("fade=t=in:st=0:d=0.8")
    if fade_out:
        fx.append(f"fade=t=out:st={max(dur - 1.5, 0):.2f}:d=1.5")
    fxs = ("," + ",".join(fx)) if fx else ""
    if is_video:
        vf = f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},fps={FPS},setsar=1{fxs}"
        cmd = ["ffmpeg", "-y", "-stream_loop", "-1", "-i", src, "-vf", vf,
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
        vf = ("scale=2880:1620:force_original_aspect_ratio=increase,crop=2880:1620,setsar=1,"
              f"zoompan=z='{z}':x='{x}':y='{y}':d={frames}:s={W}x{H}:fps={FPS}{fxs}")
        cmd = ["ffmpeg", "-y", "-i", src, "-vf", vf, "-frames:v", str(frames), "-an", *ENC, out]
    subprocess.run(cmd, check=True, capture_output=True)


# ---------------------------------------------------------------------
# Background music: নিজের bgm ফাইল, নইলে ffmpeg দিয়ে cinematic score তৈরি
# (pad + sub bass + chord-change boom + pluck arpeggio + reverb + stereo width)
# ---------------------------------------------------------------------
MOODS = {
    "cinematic": [(57, "m"), (53, "M"), (48, "M"), (55, "M")],   # Am F C G
    "dark":      [(48, "m"), (44, "M"), (51, "M"), (46, "M")],   # Cm Ab Eb Bb
    "calm":      [(48, "M"), (55, "M"), (57, "m"), (53, "M")],   # C G Am F
    "epic":      [(50, "m"), (46, "M"), (53, "M"), (48, "M")],   # Dm Bb F C
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
        for n in notes:                       # pad: detune করা দুটো sine + হালকা harmonic
            f = _mf(n)
            for det in (0.996, 1.004):
                add(f"0.05*sin(2*PI*{f * det:.3f}*t)*{env}", D, at)
            add(f"0.015*sin(2*PI*{f * 2:.3f}*t)*{env}", D, at)
        add(f"0.16*sin(2*PI*{_mf(root - 12):.3f}*t)*min(1,t/2)*min(1,({D}-t)/2)", D, at)   # sub bass
        add("0.55*sin(2*PI*(42+70*exp(-9*t))*t)*exp(-1.6*t)", 4.0, at)                    # deep boom
        arp = [notes[0] + 12, notes[1] + 12, notes[2] + 12, notes[3] + 12, notes[2] + 12, notes[1] + 12]
        for k, n in enumerate(arp):           # pluck arpeggio
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
    """রিটার্ন: ঠিক dur সেকেন্ডের (loop + fade সহ) music ফাইলের path।"""
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


def final_mix(total, bgm):
    """voice-কে পরিষ্কার করে, music duck করে, mix করে YouTube loudness-এ আনে।"""
    fc = (
        # voice: low-cut + হালকা compression
        "[1:a]highpass=f=70,acompressor=threshold=-20dB:ratio=3:attack=10:release=150,"
        "aformat=sample_rates=44100:channel_layouts=stereo,asplit=2[v1][v2];"
        # music: ভলিউম + voice-এর frequency-তে জায়গা ছাড়া
        f"[2:a]volume={BGM_VOL},equalizer=f=1500:t=q:w=1:g=-4,"
        "aformat=sample_rates=44100:channel_layouts=stereo[m];"
        # voice বললে music আপনা-আপনি নিচে নামে, বিরতিতে আবার ওঠে
        "[m][v2]sidechaincompress=threshold=0.02:ratio=6:attack=30:release=600[duck];"
        "[v1][duck]amix=inputs=2:duration=first:normalize=0,"
        f"loudnorm=I={LUFS}:TP=-1.5:LRA=9,afade=t=out:st={max(total - 2, 0):.2f}:d=2[a]")
    subprocess.run(["ffmpeg", "-y", "-i", "video_noaudio.mp4", "-i", "voice.wav", "-i", bgm,
                    "-filter_complex", fc, "-map", "0:v", "-map", "[a]",
                    "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest",
                    "-movflags", "+faststart", "final_video.mp4"], check=True, capture_output=True)


def run_video():
    proj, files = find_project()
    print("Project:", proj["name"])
    pid = proj["id"]
    sid = next(f["id"] for f in files if f["name"] == "scenes.json")
    d_download(sid, "scenes.json")
    scenes = json.load(open("scenes.json", encoding="utf-8"))["scenes"]

    # ---- ছবি মেলানো (audio-র আগে, যাতে ভুল হলে আগেই ধরা পড়ে) ----
    media, info, mapping, old_map_id = match_media(files, scenes)
    media_groups = build_groups(media, info, mapping, len(scenes))
    if old_map_id:
        try:
            d_delete(old_map_id)
        except Exception as e:
            print("পুরনো image_map মোছা যায়নি:", str(e)[:100])
    d_upload("image_map.json", "image_map.json", pid, "application/json")

    # ---- audio: block অনুযায়ী, cache সহ ----
    blocks, cur = [], []
    for s in scenes:
        if cur and sum(len(x["narration"]) for x in cur) + len(s["narration"]) > 1000:
            blocks.append(cur)
            cur = []
        cur.append(s)
    if cur:
        blocks.append(cur)

    cache_id = next((f["id"] for f in files if f["name"] == "_audio_cache"), None) or d_mkdir("_audio_cache", pid)
    cache = {f["name"]: f["id"] for f in d_list(cache_id)}

    raw = []
    for bi, blk in enumerate(blocks):
        text = " ".join(s["narration"] for s in blk)
        key = f"blk_{bi:03d}_{hashlib.md5((text + VOICE + STYLE).encode()).hexdigest()[:8]}.pcm"
        if key in cache:
            d_download(cache[key], "blk.pcm")
            pcm = open("blk.pcm", "rb").read()
            print(f"Audio block {bi + 1}/{len(blocks)} (cache থেকে)")
        else:
            print(f"Audio block {bi + 1}/{len(blocks)}")
            pcm = normalize(with_models(TTS_MODELS, lambda c, m, t=text: tts(c, m, t)))
            open("blk.pcm", "wb").write(pcm)
            d_upload("blk.pcm", key, cache_id, "application/octet-stream")
            time.sleep(5)
        raw.append(pcm)

    before = sum(len(p) for p in raw) / (2 * SR)
    raw = [trim_pauses(p) for p in raw]
    after = sum(len(p) for p in raw) / (2 * SR)
    print(f"নীরবতা ছাঁটা: {before:.0f}s -> {after:.0f}s ({before - after:.0f}s কমেছে)")

    # ---- গতি সমান করা ----
    texts_b = [" ".join(s["narration"] for s in blk) for blk in blocks]
    rates = [speech_chars(t) / active_seconds(p) for t, p in zip(texts_b, raw)]
    target = statistics.median(rates)
    print("গতি (অক্ষর/সেকেন্ড):", [round(r, 1) for r in rates], "target:", round(target, 1))

    scene_audio = []
    for blk, pcm, r in zip(blocks, raw, rates):
        factor = max(0.8, min(1.25, target / r))
        if abs(factor - 1) > 0.03:
            pcm = change_tempo(pcm, factor)
        segs = split_block(pcm, [s["narration"] for s in blk])
        segs[-1] += b"\x00\x00" * int(SR * 0.2)
        scene_audio += segs

    durs = [len(s) / (2 * SR) for s in scene_audio]
    print(f"scene দৈর্ঘ্য: min {min(durs):.1f}s, গড় {statistics.mean(durs):.1f}s, max {max(durs):.1f}s "
          f"(লক্ষ্য 4-6s; গড় বেশি হলে SCENE_CHARS কমাও, কম হলে বাড়াও)")

    full = b"".join(scene_audio)
    with wave.open("voice.wav", "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(SR)
        w.writeframes(full)
    total = len(full) / (2 * SR)
    print(f"মোট audio: {total / 60:.1f} মিনিট")

    # ---- প্রতিটা scene-এর clip ----
    os.makedirs("clips", exist_ok=True)
    clip_files, t_acc, f_acc, gi = [], 0.0, 0, 0
    n_total_scenes = len(scene_audio)
    for si, (pcm_seg, grp) in enumerate(zip(scene_audio, media_groups)):
        t_acc += len(pcm_seg) / (2 * SR)
        end_f = round(t_acc * FPS)
        frames = max(end_f - f_acc, 12)
        f_acc += frames
        k = len(grp)
        for j, inf in enumerate(grp):
            part = max(frames // k + (1 if j < frames % k else 0), 12)
            out = f"clips/c{gi:05d}.mp4"
            make_clip(inf["path"], inf["video"], part, out, variant=gi,
                      fade_in=(gi == 0),
                      fade_out=(si == n_total_scenes - 1 and j == k - 1))
            clip_files.append(out)
            gi += 1
        if (si + 1) % 5 == 0 or si + 1 == n_total_scenes:
            print(f"Clip তৈরি: {si + 1}/{n_total_scenes} scene")

    with open("list.txt", "w") as lf:
        for c in clip_files:
            lf.write(f"file '{c}'\n")
    subprocess.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", "list.txt",
                    "-c", "copy", "video_noaudio.mp4"], check=True, capture_output=True)

    # ---- music + mix ----
    subprocess.run(["ffmpeg", "-y", "-i", "voice.wav", "-af", "loudnorm=I=-16:TP=-1.5:LRA=7",
                    "-b:a", "192k", "voiceover.mp3"], check=True, capture_output=True)
    bgm = make_bgm(total + 1, files)
    final_mix(total, bgm)
    subprocess.run(["ffmpeg", "-y", "-i", bgm, "-b:a", "192k", "background_music.mp3"],
                   check=True, capture_output=True)

    d_upload("final_video.mp4", "final_video.mp4", pid, "video/mp4")
    print("Uploaded: final_video.mp4")
    d_upload("voiceover.mp3", "voiceover.mp3", pid, "audio/mpeg")
    print("Uploaded: voiceover.mp3")
    d_upload("background_music.mp3", "background_music.mp3", pid, "audio/mpeg")
    print("Uploaded: background_music.mp3")
    try:
        d_delete(cache_id)
    except Exception as e:
        print("cache মোছা যায়নি:", str(e)[:100])


if MODE == "video":
    run_video()
else:
    run_prompts()
