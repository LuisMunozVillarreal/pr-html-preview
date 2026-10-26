import hashlib

import pytest

from pr_html_preview.slug import slugify_ref


def test_slugify_ref_always_adds_digest() -> None:
    assert slugify_ref("main") == f"main-{hashlib.sha256(b'main').hexdigest()[:16]}"
    assert slugify_ref("release-2026.08").startswith("release-2026.08-")


def test_slugify_ref_prevents_normalized_literal_collision() -> None:
    transformed = slugify_ref("feature/x")
    literal = slugify_ref(transformed)
    assert transformed.startswith("feature-x-")
    assert transformed != literal


def test_slugify_ref_is_deterministic() -> None:
    assert slugify_ref("release/feat my branch") == slugify_ref("release/feat my branch")


def test_slugify_ref_rejects_empty_ref() -> None:
    with pytest.raises(ValueError):
        slugify_ref("")


def test_slugify_ref_bounds_long_branch_names() -> None:
    slug = slugify_ref("a" * 200)
    assert len(slug) <= 100
    assert slug.endswith(hashlib.sha256(("a" * 200).encode()).hexdigest()[:16])
