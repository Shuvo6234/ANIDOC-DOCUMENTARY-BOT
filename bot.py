import os, sys, uuid
from pathlib import Path
from urllib.parse import quote
from io import BytesIO

import requests
import fitz
from PIL import Image, ImageDraw
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from googleapiclient.errors import HttpError

PROJECT_DIR = Path(__file__).parent
OUTPUT_DIR = PROJECT_DIR / "output"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
DRIVE_FOLDER_ID = "1tOKfM5wPPIA42gTUzVQUsgm295mzPqeJ"
PDF_NAME = "ANIDOC MAGICAL PROMPT v4.pdf"
POLLINATIONS_URL = "https://gen.pollinations.ai/image/"
MODEL = os.getenv("POLLINATIONS_IMAGE_MODEL", "kontext").strip() or "kontext"
TEMP_NAME = f"anidoc-style-reference-{uuid.uuid4().hex}.jpg"
CONTACT_SHEET = OUTPUT_DIR / "anidoc_pdf_all_pages_reference.jpg"
IMAGE_PATH = OUTPUT_DIR / "image_generation_test.jpg"
TIMEOUT = 300
MAX_PAGES = 24
TILE_W, TILE_H, COLS = 640, 360, 2


def connect_google_drive():
    names = ("GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET", "GOOGLE_OAUTH_REFRESH_TOKEN")
    values = [os.getenv(n, "").strip() for n in names]
    missing = [n for n, v in zip(names, values) if not v]
    if missing:
        raise RuntimeError("Missing GitHub Secret(s): " + ", ".join(missing))
    creds = Credentials(
        token=None, refresh_token=values[2],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=values[0], client_secret=values[1],
        scopes=["https://www.googleapis.com/auth/drive"],
    )
    drive = build("drive", "v3", credentials=creds, cache_discovery=False)
    folder = drive.files().get(fileId=DRIVE_FOLDER_ID, fields="id,name,mimeType").execute()
    if folder.get("mimeType") != "application/vnd.google-apps.folder":
        raise RuntimeError("Configured Google Drive ID is not a folder.")
    print("Google Drive connected. Output folder:", folder.get("name", "Unknown"))
    return drive


def find_pdf():
    exact = PROJECT_DIR / PDF_NAME
    if exact.exists():
        return exact
    candidates = [p for p in PROJECT_DIR.glob("*.pdf")
                  if "anidoc" in p.name.lower() and "magical" in p.name.lower()]
    if candidates:
        return candidates[0]
    raise RuntimeError(f"Could not find {PDF_NAME!r} beside bot.py.")


