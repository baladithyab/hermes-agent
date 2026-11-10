"""Policy incarnation detects between-check ABA without revoking selection."""
import os

from hermes_cli.plugin_policy_incarnation import policy_incarnation


def test_in_place_policy_aba_and_package_removal_are_not_same_incarnation(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    home = tmp_path / 'home'
    dashboard = home / 'plugins/probe/dashboard'
    dashboard.mkdir(parents=True)
    config = home / 'config.yaml'
    config.write_text('plugins: {enabled: [probe]}\n')
    (dashboard / 'manifest.json').write_text('{}')
    api = dashboard / 'api.py'
    api.write_text('value = 1\n')
    before = policy_incarnation(home, dashboard, 'api.py')
    # Unrelated state writes must not revoke accepted work.
    (home / 'logs').mkdir()
    assert policy_incarnation(home, dashboard, 'api.py') == before
    original, stat = config.read_bytes(), config.stat()
    config.write_text('plugins: {enabled: []}\n')
    config.write_bytes(original)
    os.utime(config, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    after = policy_incarnation(home, dashboard, 'api.py')
    assert after != before
    api.unlink()
    api.write_text('value = 1\n')
    assert policy_incarnation(home, dashboard, 'api.py') != after
