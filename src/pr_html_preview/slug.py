import hashlib
import re

MAX_SLUG_LENGTH = 100
DIGEST_LENGTH = 16


def _safe_slug_segment(raw: str) -> str:
    cleaned = re.sub(r"[^0-9A-Za-z._-]+", "-", raw)
    return re.sub(r"-+", "-", cleaned).strip("-._")


def slugify_ref(ref: str) -> str:
    if not ref:
        raise ValueError("Branch ref cannot be empty")
    normalized = ref.split("/", 2)[-1] if ref.startswith("refs/") and "/" in ref else ref
    safe = _safe_slug_segment(normalized.replace("/", "-"))
    if not safe:
        raise ValueError("Invalid branch ref")
    digest = hashlib.sha256(normalized.encode()).hexdigest()[:DIGEST_LENGTH]
    readable_length = MAX_SLUG_LENGTH - DIGEST_LENGTH - 1
    return f"{safe[:readable_length].rstrip('-._')}-{digest}"
