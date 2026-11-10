"""Canonical model selection for backend routes, inside the credential-owning core."""

from typing import Any, Mapping

from agent.delegation_provider import DelegationRouteError


def _allowed_selections(selection: Any) -> set[tuple[str, str]] | None:
    if selection is True:
        return None
    if not isinstance(selection, Mapping) or set(selection) != {'allowed'} or not isinstance(selection['allowed'], list):
        raise DelegationRouteError('model_selection policy requires only an allowed list of exact provider/model pairs.')
    allowed = set()
    for row in selection['allowed']:
        if (not isinstance(row, Mapping) or set(row) != {'provider', 'model'}
                or any(not isinstance(row[key], str) or not row[key].strip() for key in ('provider', 'model'))):
            raise DelegationRouteError('Each model_selection.allowed entry requires exact provider and model strings.')
        allowed.add((row['provider'], row['model']))
    return allowed


def resolve_model_selection(route: Mapping[str, Any], parent_agent: Any) -> dict[str, Any]:
    """Retain the selector's whole route, including callable keys and alias endpoints.

    A plugin requests selection with IDs only. Running the selector here keeps
    secret-bearing Python objects out of the plugin RPC and shares CLI validation.
    This runs at dispatch, never while constructing a session's tool schema.
    """
    from hermes_cli.config import get_compatible_custom_providers, load_config_readonly
    from hermes_cli.model_switch import switch_model
    from tools.delegate_tool import _resolve_delegation_credentials
    from tools.delegate_tool_config import _merge_request_overrides

    allowed = _allowed_selections(route['model_selection'])
    provider, model = route.get('provider'), route.get('model')
    if not isinstance(provider, str) or not provider.strip() or not isinstance(model, str) or not model.strip():
        raise DelegationRouteError('model_selection requires a named provider and model.')
    if any(route.get(key) is not None for key in ('base_url', 'api_key', 'api_mode', 'request_overrides')):
        raise DelegationRouteError('model_selection takes its endpoint and credentials from the owning provider.')
    full = load_config_readonly()
    result = switch_model(
        raw_input=model, current_provider='', current_model='', explicit_provider=provider,
        user_providers=full.get('providers'), custom_providers=get_compatible_custom_providers(full),
        is_global=False)
    if not result.success:
        raise DelegationRouteError(result.error_message or 'The selected model/provider route was refused.')
    if allowed is not None and (result.target_provider, result.new_model) not in allowed:
        raise DelegationRouteError('The canonical provider/model pair is not allowed by the selection policy.')
    creds = _resolve_delegation_credentials(
        {'provider': result.target_provider, 'model': result.new_model}, parent_agent)
    return {
        **creds, 'model': result.new_model, 'provider': result.target_provider,
        'base_url': result.base_url or creds.get('base_url'),
        'api_key': result.api_key or creds.get('api_key'),
        'api_mode': result.api_mode or creds.get('api_mode'),
        'request_overrides': _merge_request_overrides(creds.get('request_overrides'), result.request_overrides) or {},
    }
