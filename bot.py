import os, re, io, sys, json, wave, time, hashlib, datetime, subprocess, audioop, statistics
from google import genai
from google.genai import types
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload

MODE = (os.getenv("MODE") or "prompts").strip()
TOPIC = (os.getenv("TOPIC") or "").strip()
PROJECT = (os.getenv("PROJECT") or "").strip()
OUT_ID = os.environ["GDRIVE_FOLDER_ID"]
SCENE_CHARS = int(os.getenv("SCENE_CHARS", "350"))  # বড় = কম ছবি লাগবে

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

    def call(self, fn, rounds=6):
        last = None
        for rnd in range(1, rounds + 1):
            tried = 0
            while self.keys and tried < len(self.keys):
                self.i %= len(self.keys)
                no, key = self.keys[self.i]
                client = genai.Client(
                    api_key=key, http_options=types.HttpOptions(timeout=300000))
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
    sents = [s.strip() for s in re.split(r"(?<=[।\.\?\!])\s+", text) if s.strip()]
    scenes, cur = [], ""
    for s in sents:
        if cur and len(cur) + len(s) > limit:
            scenes.append(cur.strip())
            cur = ""
        cur += s + " "
    if cur.strip():
        scenes.append(cur.strip())
    if len(scenes) > 1 and len(scenes[-1]) < limit * 0.3:
        scenes[-2] += " " + scenes.pop()
    return scenes


