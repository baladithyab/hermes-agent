"""Real delegation discovery and provider serialization, without model requests.

These are local serializer/JSON-Schema contracts, not remote API acceptance tests.
Legacy Gemini cannot express additionalProperties; runtime route policy remains
authoritative even where a provider's advertised schema is less restrictive.
"""

import copy
import json
from types import SimpleNamespace

from jsonschema import Draft202012Validator
import pytest

from tests.tools import test_explicit_delegation_routing as explicit_routing

_A = explicit_routing._A
_routes = explicit_routing._routes
explicit_environment = explicit_routing.environment

pytestmark = pytest.mark.platforms('posix')

_SERIALIZERS = (
    'gemini-json', 'gemini-legacy', 'anthropic',
    'moonshot-chat', 'openai-chat', 'openai-responses',
)
_RAW_FIELDS = {
    'provider', 'model', 'reasoning_effort', 'api_key', 'key_env',
    'base_url', 'request_overrides', 'fallback_providers', 'model_selection',
    'profile', 'acp_command', 'acp_args',
}


@pytest.fixture
def discovered_schema(explicit_environment):
    # Reuse the real bundled-plugin discovery fixture, isolated config and
    # seeded local catalog. No provider credential resolver is needed here.
    from tools import delegate_tool
    from tools.delegate_tool_routes import schema_extension
    from tools.registry import registry

    env = explicit_environment
    env.load()
    static = copy.deepcopy(delegate_tool.DELEGATE_TASK_SCHEMA)
    explicit = registry.get_definitions({'delegate_task'}, quiet=True)
    extra, _ = schema_extension(env.cfg['delegation'])
    assert explicit[0]['function']['parameters']['properties']['tasks']['items']['properties']['route'] == extra['route']
    assert extra['route']['type'] == 'object'
    assert 'nullable' not in extra['route']

    env.cfg['delegation']['backend'] = 'default'
    env.save()
    ordinary = registry.get_definitions({'delegate_task'}, quiet=True)
    assert schema_extension(env.cfg['delegation'])[0] == {}
    env.cfg['delegation']['backend'] = 'explicit-routing'
    env.save()

    yield SimpleNamespace(env=env, explicit=explicit, ordinary=ordinary)

    assert delegate_tool.DELEGATE_TASK_SCHEMA == static
    # The local fixture records both catalog and model-list HTTP requests.
    # Schema construction/conversion and refused selections must make none.
    assert env.requests == []


def _provider_parameters(serializer, tools):
    """Follow the shared model_tools phase, then the actual provider converter."""
    from tools.schema_sanitizer import sanitize_tool_schemas

    tools = sanitize_tool_schemas(tools)
    if serializer.startswith('gemini-'):
        from agent.gemini_native_adapter import (
            build_gemini_request,
            gemini_accepts_parameters_json_schema,
        )

        base = 'https://generativelanguage.googleapis.com/' + (
            'v1beta' if serializer == 'gemini-json' else 'v1')
        json_schema = gemini_accepts_parameters_json_schema(base)
        request = build_gemini_request(
            messages=[{'role': 'user', 'content': 'Local schema contract.'}],
            tools=tools, model='synthetic-gemini-model',
            tools_as_json_schema=json_schema,
        )
        declaration = request['tools'][0]['functionDeclarations'][0]
        key = 'parametersJsonSchema' if json_schema else 'parameters'
        assert declaration['name'] == 'delegate_task'
        assert ('parameters' in declaration) is not json_schema
        assert ('parametersJsonSchema' in declaration) is json_schema
        native, parameters = request, declaration[key]
    elif serializer == 'anthropic':
        from agent.transports.anthropic import AnthropicTransport

        native = AnthropicTransport().convert_tools(tools)
        assert native[0]['name'] == 'delegate_task'
        assert 'function' not in native[0] and 'parameters' not in native[0]
        parameters = native[0]['input_schema']
    elif serializer == 'openai-responses':
        from agent.codex_responses_adapter import _responses_tools

        native = _responses_tools(tools)
        assert native[0]['name'] == 'delegate_task'
        assert native[0]['strict'] is False
        assert 'function' not in native[0]
        parameters = native[0]['parameters']
    else:
        from agent.transports.chat_completions import ChatCompletionsTransport

        model = 'kimi-k2.6' if serializer == 'moonshot-chat' else 'synthetic-chat-model'
        native = ChatCompletionsTransport().build_kwargs(
            model, [{'role': 'user', 'content': 'Local schema contract.'}], tools=tools,
        )
        assert native['tools'][0]['type'] == 'function'
        assert native['tools'][0]['function']['name'] == 'delegate_task'
        parameters = native['tools'][0]['function']['parameters']

    # Exercise JSON serialization of the real request/tool envelope, not just
    # an independently reconstructed route fragment. No SDK client is created.
    assert json.loads(json.dumps(native, allow_nan=False)) == native
    return parameters


