#!/usr/bin/env python3
"""Test TraxDinosaur Image API and write a Markdown summary.

The documented API expects POST /api/genimage with JSON:
    {"prompt": "your image prompt"}
It returns PNG bytes directly (not JSON).
"""
from __future__ import annotations

import os
import time
import json
import mimetypes
from pathlib import Path
from datetime import datetime, timezone

import requests

BASE_URL = os.getenv("IMAGE_API_URL", "https://apiimagestrax.vercel.app").rstrip("/")
ENDPOINT = os.getenv("IMAGE_API_ENDPOINT", "/api/genimage")
PROMPT_FIELD = os.getenv("PROMPT_FIELD", "prompt")
API_KEY = os.getenv("IMAGE_API_KEY", "").strip()
REQUEST_COUNT = int(os.getenv("REQUEST_COUNT", "20"))
GAP_SECONDS = float(os.getenv("GAP_SECONDS", "1"))
PROMPT = os.getenv(
    "IMAGE_PROMPT",
    "A cinematic fantasy landscape with a small castle on a floating island, "
    "golden sunrise, detailed digital art"
)
OUTPUT_DIR = Path("generated-images")
SUMMARY_PATH = Path(os.getenv("GITHUB_STEP_SUMMARY", "summary.md"))

def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    url = BASE_URL + (ENDPOINT if ENDPOINT.startswith("/") else "/" + ENDPOINT)
    rows = []
    success = 0
    total_start = time.perf_counter()

    session = requests.Session()
    headers = {"Content-Type": "application/json", "Accept": "image/png, image/*, */*"}
    if API_KEY:
        # Generic bearer auth; leave IMAGE_API_KEY empty unless the API requires it.
        headers["Authorization"] = f"Bearer {API_KEY}"

    for index in range(1, REQUEST_COUNT + 1):
        started = time.perf_counter()
        status = "FAIL"
        detail = ""
        filename = ""
        try:
            response = session.post(
                url,
                json={PROMPT_FIELD: PROMPT},
                headers=headers,
                timeout=(20, 180),
            )
            elapsed = time.perf_counter() - started
            content_type = response.headers.get("Content-Type", "").split(";")[0].lower()

            if response.ok and response.content and (
                content_type.startswith("image/") or response.content.startswith(b"\x89PNG\r\n\x1a\n")
                or response.content.startswith(b"\xff\xd8\xff")
            ):
                ext = mimetypes.guess_extension(content_type) or ".png"
                if ext == ".jpe":
                    ext = ".jpg"
                filename = f"image_{index:02d}{ext}"
                (OUTPUT_DIR / filename).write_bytes(response.content)
                status = "SUCCESS"
                detail = f"{content_type or 'image'}; {len(response.content):,} bytes"
                success += 1
            else:
                body = response.text[:500].replace("\n", " ")
                detail = f"HTTP {response.status_code}; {content_type or 'unknown content type'}; {body}"
        except requests.RequestException as exc:
            elapsed = time.perf_counter() - started
            detail = f"{type(exc).__name__}: {str(exc)[:350]}"

        rows.append((index, status, f"{elapsed:.2f}", filename, detail))
        print(f"[{index}/{REQUEST_COUNT}] {status} | {elapsed:.2f}s | {detail}", flush=True)

        if index < REQUEST_COUNT and GAP_SECONDS > 0:
            time.sleep(GAP_SECONDS)

    total_elapsed = time.perf_counter() - total_start
    failed = REQUEST_COUNT - success
    avg = sum(float(row[2]) for row in rows) / len(rows) if rows else 0

    lines = [
        "# Trax Image API Test",
        "",
        f"- **Endpoint:** `{url}`",
        f"- **Method:** `POST`",
        f"- **Prompt JSON field:** `{PROMPT_FIELD}`",
        f"- **Requests:** {REQUEST_COUNT}",
        f"- **Success:** {success}",
        f"- **Failed:** {failed}",
        f"- **Success rate:** {success / REQUEST_COUNT * 100:.1f}%" if REQUEST_COUNT else "- **Success rate:** N/A",
        f"- **Average request time:** {avg:.2f}s",
        f"- **Total elapsed time:** {total_elapsed:.2f}s",
        f"- **Finished (UTC):** {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        "",
        "| # | Result | Seconds | Saved image | Details |",
        "|---:|---|---:|---|---|",
    ]
    for number, result, seconds, image, detail in rows:
        safe_detail = detail.replace("|", r"\|")
        lines.append(f"| {number} | {result} | {seconds} | {image or '—'} | {safe_detail} |")
    lines += ["", "Images, when generated, are available in the `generated-images` workflow artifact.", ""]

    summary = "\n".join(lines)
    SUMMARY_PATH.parent.mkdir(parents=True, exist_ok=True)
    with SUMMARY_PATH.open("a", encoding="utf-8") as f:
        f.write(summary + "\n")
    Path("test-results.json").write_text(
        json.dumps({
            "endpoint": url, "prompt_field": PROMPT_FIELD,
            "requests": REQUEST_COUNT, "success": success, "failed": failed,
            "average_seconds": round(avg, 3), "total_seconds": round(total_elapsed, 3),
            "results": [
                {"number": n, "status": s, "seconds": float(t), "image": img, "detail": d}
                for n, s, t, img, d in rows
            ],
        }, indent=2),
        encoding="utf-8",
    )
    print(f"\nFinished: {success}/{REQUEST_COUNT} succeeded; {failed} failed.")
    # Do not fail the workflow just because some API requests failed;
    # the summary and artifacts should still be available for diagnosis.

if __name__ == "__main__":
    main()
