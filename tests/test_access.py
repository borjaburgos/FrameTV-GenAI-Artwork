"""Tests for persistent browser-device access."""

from __future__ import annotations

import pytest

from frameart.access import AccessStore, InvalidPairingCodeError


def test_pairing_is_single_use_and_creates_revocable_device(tmp_path):
    store = AccessStore(tmp_path)
    pairing = store.create_pairing(created_by="admin", lifetime_seconds=600)

    token, device = store.consume_pairing(
        str(pairing["code"]),
        device_name="Kitchen iPad",
        lifetime_seconds=3600,
    )

    authenticated = store.authenticate_device(token)
    assert authenticated is not None
    assert authenticated["id"] == device["id"]
    assert authenticated["name"] == "Kitchen iPad"
    assert authenticated["scopes"] == ["admin", "control", "read"]
    assert store.list_devices()[0]["id"] == device["id"]

    with pytest.raises(InvalidPairingCodeError, match="invalid or expired"):
        store.consume_pairing(
            str(pairing["code"]),
            device_name="Replay",
            lifetime_seconds=3600,
        )

    assert store.revoke_device(str(device["id"])) is True
    assert store.authenticate_device(token) is None
    assert store.revoke_device(str(device["id"])) is False


def test_token_login_device_preserves_limited_scopes(tmp_path):
    store = AccessStore(tmp_path)
    token, device = store.create_device(
        device_name="Home Assistant tablet",
        scopes={"read", "control"},
        lifetime_seconds=3600,
    )

    assert device["scopes"] == ["control", "read"]
    assert store.authenticate_device(token)["scopes"] == ["control", "read"]
