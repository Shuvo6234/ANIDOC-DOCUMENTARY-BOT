import os
import sys
import json
import base64
from pathlib import Path

import requests

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from googleapiclient.errors import HttpError


# ==========================================
# CONFIGURATION
# ==========================================

PROJECT_DIR = Path(__file__).parent
OUTPUT_DIR = PROJECT_DIR / "output"

# Existing Google Drive Output folder ID
DRIVE_FOLDER_ID = "1tOKfM5wPPIA42gTUzVQUsgm295mzPqeJ"

IMAGE_MODEL = "@cf/bytedance/stable-diffusion-xl-lightning"
IMAGE_PATH = OUTPUT_DIR / "image_generation_test.png"

CLOUDFLARE_API_URL = (
    "https://api.cloudflare.com/client/v4/accounts/"
    "{account_id}/ai/run/{model}"
)


# ==========================================
# CLOUDFLARE CONFIGURATION
# ==========================================

def get_cloudflare_config():
    token = os.getenv("CLOUDFLARE_API_TOKEN", "").strip()
    account_id = os.getenv("CLOUDFLARE_ACCOUNT_ID", "").strip()

    if not token:
        raise RuntimeError("CLOUDFLARE_API_TOKEN GitHub Secret is missing.")
    if not account_id:
        raise RuntimeError("CLOUDFLARE_ACCOUNT_ID GitHub Secret is missing.")

    return token, account_id


# ==========================================
# GOOGLE DRIVE CONNECTION (OAUTH)
# ==========================================

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

    drive_service = build(
        "drive",
        "v3",
        credentials=credentials,
        cache_discovery=False,
    )

    folder = drive_service.files().get(
        fileId=DRIVE_FOLDER_ID,
        fields="id,name,mimeType",
    ).execute()

    if folder.get("mimeType") != "application/vnd.google-apps.folder":
        raise RuntimeError("Configured Google Drive ID is not a folder.")

    print("Google Drive connected successfully.")
    print("Output folder:", folder.get("name", "Unknown"))
    return drive_service


# ==========================================
# IMAGE GENERATION
# ==========================================

def generate_test_image(token, account_id):
    prompt = """
Cinematic documentary illustration, semi-realistic 2D
animation style, historical India in the 1940s.

An old Indian railway station at night, a steam locomotive,
distant human silhouettes, warm lantern light and light mist.

Historically appropriate Indian architecture and clothing.
Detailed South Asian environment, clean outlines, muted
warm colors, dramatic cinematic lighting, serious
historical documentary atmosphere.

Wide landscape composition, 16:9 aspect ratio.
No text, no subtitles, no logos, no watermark.
"""

    url = CLOUDFLARE_API_URL.format(
        account_id=account_id,
        model=IMAGE_MODEL,
    )
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    payload = {
        "prompt": prompt,
        # Requested near-2K landscape output (16:9).
        # If this model/account rejects these dimensions, check the API error:
        # some model configurations may enforce a lower maximum resolution.
        "width": 2048,
        "height": 1152,
        "num_steps": 4,
    }

    print("Image model:", IMAGE_MODEL)
    print("Sending one image-generation request...")

    try:
        response = requests.post(
            url,
            headers=headers,
            json=payload,
            timeout=300,
        )
    except requests.RequestException as error:
        raise RuntimeError(
            "Could not connect to Cloudflare image API. "
            f"Error type: {type(error).__name__}"
        ) from None

    if response.status_code != 200:
        print("Cloudflare API returned HTTP status:", response.status_code)
        try:
            error_data = response.json()
            for item in error_data.get("errors", [])[:3]:
                print("API error:", str(item.get("message", "Unknown error"))[:300])
        except (ValueError, AttributeError):
            print("Cloudflare returned an unexpected error response.")

        if response.status_code == 401:
            raise RuntimeError("Authentication failed. Check the Cloudflare API token.")
        if response.status_code == 403:
            raise RuntimeError(
                "Access denied. Check Workers AI permissions and account access."
            )
        if response.status_code == 429:
            raise RuntimeError(
                "Rate limit or usage quota reached. Check Cloudflare Workers AI usage."
            )
        raise RuntimeError(
            f"Image generation failed with HTTP {response.status_code}."
        )

    content_type = response.headers.get("Content-Type", "").lower()
    image_bytes = None

    if content_type.startswith("image/"):
        image_bytes = response.content
    else:
        try:
            data = response.json()
        except ValueError:
            raise RuntimeError(
                "Cloudflare returned an unknown response format."
            ) from None

        result = data.get("result")
        if isinstance(result, str):
            try:
                image_bytes = base64.b64decode(result, validate=True)
            except (ValueError, base64.binascii.Error):
                raise RuntimeError("Could not decode the returned image.") from None
        elif isinstance(result, dict):
            image_value = result.get("image")
            if isinstance(image_value, str):
                try:
                    image_bytes = base64.b64decode(image_value, validate=True)
                except (ValueError, base64.binascii.Error):
                    raise RuntimeError("Could not decode the returned image.") from None

        if not image_bytes:
            raise RuntimeError("The API response did not contain a usable image.")

    if not image_bytes:
        raise RuntimeError("The generated image is empty.")

    is_png = image_bytes.startswith(b"\x89PNG\r\n\x1a\n")
    is_jpeg = image_bytes.startswith(b"\xff\xd8\xff")
    if not is_png and not is_jpeg:
        raise RuntimeError("The API response is not a recognized PNG or JPEG image.")

    global IMAGE_PATH
    IMAGE_PATH = (
        OUTPUT_DIR / "image_generation_test.jpg"
        if is_jpeg
        else OUTPUT_DIR / "image_generation_test.png"
    )
    IMAGE_PATH.write_bytes(image_bytes)

    print("IMAGE GENERATION SUCCESSFUL.")
    print("Image file:", IMAGE_PATH.name)
    print("Image size:", IMAGE_PATH.stat().st_size, "bytes")
    return True