def build_pdf_contact_sheet():
    pdf_path = find_pdf()
    tiles = []
    with fitz.open(pdf_path) as doc:
        if not len(doc):
            raise RuntimeError("Reference PDF has no pages.")
        count = min(len(doc), MAX_PAGES)
        if len(doc) > MAX_PAGES:
            print(f"WARNING: PDF has {len(doc)} pages; using first {MAX_PAGES}.")
        print(f"Rendering {count} PDF pages to include their visual references.")
        for i in range(count):
            page = doc[i]
            pix = page.get_pixmap(matrix=fitz.Matrix(1.1, 1.1), alpha=False)
            im = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            im.thumbnail((TILE_W, TILE_H))
            tile = Image.new("RGB", (TILE_W, TILE_H + 32), "white")
            tile.paste(im, ((TILE_W-im.width)//2, (TILE_H-im.height)//2))
            ImageDraw.Draw(tile).text((12, TILE_H+8), f"PDF PAGE {i+1}", fill="black")
            tiles.append(tile)
    rows = (len(tiles) + COLS - 1) // COLS
    sheet = Image.new("RGB", (COLS*TILE_W, rows*(TILE_H+32)), (225,225,225))
    for i, tile in enumerate(tiles):
        sheet.paste(tile, ((i % COLS)*TILE_W, (i // COLS)*(TILE_H+32)))
    sheet.thumbnail((1536, 1536))
    sheet.save(CONTACT_SHEET, "JPEG", quality=88, optimize=True)
    print("PDF visual contact sheet:", CONTACT_SHEET.name, f"{sheet.width}x{sheet.height}")
    return CONTACT_SHEET


def upload_temporary_reference(drive, path):
    media = MediaFileUpload(str(path), mimetype="image/jpeg", resumable=True)
    item = drive.files().create(
        body={"name": TEMP_NAME, "parents": [DRIVE_FOLDER_ID]},
        media_body=media, fields="id,name",
    ).execute()
    fid = item["id"]
    try:
        drive.permissions().create(
            fileId=fid, body={"type":"anyone","role":"reader"}, fields="id"
        ).execute()
    except Exception:
        try:
            drive.files().delete(fileId=fid).execute()
        except Exception:
            pass
        raise RuntimeError("Could not temporarily share the PDF reference sheet. Check Drive sharing policy.") from None
    print("Temporary reference sheet uploaded for generation.")
    return fid, f"https://drive.google.com/uc?export=download&id={fid}"


def delete_temporary_reference(drive, fid):
    if not fid:
        return
    try:
        drive.files().delete(fileId=fid).execute()
        print("Temporary public reference deleted.")
    except Exception as exc:
        print("WARNING: Could not delete temporary reference:", type(exc).__name__)


def generate_image(reference_url):
    global IMAGE_PATH
    api_key = os.getenv("POLLINATIONS_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("POLLINATIONS_API_KEY GitHub Secret is missing.")
    prompt = """
Create one original cinematic 2D ANIDOC documentary illustration. Use the
attached visual reference board as the primary art-direction guide. Follow
the shared line quality, character proportions, face rendering, color palette,
shadow treatment, background detail, and serious documentary mood. Do not copy
the PDF layout, its text, or an exact reference composition.

STYLE: Semi-realistic 2D illustrated documentary frame; clean confident hand-
drawn outlines; realistic expressive South Asian faces and believable anatomy;
detailed period clothing; controlled environment detail; muted warm amber,
khaki, dusty olive, earthy brown and deep navy; warm brown/golden skin tones;
one dramatic light source, deep cinematic shadows, restrained atmospheric
depth. Serious, tense, authoritative historical-documentary realism. Premium
professional illustration, not anime, 3D, photograph, children's cartoon, or
glossy game concept art.

SCENE: A historically plausible Indian railway station at night in the 1940s.
A steam locomotive stands on the platform, with two distant human silhouettes
and a railway worker in period clothing. Warm lantern light, subtle mist, deep
navy night shadows, dusty olive and amber tones. Wide establishing shot with
clear foreground, middle ground, and background. It should look like a frame
from the same illustrated documentary series as the supplied references.

Landscape widescreen 16:9 composition, high-detail main subjects, moderate
background detail. No text, captions, letters, logos, watermarks, or borders.
""".strip()
    params = {
        "model": MODEL, "image": reference_url,
        "width": 1536, "height": 864, "seed": -1, "safe": "true",
    }
    try:
        response = requests.get(
            POLLINATIONS_URL + quote(prompt, safe=""),
            headers={"Authorization": f"Bearer {api_key}"},
            params=params, timeout=TIMEOUT,
        )
    except requests.RequestException as exc:
        raise RuntimeError(f"Could not connect to Pollinations ({type(exc).__name__}).") from None
    if response.status_code != 200:
        messages = {
            401: "Pollinations authentication failed. Check the API key.",
            402: "Pollinations reports insufficient Pollen/credits; this may not be free.",
            403: "Pollinations denied access. Check API-key permissions/model access.",
            429: "Pollinations rate limit reached; try again later.",
        }
        raise RuntimeError(messages.get(response.status_code,
                                        f"Pollinations failed with HTTP {response.status_code}."))
    if not response.headers.get("Content-Type", "").lower().startswith("image/"):
        raise RuntimeError("Pollinations returned a non-image response.")
    try:
        with Image.open(BytesIO(response.content)) as im:
            im.verify()
        with Image.open(BytesIO(response.content)) as im:
            fmt, size = (im.format or "").upper(), im.size
    except Exception:
        raise RuntimeError("API response could not be validated as an image.") from None
    ext = ".png" if fmt == "PNG" else ".jpg"
    IMAGE_PATH = OUTPUT_DIR / ("image_generation_test" + ext)
    IMAGE_PATH.write_bytes(response.content)
    print("IMAGE GENERATION SUCCESSFUL:", IMAGE_PATH.name, f"{size[0]}x{size[1]}")
    if size[1] and abs(size[0]/size[1] - 16/9) > 0.03:
        print("WARNING: provider returned an image that is not exactly 16:9.")
    return True


def upload_image_to_drive(drive):
    if not IMAGE_PATH.exists():
        raise RuntimeError("Generated image file does not exist.")
    mime = "image/png" if IMAGE_PATH.suffix.lower() == ".png" else "image/jpeg"
    uploaded = drive.files().create(
        body={"name": IMAGE_PATH.name, "parents": [DRIVE_FOLDER_ID]},
        media_body=MediaFileUpload(str(IMAGE_PATH), mimetype=mime, resumable=True),
        fields="id,name,size",
    ).execute()
    print("IMAGE UPLOAD SUCCESSFUL:", uploaded.get("name"))
    print("Drive file ID:", uploaded.get("id"), "| Size:", uploaded.get("size", "Unknown"))


def main():
    print("=" * 50)
    print("ANIDOC — PDF-WIDE REFERENCE IMAGE TEST")
    print("=" * 50)
    drive, temp_id = None, None
    try:
        drive = connect_google_drive()
        contact_sheet = build_pdf_contact_sheet()
        temp_id, ref_url = upload_temporary_reference(drive, contact_sheet)
        generate_image(ref_url)
        upload_image_to_drive(drive)
        print("TEST PASSED. Compare the uploaded image with the PDF reference style.")
    except HttpError as exc:
        print("Google Drive HTTP error:", getattr(exc.resp, "status", "Unknown"))
        print("Check Drive OAuth access, folder ID, and sharing policy.")
        sys.exit(1)
    except Exception as exc:
        print("TEST FAILED:", type(exc).__name__, str(exc)[:500])
        sys.exit(1)
    finally:
        if drive and temp_id:
            delete_temporary_reference(drive, temp_id)


if __name__ == "__main__":
    main()
