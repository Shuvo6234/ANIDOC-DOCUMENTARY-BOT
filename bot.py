import os
import sys
import json
import uuid
from pathlib import Path
from urllib.parse import quote

import requests
import fitz  # PyMuPDF
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from googleapiclient.errors import HttpError

# ============================================================
# CONFIGURATION
# ============================================================

PROJECT_DIR = Path(__file__).parent
OUTPUT_DIR = PROJECT_DIR / "output"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

DRIVE_FOLDER_ID = "1tOKfM5wPPIA42gTUzVQUsgm295mzPqeJ"

POLLINATIONS_IMAGE_URL = "https://gen.pollinations.ai/image/"
POLLINATIONS_MODEL = os.getenv("POLLINATIONS_IMAGE_MODEL", "kontext").strip() or "kontext"

REFERENCE_PDF_NAME = "ANIDOC MAGICAL PROMPT v4.pdf"
REFERENCE_TEMP_NAME = f"anidoc-style-reference-{uuid.uuid4().hex}.png"
IMAGE_PATH = OUTPUT_DIR / "image_generation_test.jpg"

TIMEOUT_SECONDS = 300


# ============================================================
# GOOGLE DRIVE OAUTH
# ============================================================

def connect_google_drive():
    client_id = os.getenv("GOOGLE_OAUTH_CLIENT_ID", "").strip()
    client_secret = os.getenv("GOOGLE_OAUTH_CLIENT_SECRET", "").strip()
    refresh_token = os.getenv("GOOGLE_OAUTH_REFRESH_TOKEN", "").strip()

    missing = [
        name for name, value in (
            ("GOOGLE_OAUTH_CLIENT_ID", client_id),
            ("GOOGLE_OAUTH_CLIENT_SECRET", client_secret),
            ("GOOGLE_OAUTH_REFRESH_TOKEN", refresh_token),
        ) if not value
    ]
    if missing:
        raise RuntimeError("Missing GitHub Secret(s): " + ", ".join(missing))

    credentials = Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=client_id,
        client_secret=client_secret,
        scopes=["https://www.googleapis.com/auth/drive"],
    )

    drive = build("drive", "v3", credentials=credentials, cache_discovery=False)

    folder = drive.files().get(
        fileId=DRIVE_FOLDER_ID,
        fields="id,name,mimeType",
    ).execute()

    if folder.get("mimeType") != "application/vnd.google-apps.folder":
        raise RuntimeError("Configured Google Drive ID is not a folder.")

    print("Google Drive connected.")
    print("Output folder:", folder.get("name", "Unknown"))
    return drive


# ============================================================
# LOAD PDF REFERENCE IMAGE
# ============================================================

def render_reference_page():
    pdf_path = PROJECT_DIR / REFERENCE_PDF_NAME

    if not pdf_path.exists():
        candidates = [
            p for p in PROJECT_DIR.glob("*.pdf")
            if "anidoc" in p.name.lower() and "magical" in p.name.lower()
        ]
        if not candidates:
            raise RuntimeError(
                f"Could not find {REFERENCE_PDF_NAME!r} beside bot.py."
            )
        pdf_path = candidates[0]

    try:
        with fitz.open(pdf_path) as document:
            if len(document) < 4:
                raise RuntimeError("Reference PDF has fewer than 4 pages.")
            # Page 4 is the previously configured AniDoc style-reference page.
            page = document[3]
            pixmap = page.get_pixmap(matrix=fitz.Matrix(1.5, 1.5), alpha=False)
            image_bytes = pixmap.tobytes("png")
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(
            f"Could not render reference PDF ({type(exc).__name__})."
        ) from None

    local_path = OUTPUT_DIR / REFERENCE_TEMP_NAME
    local_path.write_bytes(image_bytes)
    print("Rendered PDF style reference page 4.")
    return local_path