# ==========================================
# UPLOAD IMAGE TO GOOGLE DRIVE
# ==========================================

def upload_image_to_drive(drive_service):
    if not IMAGE_PATH.exists():
        raise RuntimeError("Generated image file does not exist.")

    mime_type = (
        "image/jpeg"
        if IMAGE_PATH.suffix.lower() == ".jpg"
        else "image/png"
    )
    file_metadata = {
        "name": IMAGE_PATH.name,
        "parents": [DRIVE_FOLDER_ID],
    }
    media = MediaFileUpload(
        str(IMAGE_PATH),
        mimetype=mime_type,
        resumable=True,
    )

    uploaded_file = drive_service.files().create(
        body=file_metadata,
        media_body=media,
        fields="id,name,size,webViewLink",
    ).execute()

    print("IMAGE UPLOAD SUCCESSFUL.")
    print("Uploaded file:", uploaded_file.get("name"))
    print("Drive file ID:", uploaded_file.get("id"))
    print("File size:", uploaded_file.get("size", "Unknown"), "bytes")


# ==========================================
# MAIN
# ==========================================

def main():
    print("=" * 45)
    print("ANIDOC DOCUMENTARY BOT")
    print("CLOUDFLARE IMAGE GENERATION TEST")
    print("=" * 45)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    try:
        token, account_id = get_cloudflare_config()
        print("Cloudflare configuration detected.")

        drive_service = connect_google_drive()
        success = generate_test_image(token, account_id)
        if not success:
            raise RuntimeError("Image generation failed.")

        upload_image_to_drive(drive_service)

        print("=" * 45)
        print("IMAGE GENERATION AND DRIVE UPLOAD TEST PASSED")
        print("=" * 45)

    except HttpError as error:
        print("Google Drive HTTP request failed.")
        print("HTTP status:", getattr(error.resp, "status", "Unknown"))
        try:
            raw_content = getattr(error, "content", b"")
            if isinstance(raw_content, bytes):
                raw_content = raw_content.decode("utf-8", errors="replace")
            details = json.loads(raw_content)
            print(
                "Google Drive error details:",
                json.dumps(details, ensure_ascii=False)[:2000],
            )
        except Exception:
            print("Could not read detailed Google Drive error.")
        sys.exit(1)

    except json.JSONDecodeError:
        print("Google OAuth configuration returned invalid JSON.")
        sys.exit(1)

    except Exception as error:
        print("TEST FAILED.")
        print("Error type:", type(error).__name__)
        print("Error:", str(error)[:500])
        sys.exit(1)


if __name__ == "__main__":
    main()
