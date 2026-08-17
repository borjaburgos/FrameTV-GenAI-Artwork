"""Tests for public-domain rights enforcement."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from frameart import public_domain


def _europeana_record(rights: str | None) -> dict:
    record = {
        "id": "/record/1",
        "title": ["Landscape"],
        "edmIsShownBy": ["https://example.test/image.jpg"],
    }
    if rights is not None:
        record["rights"] = [rights]
    return record


def test_europeana_requires_explicit_public_domain_rights():
    item = public_domain._europeana_object_to_item(_europeana_record(None))
    assert item is not None
    assert item["is_public_domain"] is False


def test_europeana_accepts_public_domain_mark():
    item = public_domain._europeana_object_to_item(
        _europeana_record("http://creativecommons.org/publicdomain/mark/1.0/")
    )
    assert item is not None
    assert item["is_public_domain"] is True


def test_cma_open_access_alone_is_not_treated_as_public_domain():
    assert public_domain._cma_is_public_domain({"open_access": True}) is False


@patch("frameart.public_domain._met_fetch_object")
@patch("frameart.public_domain._http_client")
def test_get_artwork_fails_closed_for_non_public_item(mock_client, mock_fetch):
    mock_client.return_value.__enter__.return_value = MagicMock()
    mock_fetch.return_value = {
        "objectID": 123,
        "title": "Copyrighted work",
        "primaryImage": "https://example.test/image.jpg",
        "isPublicDomain": False,
    }

    with pytest.raises(ValueError, match="not verified as public domain"):
        public_domain.get_artwork("met", "123")