# ============================================================
# TEMPORARY REFERENCE HOSTING ON GOOGLE DRIVE
# Pollinations' documented image-to-image parameter expects a URL,
# not the base64 image payload used by the old Cloudflare code.
# The reference image is briefly shared publicly so the API can fetch it,
# then its public permission is removed in the cleanup function.
# ============================================================

def upload_temporary_reference(drive, reference_path):
    metadata = {
        "name": REFERENCE_TEMP_NAME,
        "parents": [DRIVE_FOLDER_ID],
    }
    media = MediaFileUpload(str(reference_path), mimetype="image/png", resumable=True)
    item = drive.files().create(
        body=metadata,
        media_body=media,
        fields="id,name,webViewLink",
    ).execute()

    file_id = item["id"]
    try:
        drive.permissions().create(
            fileId=file_id,
            body={"type": "anyone", "role": "reader"},
            fields="id",
        ).execute()
    except Exception:
        try:
            drive.files().delete(fileId=file_id).execute()
        except Exception:
            pass
        raise RuntimeError(
            "Could not temporarily share the reference image for the image API. "
            "Check whether your Google Drive policy allows public-link sharing."
        ) from None

    reference_url = f"https://drive.google.com/uc?export=download&id={file_id}"
    print("Temporary reference image uploaded and shared for generation.")
    return file_id, reference_url


def delete_temporary_reference(drive, file_id):
    if not file_id:
        return
    try:
        # Delete the temporary file, which also removes its public permission.
        drive.files().delete(fileId=file_id).execute()
        print("Temporary reference image deleted from Drive.")
    except Exception as exc:
        print(
            "WARNING: Could not delete temporary reference image. "
            f"Please remove it from Drive manually (error: {type(exc).__name__})."
        )


# ============================================================
# POLLINATIONS IMAGE GENERATION
# ============================================================