# =====================================================================
# MODE 1: script + image prompts + youtube package  (audio নেই)
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
    print("মোট scene (= ছবি লাগবে):", len(texts))

    prompts = {}
    B = 12
    for i in range(0, len(texts), B):
        batch = [(i + j + 1, t) for j, t in enumerate(texts[i:i + B])]
        listing = "\n".join(f"SCENE {n}: {t}" for n, t in batch)
        ask = (
            f"Documentary topic: {TOPIC}\n"
            "সংযুক্ত PDF-এ ANIDOC-এর নির্দেশনা আছে। PDF-এ image prompt বানানোর নিয়ম "
            "(style, aspect ratio, ভাষা, visual consistency, কী করা যাবে না) থাকলে তা হুবহু মানো। "
            "না থাকলে English-এ cinematic, realistic, 16:9 prompt লেখো। "
            "নিচের প্রতিটা scene-এর narration-এর সাথে মেলে এমন একটা করে image prompt লেখো, "
            "সব prompt-এ একই visual style রাখো।\n"
            'শুধু JSON array দাও: [{"n": 1, "image_prompt": "..."}] — ঠিক এই scene নম্বরগুলো দিয়ে।\n\n'
            + listing)
        got = {}
        for attempt in range(3):
            try:
                data = with_models(TEXT_MODELS, lambda c, m: gen_text(c, m, [pdf_part, ask], json_mode=True))
                arr = parse_json(data)
                got = {int(x["n"]): str(x["image_prompt"]).strip() for x in arr}
                if all(n in got for n, _ in batch):
                    break
            except Exception as e:
                print("Image prompt parse সমস্যা, আবার চেষ্টা:", str(e)[:100])
        for n, _ in batch:
            if n not in got:
                sys.exit(f"Scene {n}-এর image prompt পাওয়া যায়নি, আবার run করো")
            prompts[n] = got[n]
        print("Image prompt তৈরি:", min(i + B, len(texts)), "/", len(texts))

    scenes = [{"n": n, "narration": t, "image_prompt": prompts[n]}
              for n, t in enumerate(texts, 1)]

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
        "=" * 60, "",
    ]
    for s in scenes:
        lines += [f"SCENE {s['n']:02d}", f"Narration: {s['narration'][:160]}...",
                  f"IMAGE PROMPT: {s['image_prompt']}", ""]
    open("image_prompts.txt", "w", encoding="utf-8").write("\n".join(lines))
    open("script.txt", "w", encoding="utf-8").write(script)
    open("youtube_package.txt", "w", encoding="utf-8").write(pkg)
    json.dump({"topic": TOPIC, "scenes": scenes}, open("scenes.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)

    for f, mime in [("image_prompts.txt", "text/plain"), ("script.txt", "text/plain"),
                    ("youtube_package.txt", "text/plain"), ("scenes.json", "application/json")]:
        d_upload(f, f, proj_id, mime)
        print("Uploaded:", f)
    print(f"\nProject folder: {proj_name}\nএবার ছবি বানিয়ে ওই folder-এ upload করো, তারপর mode=video চালাও।")


# =====================================================================
# MODE 2: audio + sync + edit → final_video.mp4
# =====================================================================
SR, FPS, W, H = 24000, 25, 1920, 1080
STYLE = ("calm, deep and authoritative documentary narrator, "
         "speaking slowly at a steady, measured pace with a serious cinematic tone")
LEGACY_PREFIX = (
    "Read the following text aloud as a deep, calm, authoritative documentary narrator, "
    "slow and steady pace, serious tone. Do not read these instructions:\n")
IMG_EXT = (".png", ".jpg", ".jpeg", ".webp")
VID_EXT = (".mp4", ".mov", ".mkv", ".webm")
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
            lo, hi = max(int(est - 75), a + 1, prev + 1), min(int(est + 75), b - 1)
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
    media = [f for f in files if f["name"].lower().endswith(IMG_EXT + VID_EXT)
             and not f["name"].lower().startswith(("thumb", "final"))]
    if not media:
        sys.exit("project folder-এ কোনো ছবি/video পাওয়া যায়নি")
    media.sort(key=lambda x: x["createdTime"])  # upload-এর ক্রম

    os.makedirs("media", exist_ok=True)
    info = {}
    for f in media:
        p = os.path.join("media", f"{f['id'][:8]}_{re.sub(r'[^\w.]+', '_', f['name'])}")
        d_download(f["id"], p)
        info[f["id"]] = {"name": f["name"], "path": p,
                         "video": f["name"].lower().endswith(VID_EXT)}
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
        desc = "\n".join(f"SCENE {s['n']}: {s['image_prompt'][:220]}" for s in scenes)
        os.makedirs("thumbs", exist_ok=True)
        B = 8
        for i in range(0, len(todo), B):
            batch = todo[i:i + B]
            ask = (
                "নিচে একটা documentary-র scene তালিকা (প্রতিটার image prompt সহ) আছে। তারপর কিছু ছবি "
                "(IMAGE 1, 2, ...) দেওয়া আছে, যেগুলো ওই prompt থেকেই AI দিয়ে বানানো। প্রতিটা ছবি "
                "কোন scene-এর prompt-এর সাথে সবচেয়ে বেশি মেলে সেটা বলো। ছবিগুলো সাধারণত scene-এর ক্রমেই "
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


ENC = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", "-r", str(FPS)]


def make_clip(src, is_video, frames, out, zoom_in):
    dur = frames / FPS
    fd = min(0.3, dur / 3)
    fx = f"fade=t=in:st=0:d={fd:.2f},fade=t=out:st={dur - fd:.2f}:d={fd:.2f}"
    if is_video:
        vf = f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},fps={FPS},setsar=1,{fx}"
        cmd = ["ffmpeg", "-y", "-stream_loop", "-1", "-i", src, "-vf", vf,
               "-frames:v", str(frames), "-an", *ENC, out]
    else:
        z = f"1+0.12*on/{frames}" if zoom_in else f"1.12-0.12*on/{frames}"
        vf = ("scale=2880:1620:force_original_aspect_ratio=increase,crop=2880:1620,setsar=1,"
              f"zoompan=z='{z}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d={frames}:s={W}x{H}:fps={FPS},{fx}")
        cmd = ["ffmpeg", "-y", "-i", src, "-vf", vf, "-frames:v", str(frames), "-an", *ENC, out]
    subprocess.run(cmd, check=True, capture_output=True)


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
        segs[-1] += b"\x00\x00" * int(SR * 0.3)
        scene_audio += segs

    full = b"".join(scene_audio)
    with wave.open("voice.wav", "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(SR)
        w.writeframes(full)
    print(f"মোট audio: {len(full) / (2 * SR) / 60:.1f} মিনিট")

    # ---- প্রতিটা scene-এর clip ----
    os.makedirs("clips", exist_ok=True)
    clip_files, t_acc, f_acc, gi = [], 0.0, 0, 0
    for si, (pcm_seg, grp) in enumerate(zip(scene_audio, media_groups)):
        t_acc += len(pcm_seg) / (2 * SR)
        end_f = round(t_acc * FPS)
        frames = max(end_f - f_acc, 12)
        f_acc += frames
        k = len(grp)
        for j, inf in enumerate(grp):
            part = max(frames // k + (1 if j < frames % k else 0), 12)
            out = f"clips/c{gi:05d}.mp4"
            make_clip(inf["path"], inf["video"], part, out, zoom_in=(gi % 2 == 0))
            clip_files.append(out)
            gi += 1
        if (si + 1) % 5 == 0 or si + 1 == len(scene_audio):
            print(f"Clip তৈরি: {si + 1}/{len(scene_audio)} scene")

    with open("list.txt", "w") as lf:
        for c in clip_files:
            lf.write(f"file '{c}'\n")
    subprocess.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", "list.txt",
                    "-c", "copy", "video_noaudio.mp4"], check=True, capture_output=True)

    subprocess.run(["ffmpeg", "-y", "-i", "voice.wav", "-af", "loudnorm=I=-16:TP=-1.5:LRA=7",
                    "-b:a", "192k", "voiceover.mp3"], check=True, capture_output=True)
    subprocess.run(["ffmpeg", "-y", "-i", "video_noaudio.mp4", "-i", "voice.wav",
                    "-map", "0:v", "-map", "1:a", "-af", "loudnorm=I=-16:TP=-1.5:LRA=7",
                    "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest",
                    "-movflags", "+faststart", "final_video.mp4"], check=True, capture_output=True)

    d_upload("final_video.mp4", "final_video.mp4", pid, "video/mp4")
    print("Uploaded: final_video.mp4")
    d_upload("voiceover.mp3", "voiceover.mp3", pid, "audio/mpeg")
    print("Uploaded: voiceover.mp3")
    try:
        d_delete(cache_id)
    except Exception as e:
        print("cache মোছা যায়নি:", str(e)[:100])


if MODE == "video":
    run_video()
else:
    run_prompts()
