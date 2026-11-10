"""An explicit allowlist bounds fallback normalization and provider-owned endpoints."""

import pytest

from tests.tools import test_explicit_delegation_routing as explicit_routing

explicit_environment = explicit_routing.environment
pytestmark = pytest.mark.platforms('posix')


@pytest.mark.parametrize('placement', ['bundled', 'host'])
@pytest.mark.parametrize('provider,raw,canonical', [
    ('deepseek', 'deepseek-reasoner', 'deepseek-flash'),
    ('openrouter', 'gpt-4.1', 'openai/gpt-4.1'),
])
@pytest.mark.parametrize('allow_canonical', [False, True])
def test_fallback_needs_both_configured_and_effective_pairs(explicit_environment, placement, provider, raw, canonical, allow_canonical):
    environment = explicit_environment
    fallback = {'provider': provider, 'model': raw}
    config = environment.cfg['delegation']
    allowed = config['backends']['explicit-routing']['allowed']
    allowed.append(fallback.copy())
    if allow_canonical:
        allowed.append({'provider': provider, 'model': canonical})
    config['fallback_providers'] = [fallback]
    environment.load(placement)
    before = list(environment.requests)
    route, = explicit_routing._routes(environment, [{'goal': 'bounded fallback', 'route': explicit_routing._A}])
    if not allow_canonical:
        assert 'canonical fallback provider/model pair' in route.error
        assert environment.requests == before
    else:
        assert not route.error
        assert route.routing_cfg['fallback_providers'] == [fallback]
        from hermes_cli.model_normalize import normalize_model_for_provider
        effective = normalize_model_for_provider(fallback['model'], fallback['provider'])
        assert effective == canonical and {'provider': provider, 'model': effective} in allowed


@pytest.mark.parametrize('placement', ['bundled', 'host'])
@pytest.mark.parametrize('field,value', [
    ('base_url', 'http://127.0.0.1:9/generated-other-endpoint'),
    ('api_key', 'generated-forbidden-override'),
    ('key_env', 'GENERATED_FORBIDDEN_KEY'),
    ('api_key_env', 'GENERATED_FORBIDDEN_KEY_ALIAS'),
])
def test_allowlisted_fallback_cannot_override_its_provider_endpoint_or_key(explicit_environment, placement, field, value):
    environment = explicit_environment
    environment.cfg['delegation']['fallback_providers'] = [{**explicit_routing._B, field: value}]
    environment.load(placement)
    before = list(environment.requests)
    route, = explicit_routing._routes(environment, [{'goal': 'refuse fallback override', 'route': explicit_routing._A}])
    assert 'owning providers' in route.error and value not in route.error
    assert environment.requests == before


def test_provider_profile_alias_is_checked_by_the_same_core_normalizer(explicit_environment, monkeypatch):
    from types import SimpleNamespace
    import providers
    environment = explicit_environment
    fallback = {**explicit_routing._B, 'model': 'profile-alias'}
    config = environment.cfg['delegation']
    allowed = config['backends']['explicit-routing']['allowed']
    allowed.append(fallback.copy())
    config['fallback_providers'] = [fallback]
    environment.load()
    original_profile = providers.get_provider_profile
    profile = SimpleNamespace(normalize_model_id=lambda model: 'canonical-profile-model' if model == 'profile-alias' else None)
    monkeypatch.setattr(providers, 'get_provider_profile',
                        lambda provider: profile if provider == fallback['provider'] else original_profile(provider))
    before = list(environment.requests)
    route, = explicit_routing._routes(environment, [{'goal': 'refuse normalized profile alias', 'route': explicit_routing._A}])
    assert 'canonical fallback provider/model pair' in route.error
    assert environment.requests == before
    allowed.append({'provider': fallback['provider'], 'model': 'canonical-profile-model'})
    environment.save()
    route, = explicit_routing._routes(environment, [{'goal': 'allowed normalized profile alias', 'route': explicit_routing._A}])
    assert not route.error and route.routing_cfg['fallback_providers'] == [fallback]
