import hashlib
import hmac


def validate_github_signature(secret: str, body: bytes, signature_header: str | None) -> bool:
    if not signature_header:
        return False

    if not signature_header.startswith("sha256="):
        return False

    signature = signature_header[len("sha256=") :]
    if not signature:
        return False

    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature, expected)
