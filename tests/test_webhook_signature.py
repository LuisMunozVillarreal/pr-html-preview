import hashlib
import hmac

from pr_html_preview.webhook import validate_github_signature


def test_validate_signature_with_valid_header():
    body = b"payload-body"
    secret = "whsec"
    sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    header = f"sha256={sig}"

    assert validate_github_signature(secret=secret, body=body, signature_header=header)


def test_validate_signature_rejects_invalid_signature():
    body = b"payload-body"
    assert not validate_github_signature(
        secret="whsec",
        body=body,
        signature_header="sha256=deadbeef",
    )


def test_validate_signature_rejects_bad_prefix():
    body = b"payload-body"
    sig = hmac.new(b"whsec", body, hashlib.sha256).hexdigest()
    assert not validate_github_signature(
        secret="whsec",
        body=body,
        signature_header=f"md5={sig}",
    )


def test_validate_signature_rejects_empty_digest():
    assert not validate_github_signature("whsec", b"payload", "sha256=")
