"""Old raw-routing pins adopt today's declared route without rewriting saved history."""

import copy
import json
from types import SimpleNamespace

import pytest

from tests.tools.test_delegation_route_fields import _construct
from tests.tools import test_explicit_delegation_routing as explicit_routing

_B = explicit_routing._B
_parent = explicit_routing._parent
environment = explicit_routing.environment

pytestmark = pytest.mark.platforms('posix')


def _fresh_agent(db, session_id):
    from agent.delegation_backends import adopt_built_backend, saved_backend
    from model_tools import get_tool_definitions

    definitions = [tool for tool in get_tool_definitions(enabled_toolsets=['delegation'])
                   if tool['function']['name'] == 'delegate_task']
    assert len(definitions) == 1
    agent = SimpleNamespace(tools=definitions, valid_tool_names={'delegate_task'},
                            enabled_toolsets=['delegation'], disabled_toolsets=None,
                            _session_db=db, session_id=session_id, platform='cli')
    adopt_built_backend(agent, definitions, saved_backend())
    return agent


def _task_properties(agent):
    return agent.tools[0]['function']['parameters']['properties']['tasks']['items']['properties']


def test_old_raw_pin_refreshes_repins_and_then_freezes_supported_explicit_routing(environment, monkeypatch):
    from hermes_state import SessionDB
    from run_agent import AIAgent
    from tools import delegate_tool, mcp_tool_agent

    environment.load('host')
    session_id = 'generated-old-routing-session'
    with SessionDB(db_path=environment.home / 'state.db') as db:
        db.create_session(session_id, source='tui')
        db.append_message(session_id, 'user', 'Generated historical delegation request.')
        db.append_message(session_id, 'assistant', tool_calls=[{
            'id': 'generated-old-tool-call', 'type': 'function', 'function': {
                'name': 'delegate_task', 'arguments': json.dumps({'tasks': [{
                    'goal': 'Generated historical task', **_B}]})}}])
        db.append_message(session_id, 'tool', 'Generated historical result.', tool_name='delegate_task',
                          tool_call_id='generated-old-tool-call')
        messages_before = db.get_messages(session_id)
        conversation_before = db.get_messages_as_conversation(session_id)

        upgraded = _fresh_agent(db, session_id)
        assert 'route' in _task_properties(upgraded)
        old_definitions = copy.deepcopy(upgraded.tools)
        properties = old_definitions[0]['function']['parameters']['properties']
        task_properties = properties['tasks']['items']['properties']
        task_properties.pop('route')
        for field in ('model', 'provider'):
            properties[field] = {'type': 'string'}
            task_properties[field] = {'type': 'string'}
        old_pin = {'version': 'generated-preupgrade-build', 'tools': old_definitions}
        assert old_pin['version'] != mcp_tool_agent.tool_pin_version()
        db.update_session_tool_names(session_id, old_pin)
        stored_old_pin = json.loads(db.get_session(session_id)['tool_names'])
        assert 'delegation_backend' not in stored_old_pin

        mcp_tool_agent.restore_agent_tool_prefix(upgraded, stored_old_pin)
        assert upgraded._delegation_backend == 'explicit-routing'
        assert 'route' in _task_properties(upgraded) and 'model' not in _task_properties(upgraded)
        repinned = json.loads(db.get_session(session_id)['tool_names'])
        assert repinned['version'] == mcp_tool_agent.tool_pin_version()
        assert repinned['delegation_backend'] == 'explicit-routing'
        assert repinned['tools'] == upgraded.tools and stored_old_pin == old_pin

        # A later config change cannot replace this same-version session's pinned prefix or backend.
        environment.cfg['delegation']['backend'] = 'default'
        environment.save()
        resumed = _fresh_agent(db, session_id)
        assert resumed._delegation_backend == 'default' and 'route' not in _task_properties(resumed)
        mcp_tool_agent.restore_agent_tool_prefix(resumed, repinned)
        assert resumed.tools == upgraded.tools and resumed._delegation_backend == 'explicit-routing'
        assert json.loads(db.get_session(session_id)['tool_names']) == repinned

        parent = _parent()
        parent.tools, parent._delegation_backend = resumed.tools, resumed._delegation_backend
        parent.session_id = session_id
        built = []

        def construct(**kwargs):
            built.append(kwargs)
            return _construct(**kwargs)

        monkeypatch.setattr('run_agent.AIAgent', construct)
        monkeypatch.setattr(delegate_tool, '_run_batch', lambda batch, background: json.dumps(
            {'built': len(batch.children)}))
        requests_before = list(environment.requests)
        for raw_args in ({'model': _B['model'], 'tasks': [{'goal': 'Reject stale top-level selection'}]},
                         {'tasks': [{'goal': 'Reject stale per-task selection', **_B}]}):
            refused = json.loads(AIAgent._dispatch_delegate_task(parent, raw_args))
            assert 'error' in refused and built == [] and environment.requests == requests_before
        accepted = json.loads(AIAgent._dispatch_delegate_task(parent, {
            'tasks': [{'goal': 'Use the supported explicit selection', 'route': _B}]}))
        assert accepted == {'built': 1}
        assert built[0]['provider'] == _B['provider'] and built[0]['api_key'] == 'generated-route-b'
        assert db.get_messages(session_id) == messages_before
        assert db.get_messages_as_conversation(session_id) == conversation_before
        assert json.loads(db.get_session(session_id)['tool_names']) == repinned
