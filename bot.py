import os
import sys
import json
import time
from pathlib import Path

from google import genai
from google.genai import types

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from googleapiclient.errors import HttpError


PROJECT_DIR = Path(__file__).parent
OUTPUT_DIR = PROJECT_DIR / "output"

DRIVE_FOLDER_ID = "1tOKfM5wPPIA42gTUzVQUsgm295mzPqeJ"

IMAGE_MODEL = "gemini-3.1-flash-image"

IMAGE_PATH = OUTPUT_DIR / "image_generation_test.png"


def get_api_keys():
    raw_keys = os.getenv("GEMINI_API_KEYS", "").strip()

    if not raw_keys:
        raise RuntimeError(
            "GEMINI_API_KEYS secret is missing."
        )

    # Supports one key per line or comma-separated keys.
    keys = []

    for line in raw_keys.splitlines():
        for item in line.split(","):
            key = item.strip()

            if key and key not in keys:
                keys.append(key)

    if not keys:
        raise RuntimeError("No API keys were found.")

    return keys


def connect_google_drive():
    raw_credentials = os.getenv(
        "GOOGLE_SERVICE_ACCOUNT_JSON", ""
    ).strip()

    if not raw_credentials:
        raise RuntimeError(
            "GOOGLE_SERVICE_ACCOUNT_JSON secret is missing."
        )

    credentials_info = json.loads(raw_credentials)

    credentials = (
        service_account.Credentials.from_service_account_info(
            credentials_info,
            scopes=[
                "https://www.googleapis.com/auth/drive"
            ],
        )
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
        raise RuntimeError(
            "Configured Google Drive ID is not a folder."
        )

    print("Google Drive connected successfully.")
    print("Output folder:", folder.get("name", "Unknown"))

    return drive_service


def is_quota_or_temporary_error(error):
    """
    Rotate to the next key for quota/rate-limit and
    temporary server errors. Do not retry every kind
    of error because some errors will affect all keys.
    """

    message = str(error).lower()

    retry_markers = [
        "429",
        "resource_exhausted",
        "resource exhausted",
        "quota exceeded",
        "rate limit",
        "too many requests",
        "503",
        "service unavailable",
        "500",
        "internal server error",
    ]

    return any(marker in message for marker in retry_markers)


def generate_test_image(api_keys):
    prompt = """
Create one cinematic 16:9 documentary illustration
in a semi-realistic 2D animation style.

Scene: An old Indian railway station at night during
the 1940s, with a steam locomotive, a few distant
silhouettes, warm lantern light, light mist, historically
appropriate architecture and clothing.

Use clean outlines, realistic South Asian environments,
muted warm colors, dramatic cinematic lighting, detailed
backgrounds and a serious documentary atmosphere.

Wide landscape composition, 16:9 aspect ratio.
No text, no subtitles, no logos, no watermark text.
"""

    total_keys = len(api_keys)

    for index, api_key in enumerate(api_keys, start=1):
        print(f"Testing image generation with API key {index}/{total_keys}...")

        try:
            client = genai.Client(api_key=api_key)

            response = client.models.generate_content(
                model=IMAGE_MODEL,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_modalities=["IMAGE"],
                    image_config=types.ImageConfig(
                        aspect_ratio="16:9"
                    ),
                ),
            )

            for part in response.parts:
                if part.inline_data is not None:
                    image = part.as_image()
                    image.save(str(IMAGE_PATH))

                    if not IMAGE_PATH.exists() or IMAGE_PATH.stat().st_size == 0:
                        raise RuntimeError(
                            "Image response received, but image file is empty."
                        )

                    print("IMAGE GENERATION SUCCESSFUL.")
                    print("Model:", IMAGE_MODEL)
                    print("Image saved:", IMAGE_PATH.name)
                    print("Image size:", IMAGE_PATH.stat().st_size, "bytes")

                    return True

            print("This key returned no image.")

            # No image returned: stop rather than sending
            # repeated potentially chargeable requests.
            return False

        except Exception as error:
            print(
                f"Key {index} failed with error type: "
                f"{type(error).__name__}"
            )

            # Do not print the full exception because
            # error details may contain sensitive request data.
            message = str(error)
            print("Error:", message[:500])

            if is_quota_or_temporary_error(error):
                if index < total_keys:
                    print("Trying the next API key...")
                    time.sleep(2)
                    continue

            else:
                print(
                    "Error is not clearly a quota/rate-limit "
                    "error. Stopping to avoid unnecessary requests."
                )
                return False

    print("All API keys were tried, but image generation failed.")
    return False


def upload_image_to_drive(drive_service):
    if not IMAGE_PATH.exists():
        raise RuntimeError("Test image file does not exist.")

    file_metadata = {
        "name": IMAGE_PATH.name,
        "parents": [DRIVE_FOLDER_ID],
    }

    media = MediaFileUpload(
        str(IMAGE_PATH),
        mimetype="image/png",
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


def main():
    print("=" * 45)
    print("ANIDOC DOCUMENTARY BOT - IMAGE TEST")
    print("=" * 45)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    try:
        api_keys = get_api_keys()
        print("Gemini API keys detected:", len(api_keys))

        drive_service = connect_google_drive()

        print("Image model:", IMAGE_MODEL)
        print("Starting one-image test...")

        success = generate_test_image(api_keys)

        if not success:
            print("TEST FAILED: No image was generated.")
            sys.exit(1)

        upload_image_to_drive(drive_service)

        print("=" * 45)
        print("IMAGE GENERATION AND DRIVE UPLOAD TEST PASSED")
        print("=" * 45)

    except HttpError as error:
        print("Google Drive HTTP request failed.")
        print("HTTP status:", getattr(error.resp, "status", "Unknown"))
        sys.exit(1)

    except json.JSONDecodeError:
        print(
            "ERROR: GOOGLE_SERVICE_ACCOUNT_JSON "
            "is not valid JSON."
        )
        sys.exit(1)

    except Exception as error:
        print("TEST FAILED.")
        print("Error type:", type(error).__name__)
        print("Error:", str(error)[:500])
        sys.exit(1)


if __name__ == "__main__":
    main()
