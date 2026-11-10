"""Unsupported raw selection must never silently execute an inherited route.

Real AIAgent/registry dispatch, plugin discovery and delegate_task routing; only child
construction and execution are stubbed. The provider fixture serves synthetic loopback data.
"""

import json

import pytest

from tests.tools.test_delegation_route_fields import _construct
from tests.tools.test_explicit_delegation_routing import _B, _parent, environment

pytestmark = pytest.mark.platforms('posix')
_FIELDS = ['provider', 'model', 'base_url', 'api_key', 'api_mode']


def _capture_children(monkeypatch):
    from tools import delegate_tool
    built = []

    def construct(**kwargs):
        built.append(kwargs)
        return _construct(**kwargs)

    monkeypatch.setattr('run_agent.AIAgent', construct)
    monkeypatch.setattr(delegate_tool, '_run_batch', lambda batch, background: json.dumps(
        {'built': len(batch.children), 'refused': batch.refused}))
    return built


@pytest.mark.parametrize('dispatch', ['agent', 'registry'])
@pytest.mark.parametrize('field', _FIELDS)
@pytest.mark.parametrize('value', ['generated-unsupported-private-value', None, False])
def test_model_dispatch_refuses_raw_intent_before_filtering(environment, monkeypatch, dispatch, field, value):
    from run_agent import AIAgent
    from tools.registry import registry
    from tools.delegate_tool_routes import schema_extension

    environment.load()
    parent = _parent()
    parent._delegation_backend = 'explicit-routing'
    # The pinned backend, rather than a subsequent config edit, owns schema and guidance.
    before = schema_extension(environment.cfg['delegation'])
    environment.cfg['delegation']['backend'] = 'default'
    environment.save()
    built = _capture_children(monkeypatch)
    args = {field: value, 'tasks': [{'goal': 'nested selection', 'route': _B}, {'goal': 'ordinary selection'}]}
    requests_before = list(environment.requests)
    result = json.loads(AIAgent._dispatch_delegate_task(parent, args) if dispatch == 'agent' else
                        registry.dispatch('delegate_task', args, parent_agent=parent))
    if value is not None:
        assert 'error' in result and built == [] and environment.requests == requests_before
        assert field in result['error'] and 'explicit-routing' in result['error']
        assert 'declared per-task parameters: route' in result['error']
        assert 'generated-unsupported-private-value' not in result['error']
    else:
        assert result == {'built': 2, 'refused': []}
        assert built[0]['provider'] == _B['provider'] and built[0]['api_key'] == 'generated-route-b'
        assert built[1]['base_url'] == environment.cfg['delegation']['base_url']
        assert built[1]['api_key'] == environment.cfg['delegation']['api_key']
    assert schema_extension({**environment.cfg['delegation'], 'backend': 'explicit-routing'}) == before


@pytest.mark.parametrize('field', _FIELDS)
@pytest.mark.parametrize('value', ['generated-unsupported-private-value', None, False])
def test_direct_delegate_refuses_raw_task_without_discarding_valid_siblings(environment, monkeypatch, field, value):
    from tools.delegate_tool import delegate_task

    environment.load()
    built = _capture_children(monkeypatch)
    if value is not None:
        requests_before = list(environment.requests)
        refused_alone = json.loads(delegate_task(tasks=[{'goal': 'only raw task', field: value}],
                                                parent_agent=_parent()))
        assert 'error' in refused_alone and built == [] and environment.requests == requests_before
        assert 'generated-unsupported-private-value' not in refused_alone['error']
    result = json.loads(delegate_task(tasks=[
        {'goal': 'raw task selection', field: value},
        {'goal': 'nested selection', 'route': _B},
        {'goal': 'ordinary selection'},
    ], parent_agent=_parent()))
    if value is not None:
        assert result['built'] == 2 and len(built) == 2
        refused, = result['refused']
        assert refused['task_index'] == 0 and refused['route']['refused'] is True
        assert field in refused['error'] and 'declared per-task parameters: route' in refused['error']
        assert 'generated-unsupported-private-value' not in refused['error']
    else:
        assert result == {'built': 3, 'refused': []} and len(built) == 3
    nested, ordinary = built[-2:]
    assert nested['provider'] == _B['provider'] and nested['api_key'] == 'generated-route-b'
    assert ordinary['base_url'] == environment.cfg['delegation']['base_url']
    assert ordinary['api_key'] == environment.cfg['delegation']['api_key']