def generate_image(reference_url):
    api_key = os.getenv("POLLINATIONS_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("POLLINATIONS_API_KEY GitHub Secret is missing.")

    prompt = """
Create a NEW scene. Use the supplied image only as a visual STYLE reference;
do not copy its characters, objects, or exact composition.

ANIDOC DOCUMENTARY VISUAL STYLE:
Semi-realistic 2D documentary illustration, polished hand-drawn animation,
clean confident outlines, realistic expressive faces and believable anatomy,
carefully detailed clothing and environment, cinematic staging.
Muted warm amber, khaki, dusty olive, earthy brown, and deep navy palette.
Dramatic single-source cinematic lighting, strong but natural shadows,
subtle atmospheric depth, serious historical-documentary mood.
Premium illustrated documentary frame, not a children's cartoon, anime,
3D render, photograph, or glossy game concept art.

SCENE:
An old Indian railway station at night in the 1940s, a steam locomotive,
a few distant human silhouettes, warm lantern light and light mist.
Historically plausible Indian architecture, railway details, and clothing.
Wide cinematic landscape framing, 16:9 composition.
No text, subtitles, letters, logos, or watermark.
""".strip()

    # Pollinations uses a URL reference image, rather than Cloudflare's
    # image_b64 JSON field. URL-encode the prompt safely.
    encoded_prompt = quote(prompt, safe="")
    url = f"{POLLINATIONS_IMAGE_URL}{encoded_prompt}"

    params = {
        "model": POLLINATIONS_MODEL,
        "image": reference_url,
        "width": 1024,
        "height": 576,
        "seed": -1,
        "safe": "true",
    }
    headers = {"Authorization": f"Bearer {api_key}"}

    print("Image provider: Pollinations")
    print("Image model:", POLLINATIONS_MODEL)
    print("Using PDF visual reference: yes (temporary Drive URL)")
    print("Sending one image-generation request...")

    try:
        response = requests.get(
            url,
            headers=headers,
            params=params,
            timeout=TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        raise RuntimeError(
            f"Could not connect to Pollinations ({type(exc).__name__})."
        ) from None

    if response.status_code != 200:
        # Avoid printing response bodies that might contain private details.
        if response.status_code in (401, 403):
            raise RuntimeError(
                "Pollinations authentication/permission failed. Check the API key, "
                "Image permission, and available Pollen balance."
            )
        if response.status_code == 402:
            raise RuntimeError(
                "Pollinations reports insufficient Pollen/credits. Image generation "
                "may not be free for this key."
            )
        if response.status_code == 429:
            raise RuntimeError("Pollinations rate limit reached; try again later.")
        raise RuntimeError(
            f"Pollinations image generation failed with HTTP {response.status_code}."
        )

    content_type = response.headers.get("Content-Type", "").lower()
    if not content_type.startswith("image/"):
        raise RuntimeError(
            "Pollinations returned a non-image response. Check the selected model "
            "and the current API documentation."
        )

    image_bytes = response.content
    is_png = image_bytes.startswith(b"\x89PNG\r\n\x1a\n")
    is_jpeg = image_bytes.startswith(b"\xff\xd8\xff")
    if not is_png and not is_jpeg:
        raise RuntimeError("The API response is not a recognized PNG or JPEG image.")

    global IMAGE_PATH
    IMAGE_PATH = OUTPUT_DIR / (
        "image_generation_test.png" if is_png else "image_generation_test.jpg"
    )
    IMAGE_PATH.write_bytes(image_bytes)

    print("IMAGE GENERATION SUCCESSFUL.")
    print("Image file:", IMAGE_PATH.name)
    print("Image size:", IMAGE_PATH.stat().st_size, "bytes")
    return True


# ============================================================
# UPLOAD GENERATED IMAGE TO EXISTING DRIVE OUTPUT FOLDER
# ============================================================

def upload_image_to_drive(drive):
    if not IMAGE_PATH.exists():
        raise RuntimeError("Generated image file does not exist.")

    mime_type = "image/png" if IMAGE_PATH.suffix.lower() == ".png" else "image/jpeg"
    metadata = {
        "name": IMAGE_PATH.name,
        "parents": [DRIVE_FOLDER_ID],
    }
    media = MediaFileUpload(str(IMAGE_PATH), mimetype=mime_type, resumable=True)

    uploaded = drive.files().create(
        body=metadata,
        media_body=media,
        fields="id,name,size,webViewLink",
    ).execute()

    print("IMAGE UPLOAD SUCCESSFUL.")
    print("Uploaded file:", uploaded.get("name"))
    print("Drive file ID:", uploaded.get("id"))
    print("File size:", uploaded.get("size", "Unknown"), "bytes")


# ============================================================
# MAIN
# ============================================================

def main():
    print("=" * 45)
    print("ANIDOC DOCUMENTARY BOT")
    print("POLLINATIONS IMAGE GENERATION TEST")
    print("=" * 45)

    drive = None
    reference_file_id = None
    reference_local_path = None

    try:
        drive = connect_google_drive()
        reference_local_path = render_reference_page()
        reference_file_id, reference_url = upload_temporary_reference(
            drive, reference_local_path
        )

        generate_image(reference_url)
        upload_image_to_drive(drive)

        print("=" * 45)
        print("IMAGE GENERATION AND DRIVE UPLOAD TEST PASSED")
        print("=" * 45)

    except HttpError as exc:
        print("Google Drive HTTP request failed.")
        print("HTTP status:", getattr(exc.resp, "status", "Unknown"))
        print("Check Drive OAuth access, folder ID, and sharing policy.")
        sys.exit(1)
    except Exception as exc:
        print("TEST FAILED.")
        print("Error type:", type(exc).__name__)
        print("Error:", str(exc)[:500])
        sys.exit(1)
    finally:
        if drive is not None and reference_file_id:
            delete_temporary_reference(drive, reference_file_id)
        if reference_local_path and reference_local_path.exists():
            try:
                reference_local_path.unlink()
            except OSError:
                pass


if __name__ == "__main__":
    main()
