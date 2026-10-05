"""Fetch YOLO weights during Render's build so they exist in the deploy image."""

from __future__ import annotations

import hashlib
import os
import re
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parent
raw_path = os.environ.get("YOLO_MODEL_PATH", "best.pt").strip() or "best.pt"
MODEL_PATH = Path(raw_path).expanduser()
if not MODEL_PATH.is_absolute():
    MODEL_PATH = ROOT / MODEL_PATH
MODEL_URL = os.environ.get("YOLO_MODEL_URL", "").strip()
EXPECTED_SHA256 = os.environ.get("YOLO_MODEL_SHA256", "").strip().lower()
MAX_MODEL_BYTES = 2 * 1024 * 1024 * 1024


def main() -> int:
    if not MODEL_URL:
        if MODEL_PATH.is_file():
            print(f"Using YOLO model at {MODEL_PATH}.")
            return 0
        print(
            "YOLO model missing. Set YOLO_MODEL_URL to a stable HTTPS download "
            "URL for best.pt, or include best.pt in the build context.",
            file=sys.stderr,
        )
        return 1

    if urlsplit(MODEL_URL).scheme != "https":
        print("YOLO_MODEL_URL must use HTTPS.", file=sys.stderr)
        return 1
    if EXPECTED_SHA256 and not re.fullmatch(r"[0-9a-f]{64}", EXPECTED_SHA256):
        print("YOLO_MODEL_SHA256 must be a 64-character SHA-256 hex digest.", file=sys.stderr)
        return 1

    MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    total = 0
    temp_path: Path | None = None
    try:
        request = Request(MODEL_URL, headers={"User-Agent": "EggSort-Render-build/1.0"})
        with urlopen(request, timeout=60) as response:
            with tempfile.NamedTemporaryFile(
                mode="wb", dir=MODEL_PATH.parent, prefix=f"{MODEL_PATH.name}.",
                suffix=".download", delete=False,
            ) as output:
                temp_path = Path(output.name)
                while chunk := response.read(1024 * 1024):
                    total += len(chunk)
                    if total > MAX_MODEL_BYTES:
                        raise ValueError("Model download exceeds the 2 GiB limit.")
                    digest.update(chunk)
                    output.write(chunk)

        if total == 0:
            raise ValueError("Model download was empty.")
        if EXPECTED_SHA256 and digest.hexdigest() != EXPECTED_SHA256:
            raise ValueError("Downloaded model SHA-256 does not match YOLO_MODEL_SHA256.")
        temp_path.replace(MODEL_PATH)
        print(f"Downloaded YOLO model ({total:,} bytes) to {MODEL_PATH}.")
        return 0
    except Exception as exc:
        print(f"Unable to prepare YOLO model: {exc}", file=sys.stderr)
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
