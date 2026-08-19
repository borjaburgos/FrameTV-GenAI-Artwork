"""Regression tests for credential-safe runtime logging."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx2 as httpx

from frameart.api import _fetch_google_image_models
from frameart.config import ProviderConfig, Settings
from frameart.logging_utils import safe_exception_message, sanitize_log_value
from frameart.pipeline import run_generate


def test_sanitizer_removes_queries_authorization_and_explicit_secrets():
    marker = "marker-secret-provider-key"
    value = (
        f"Authorization: Bearer {marker} "
        f"GET https://images.example.test/file.png?X-Amz-Signature={marker}&token=abc "
        f"api_key={marker}"
    )

    sanitized = sanitize_log_value(value, secrets=[marker])

    assert marker not in sanitized
    assert "X-Amz-Signature" not in sanitized
    assert "?" not in sanitized
    assert "Authorization: [redacted]" in sanitized
    assert "https://images.example.test/file.png" in sanitized


def test_http_exception_message_keeps_status_host_and_path_only():
    marker = "marker-secret-provider-key"
    request = SimpleNamespace(
        method="GET",
        url=f"https://api.example.test/v1/models?key={marker}&alt=json",
    )
    exc = RuntimeError("upstream failed")
    exc.response = SimpleNamespace(status_code=503, request=request)

    sanitized = safe_exception_message(exc, secrets=[marker])

    assert sanitized == "RuntimeError status=503 GET https://api.example.test/v1/models"


def test_google_model_discovery_503_never_logs_query_key(caplog):
    marker = "marker-google-api-key"
    request = httpx.Request(
        "GET",
        f"https://generativelanguage.googleapis.com/v1beta/models?key={marker}&alt=json",
    )
    response = httpx.Response(503, request=request)
    client = MagicMock()
    client.get.return_value = response

    caplog.set_level(logging.WARNING)
    with patch("frameart.api.httpx.Client") as client_factory:
        client_factory.return_value.__enter__.return_value = client
        assert _fetch_google_image_models(ProviderConfig(api_key=marker)) == []

    log_text = caplog.text
    assert marker not in log_text
    assert "?key=" not in log_text
    assert "status=503" in log_text
    assert "generativelanguage.googleapis.com/v1beta/models" in log_text


def test_google_model_discovery_timeout_logs_safe_endpoint(caplog):
    marker = "marker-google-api-key"
    request = httpx.Request(
        "GET",
        f"https://generativelanguage.googleapis.com/v1beta/models?key={marker}",
    )
    client = MagicMock()
    client.get.side_effect = httpx.ConnectTimeout("timed out", request=request)

    caplog.set_level(logging.WARNING)
    with patch("frameart.api.httpx.Client") as client_factory:
        client_factory.return_value.__enter__.return_value = client
        assert _fetch_google_image_models(ProviderConfig(api_key=marker)) == []

    assert marker not in caplog.text
    assert "ConnectTimeout GET" in caplog.text
    assert "generativelanguage.googleapis.com/v1beta/models" in caplog.text


def test_provider_pipeline_error_redacts_configured_key_from_result_and_log(
    caplog,
    tmp_path,
):
    marker = "marker-openai-api-key"
    provider = MagicMock(name="provider")
    provider.name = "openai"
    provider.generate.side_effect = RuntimeError(
        f"Authorization: Bearer {marker} at "
        f"https://api.openai.com/v1/images?sig={marker}"
    )
    settings = Settings(
        data_dir=tmp_path,
        providers={"openai": ProviderConfig(api_key=marker)},
    )

    caplog.set_level(logging.ERROR)
    with patch("frameart.pipeline._get_provider_instance", return_value=provider):
        result = run_generate(settings, "safe prompt")

    assert result.error_code == "generation_failed"
    assert marker not in (result.error or "")
    assert marker not in caplog.text
    assert "?sig=" not in caplog.text
