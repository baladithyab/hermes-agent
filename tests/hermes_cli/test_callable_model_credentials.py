"""Command-backed catalog credentials never become anonymous requests."""

import pytest
from unittest.mock import MagicMock, patch

from hermes_cli.models import fetch_api_models, probe_api_models
from hermes_cli.models_validate import validate_requested_model


class TestCallableModelCredentials:
    @staticmethod
    def _request(entrypoint, credential, base_url, *, api_mode=None):
        from hermes_cli.models import cached_fetch_api_models

        if entrypoint == "probe":
            return probe_api_models(credential, base_url, api_mode=api_mode)["models"]
        if entrypoint == "fetch":
            return fetch_api_models(credential, base_url, api_mode=api_mode)
        if entrypoint == "cached":
            return cached_fetch_api_models(credential, base_url, api_mode=api_mode, force_refresh=True)
        if entrypoint == "picker":
            from hermes_cli.model_switch_providers import _fetch_picker_live_models
            return _fetch_picker_live_models(credential, base_url, "custom", False, api_mode=api_mode)
        return validate_requested_model(
            "generated-model", "custom:cpa", api_key=credential, base_url=base_url, api_mode=api_mode,
        )

    @pytest.mark.parametrize("entrypoint", ["probe", "fetch", "cached", "validate"])
    @pytest.mark.parametrize("api_mode", ["chat_completions", "anthropic_messages"])
    def test_command_credential_reaches_http_as_token(self, entrypoint, api_mode):
        from agent.command_token_source import CommandTokenSource

        credential = CommandTokenSource("generated-test-command", "test-provider")
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = b'{"data":[{"id":"generated-model"}]}'
        with patch("agent.command_token_source._mint", return_value=("generated-grant", 0)) as mint, \
             patch("hermes_cli.models._urlopen_model_catalog_request", return_value=response) as request:
            result = self._request(entrypoint, credential, "http://127.0.0.1:19181/v1", api_mode=api_mode)

        mint.assert_called_once()
        request.assert_called_once()
        headers = {key.lower(): value for key, value in request.call_args.args[0].header_items()}
        if api_mode == "anthropic_messages":
            assert headers["x-api-key"] == "generated-grant"
            assert headers["anthropic-version"] == "2023-06-01"
            assert "authorization" not in headers
        else:
            assert headers["authorization"] == "Bearer generated-grant"
        if entrypoint == "validate":
            assert result["accepted"] and result["recognized"]
        else:
            assert result == ["generated-model"]

    @pytest.mark.parametrize("entrypoint", ["probe", "fetch", "cached", "picker", "validate"])
    @pytest.mark.parametrize("failure", ["raises", "empty", "non-string"])
    def test_failed_command_credential_never_probes_anonymously(self, entrypoint, failure):
        from agent.command_token_source import CommandTokenError, CommandTokenSource

        credential = CommandTokenSource("generated-test-command", "test-provider")
        mint_options = (
            {"side_effect": CommandTokenError("generated credential failure")}
            if failure == "raises" else {"return_value": (" " if failure == "empty" else None, 0)}
        )
        with patch("agent.command_token_source._mint", **mint_options) as mint, \
             patch("hermes_cli.models._urlopen_model_catalog_request") as request, \
             patch("hermes_cli.models_local.probe_ollama_local_models") as native_request:
            result = self._request(entrypoint, credential, "http://127.0.0.1:11434/v1")

        mint.assert_called_once()
        request.assert_not_called()
        native_request.assert_not_called()
        if entrypoint == "validate":
            assert (result["accepted"], result["persist"], result["recognized"]) == (False, True, False)
        else:
            assert result is None

    @pytest.mark.parametrize("api_mode,accepted", [
        ("chat_completions", True),
        ("anthropic_messages", True),
        ("codex_responses", False),
        (None, False),
    ])
    @pytest.mark.parametrize("failure", ["raises", "empty", "none", "non-string"])
    def test_failed_callable_validation_preserves_mode_policy(self, api_mode, accepted, failure):
        credential = MagicMock(
            side_effect=RuntimeError("generated credential failure"),
        ) if failure == "raises" else MagicMock(
            return_value={"empty": " ", "none": None, "non-string": 42}[failure],
        )
        with patch("hermes_cli.models._urlopen_model_catalog_request") as request, \
             patch("hermes_cli.models_local.probe_ollama_local_models") as native_request:
            result = self._request(
                "validate", credential, "http://127.0.0.1:11434/v1", api_mode=api_mode,
            )

        assert (result["accepted"], result["persist"], result["recognized"]) == (accepted, True, False)
        credential.assert_called_once_with()
        request.assert_not_called()
        native_request.assert_not_called()

    @pytest.mark.parametrize("credential", [None, ""])
    def test_anonymous_local_catalog_remains_available(self, credential):
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = b'{"data":[]}'
        with patch("hermes_cli.models._urlopen_model_catalog_request", return_value=response) as request:
            result = probe_api_models(credential, "http://127.0.0.1:19181/v1")

        assert result["models"] == []
        request.assert_called_once()
        assert request.call_args.args[0].get_header("Authorization") is None

    def test_plain_callable_can_return_an_empty_authenticated_catalog(self):
        credential = MagicMock(return_value=" generated-grant ")
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = b'{"data":[]}'
        with patch("hermes_cli.models._urlopen_model_catalog_request", return_value=response) as request:
            result = probe_api_models(credential, "http://127.0.0.1:19181/v1")

        assert result["models"] == []
        credential.assert_called_once_with()
        assert request.call_args.args[0].get_header("Authorization") == "Bearer generated-grant"
