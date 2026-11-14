"""Redact temporary invitation/reset URLs and lab keys from local evidence."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2] / "EGGSor_Test_Evidence"
TEXT_SUFFIXES = {".json", ".log", ".csv", ".html", ".xml", ".md", ".txt"}

for path in ROOT.rglob("*"):
    if not path.is_file() or path.suffix.lower() not in TEXT_SUFFIXES:
        continue
    content = path.read_text(encoding="utf-8", errors="replace")
    content = re.sub(
        r"(?i)(/(?:accept-invite|reset-password)/)[A-Za-z0-9_-]{16,}",
        r"\1[REDACTED]", content,
    )
    content = re.sub(r"(?i)([?&]key=)[^&\s\"<>]+", r"\1[REDACTED]", content)
    path.write_text(content, encoding="utf-8")
