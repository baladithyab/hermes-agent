"""Explicit per-task choices, bounded by the owning profile's operator policy."""
from __future__ import annotations

from collections.abc import Mapping
import re
from typing import Any

from agent.delegation_provider import DelegationProvider, DelegationRouteError

_FIELDS = frozenset({'provider', 'model', 'reasoning_effort'})
_PROVIDER = re.compile(r'[a-z][a-z0-9_.:-]{0,127}')


def _selection(value: Any) -> dict[str, str]:
    if (not isinstance(value, Mapping) or set(value) - _FIELDS
            or not {'provider', 'model'} <= set(value)):
        raise DelegationRouteError('route requires only provider, model and optional reasoning_effort.')
    provider, model = value['provider'], value['model']
    if not isinstance(provider, str) or not _PROVIDER.fullmatch(provider):
        raise DelegationRouteError('route.provider must be an exact provider ID.')
    if (not isinstance(model, str) or not 1 <= len(model) <= 256
            or any(char.isspace() or ord(char) < 32 for char in model)):
        raise DelegationRouteError('route.model must be one nonempty exact model ID.')
    result = {'provider': provider, 'model': model}
    if 'reasoning_effort' in value:
        from hermes_constants import VALID_REASONING_EFFORTS
        effort = value['reasoning_effort']
        if not isinstance(effort, str) or effort not in ('none', *VALID_REASONING_EFFORTS):
            raise DelegationRouteError('route.reasoning_effort is unsupported.')
        result['reasoning_effort'] = effort
    return result


def _model_ids(entry: Mapping[str, Any]) -> set[str]:
    raw = entry.get('models')
    result = {key for key in raw if isinstance(key, str) and not key.startswith('__')} if isinstance(raw, Mapping) else set()
    if isinstance(raw, list):
        for row in raw:
            model = row if isinstance(row, str) else row.get('id') if isinstance(row, Mapping) else None
            if isinstance(model, str):
                result.add(model)
    for key in ('default_model', 'model'):
        if isinstance(entry.get(key), str) and entry[key]:
            result.add(entry[key])
    return result


def _check_inventory(route: Mapping[str, str], full: Mapping[str, Any]) -> None:
    """Read only local catalog/config metadata; never probe or resolve credentials."""
    from hermes_cli.config import is_provider_enabled
    from hermes_cli.providers import custom_provider_aliases, get_provider
    from hermes_cli.models import static_catalog
    from hermes_cli.runtime_provider_custom import named_custom_provider_config_path
    from providers import get_provider_profile

    provider, model = route['provider'], route['model']
    blocks = full.get('providers')
    for key, block in blocks.items() if isinstance(blocks, Mapping) else ():
        if (isinstance(block, Mapping) and not is_provider_enabled(block)
                and provider in custom_provider_aliases(str(block.get('name') or key), str(key))):
            raise DelegationRouteError('The selected provider is disabled in this profile.')
    path = named_custom_provider_config_path(provider, dict(full))
    entry = None
    if path is not None:
        entry = full
        for key in path:
            entry = entry[key]
        if not isinstance(entry, Mapping) or not is_provider_enabled(entry):
            raise DelegationRouteError('The selected provider is disabled in this profile.')
        known = _model_ids(entry)
    else:
        definition = get_provider(provider, allow_network=False)
        if definition is None:
            raise DelegationRouteError('The selected provider is unknown in this profile.')
        known = set(static_catalog(provider))
    if model not in known:
        raise DelegationRouteError('The selected model is unknown in the profile\'s offline provider inventory.')
    if 'reasoning_effort' in route:
        from agent.reasoning_effort import route_supported_efforts
        profile = get_provider_profile(provider)
        declared = profile.supported_reasoning_efforts(model) if profile is not None else None
        if declared is None and profile is not None and profile.setup_reasoning_efforts is not None:
            declared = profile.setup_reasoning_efforts(model)
        mode = entry.get('api_mode') if entry is not None else getattr(profile, 'api_mode', None)
        supported = declared if declared is not None else route_supported_efforts(provider, model, mode)
        if route['reasoning_effort'] not in supported:
            raise DelegationRouteError('The selected reasoning effort is unsupported on this provider/model route.')


class ExplicitRouting(DelegationProvider):
    @property
    def name(self) -> str:
        return 'explicit-routing'

    @property
    def display_name(self) -> str:
        return 'Explicit model/provider'

    @property
    def summary(self) -> str:
        return 'Choose an operator-allowed model/provider pair for each task.'

    def task_parameters(self, config: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
        # Constant schema: constructing it never reads credentials, calls providers,
        # or changes a running conversation's cached tool prefix.
        from hermes_constants import VALID_REASONING_EFFORTS
        return {'route': {'type': ['object', 'null'], 'additionalProperties': False,
                          'required': ['provider', 'model'], 'properties': {
                              'provider': {'type': 'string'}, 'model': {'type': 'string'},
                              'reasoning_effort': {'type': 'string', 'enum': ['none', *VALID_REASONING_EFFORTS]},
                          }, 'description': 'Explicit operator-allowed provider/model pair; no keys or endpoints.'}}

    def description(self, config: Mapping[str, Any]) -> str:
        return ('For an explicit choice use tasks[].route with provider, model and optional reasoning_effort. '
                'The owning profile must authorize the exact provider/model pair; omitted or null route uses ordinary delegation.')

    def route_task(self, task: Mapping[str, Any], config: Mapping[str, Any]) -> dict[str, Any]:
        if 'route' not in task:
            return {key: value for key, value in config.items() if key != 'service_tier'}
        selected = _selection(task['route'])
        raw_allowed = self.settings(config).get('allowed', [])
        if not isinstance(raw_allowed, list):
            raise DelegationRouteError('explicit-routing.allowed must be a list of exact provider/model pairs.')
        allowed = set()
        for row in raw_allowed:
            if not isinstance(row, Mapping) or set(row) != {'provider', 'model'}:
                raise DelegationRouteError('Each explicit-routing.allowed entry must contain only provider and model.')
            normalized = _selection(row)
            allowed.add((normalized['provider'], normalized['model']))
        from hermes_cli.config import load_config_readonly
        full = load_config_readonly()

        def authorize(route):
            if (route['provider'], route['model']) not in allowed:
                raise DelegationRouteError('This exact provider/model pair is not allowed by explicit-routing policy.')
            _check_inventory(route, full)

        authorize(selected)
        raw_fallback = config.get('fallback_providers')
        if raw_fallback is None:
            fallback = []  # A pinned choice never borrows the parent's chain.
        else:
            if not isinstance(raw_fallback, list):
                raise DelegationRouteError('Explicit routing requires fallback_providers to be a list.')
            fallback = []
            for row in raw_fallback:
                # Every fallback is provider-owned too. Reject endpoint/key/profile
                # injection rather than silently dropping operator policy fields.
                value = _selection(row)
                authorize(value)
                fallback.append(value)
        # Do not merge delegation/default or parent endpoint/key/overrides. Core
        # resolves this named provider in the activity's existing profile scope.
        return {**selected, 'fallback_providers': fallback}


def register(ctx) -> None:
    ctx.register_delegation_provider(ExplicitRouting())
