"""Plugin-specific revocation survives unrelated edits and relevant ABA writes."""
import copy

import pytest

from hermes_cli.config import atomic_config_replace
from hermes_cli.plugin_policy_epoch import read_policy_epoch, record_policy_bytes


def enabled_config():
    return {'plugins': {'enabled': ['hermes-cpa'], 'disabled': [],
                        'entries': {'hermes-cpa': {'settings': {'mode': 'managed'}}}}}


def test_unrelated_model_and_other_plugin_changes_preserve_cpa_epoch(tmp_path):
    config = enabled_config()
    path = tmp_path / 'config.yaml'
    atomic_config_replace(path, config)
    original = read_policy_epoch(tmp_path, 'hermes-cpa')
    config['model'] = {'provider': 'generated-provider', 'default': 'generated-model'}
    config['plugins']['enabled'].append('other-plugin')
    config['plugins']['entries']['other-plugin'] = {'settings': {'sample_rate': 1}}
    atomic_config_replace(path, config)
    assert read_policy_epoch(tmp_path, 'hermes-cpa') == original
    other = read_policy_epoch(tmp_path, 'other-plugin')
    config['plugins']['entries']['other-plugin']['settings']['sample_rate'] = 2
    atomic_config_replace(path, config)
    assert read_policy_epoch(tmp_path, 'hermes-cpa') == original
    assert read_policy_epoch(tmp_path, 'other-plugin') != other


def test_disable_reenable_cannot_restore_old_epoch(tmp_path):
    original_config = enabled_config()
    path = tmp_path / 'config.yaml'
    atomic_config_replace(path, original_config)
    original = read_policy_epoch(tmp_path, 'hermes-cpa')
    disabled = copy.deepcopy(original_config)
    disabled['plugins']['disabled'] = ['hermes-cpa']
    atomic_config_replace(path, disabled)
    revoked = read_policy_epoch(tmp_path, 'hermes-cpa')
    assert revoked != original
    atomic_config_replace(path, original_config)
    restored = read_policy_epoch(tmp_path, 'hermes-cpa')
    assert restored not in (original, revoked)


def test_removal_reinstall_and_mode_change_revoke_epoch(tmp_path):
    config = enabled_config()
    path = tmp_path / 'config.yaml'
    atomic_config_replace(path, config)
    original = read_policy_epoch(tmp_path, 'hermes-cpa')
    atomic_config_replace(path, {})
    removed = read_policy_epoch(tmp_path, 'hermes-cpa')
    assert removed != original
    atomic_config_replace(path, config)
    reinstalled = read_policy_epoch(tmp_path, 'hermes-cpa')
    assert reinstalled not in (original, removed)
    config['plugins']['entries']['hermes-cpa']['settings']['mode'] = 'external'
    atomic_config_replace(path, config)
    assert read_policy_epoch(tmp_path, 'hermes-cpa') != reinstalled


def test_unreadable_epoch_does_not_allow_config_publication(tmp_path):
    path = tmp_path / 'config.yaml'
    config = enabled_config()
    atomic_config_replace(path, config)
    before = path.read_bytes()
    (tmp_path / '.plugin-policy-epochs.json').write_text('{invalid-json')
    assert read_policy_epoch(tmp_path, 'hermes-cpa') is None
    config['model'] = {'default': 'generated-other-model'}
    with pytest.raises(ValueError):
        atomic_config_replace(path, config)
    assert path.read_bytes() == before


def test_serialized_pm_policy_disable_reenable_revokes_epoch(tmp_path):
    enabled = b'plugins:\n  enabled: [hermes-cpa]\n  disabled: []\n'
    disabled = b'plugins:\n  enabled: [hermes-cpa]\n  disabled: [hermes-cpa]\n'
    record_policy_bytes(tmp_path, None, enabled)
    original = read_policy_epoch(tmp_path, 'hermes-cpa')
    record_policy_bytes(tmp_path, enabled, disabled)
    revoked = read_policy_epoch(tmp_path, 'hermes-cpa')
    record_policy_bytes(tmp_path, disabled, enabled)
    assert read_policy_epoch(tmp_path, 'hermes-cpa') not in (original, revoked)