def _task_schema(parameters):
    return parameters['properties']['tasks']['items']


@pytest.mark.parametrize('serializer', _SERIALIZERS)
def test_converters_keep_explicit_fields_required_values_and_ordinary_omission(discovered_schema, serializer):
    from hermes_constants import VALID_REASONING_EFFORTS

    before = copy.deepcopy(discovered_schema.explicit)
    parameters = _provider_parameters(serializer, discovered_schema.explicit)
    ordinary = _provider_parameters(serializer, discovered_schema.ordinary)
    task = _task_schema(parameters)
    route = task['properties']['route']
    assert not (_RAW_FIELDS & task['properties'].keys())
    assert 'route' not in task.get('required', [])
    assert 'route' not in _task_schema(ordinary)['properties']
    assert set(route['properties']) == {'provider', 'model', 'reasoning_effort'}
    assert set(route['required']) == {'provider', 'model'}
    assert route['properties']['provider']['type'] == 'string'
    assert route['properties']['model']['type'] == 'string'
    effort = route['properties']['reasoning_effort']
    assert effort['type'] == 'string'
    assert set(effort['enum']) == {'none', *VALID_REASONING_EFFORTS}
    assert route['type'] == 'object'
    assert 'nullable' not in route
    assert 'anyOf' not in route
    if serializer == 'gemini-legacy':
        assert 'additionalProperties' not in route
    else:
        assert route['additionalProperties'] is False

    # A real standards validator checks the common JSON-Schema subset. It is
    # deliberately not presented as a Gemini OpenAPI or hosted API validator.
    Draft202012Validator.check_schema(parameters)
    validator = Draft202012Validator(parameters)
    assert validator.is_valid({'tasks': [{'goal': 'ordinary'}]})
    assert validator.is_valid({'tasks': [{'goal': 'explicit', 'route': _A}]})
    for invalid in ({'model': _A['model']}, {**_A, 'provider': 7}, {**_A, 'reasoning_effort': 'unsupported'}):
        assert not validator.is_valid({'tasks': [{'goal': 'invalid', 'route': invalid}]})
    assert discovered_schema.explicit == before


@pytest.mark.parametrize('serializer', _SERIALIZERS)
def test_backend_keeps_closed_route_policy_when_wire_schema_is_lossy(discovered_schema, serializer):
    from agent.delegation_provider import DelegationRouteError
    from agent.delegation_registry import get_active_provider

    parameters = _provider_parameters(serializer, discovered_schema.explicit)
    assert 'route' not in _task_schema(parameters).get('required', [])
    env = discovered_schema.env
    provider = get_active_provider(env.cfg['delegation'])
    absent = provider.route_task({'goal': 'ordinary'}, env.cfg['delegation'])
    explicit_null = provider.route_task({'goal': 'ordinary', 'route': None}, env.cfg['delegation'])
    assert absent == explicit_null
    assert absent['base_url'] == env.cfg['delegation']['base_url']

    rejected = [
        {**_A, 'api_key': 'synthetic-injected'},
        {**_A, 'key_env': 'SYNTHETIC_UNOWNED_KEY'},
        {**_A, 'base_url': 'http://127.0.0.1:9/injected'},
        {**_A, 'model_selection': True},
        {**_A, 'profile': 'another-profile'},
        {**_A, 'request_overrides': {}},
        {**_A, 'fallback_providers': []},
        {**_A, 'reasoning_effort': 'unsupported'},
        {**_A, 'model': 'outside-operator-allowlist'},
    ]
    for route in rejected:
        # Real plugin and core task routing refuse before credentials, catalog
        # resolution or inference. No mock validator/provider supplies denial.
        with pytest.raises(DelegationRouteError):
            provider.route_task({'goal': 'refuse', 'route': route}, env.cfg['delegation'])
        refused, = _routes(env, [{'goal': 'refuse', 'route': route}])
        assert refused.error

    for field in ('model_selection', 'request_overrides', 'fallback_providers'):
        refused, = _routes(env, [{'goal': 'refuse raw task field', field: 'synthetic-injected'}])
        assert refused.error
