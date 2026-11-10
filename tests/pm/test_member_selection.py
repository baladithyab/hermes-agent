"""The workspace and admission paths agree on scoped keys and disabled plugins."""
from pathlib import Path
import sys
import tomllib

import pytest

from pm import workspace
from pm.workspace import enabled_member_dirs


def test_scoped_members_respect_disabled_and_keep_other_profiles(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HERMES_HOME", str(home))
    current = home / "plugins" / "group" / "plugin"
    other_home = home / "profiles" / "other"
    other = other_home / "plugins" / "same"
    for member in (current, other):
        member.mkdir(parents=True)
        (member / "pyproject.toml").write_text('[project]\nname="test"\nversion="1"\ndependencies=["requests>=2"]\n')
    (home / "config.yaml").write_text('plugins:\n  enabled: [group/plugin]\n  disabled: []\n')
    (other_home / "config.yaml").write_text('plugins:\n  enabled: [same]\n')
    assert set(enabled_member_dirs()) == {current, other}
    (home / "config.yaml").write_text('plugins:\n  enabled: [group/plugin]\n  disabled: [group/plugin]\n')
    assert enabled_member_dirs() == [other]


def test_manifest_only_member_is_named_after_its_plugin_dir_and_stays_unique(tmp_path):
    """uv's conflict text names the workspace member (``hermes-plugin-<key> depends on …``);
    a bare path hash left the user with nothing to disable. Two same-named plugins from
    different homes must still be distinct members."""
    import tomllib
    from pm.workspace import _workspace_member

    members = []
    for home in ("home-a", "home-b"):
        plugin = tmp_path / home / "plugins" / "My Plugin!"
        plugin.mkdir(parents=True)
        (plugin / "plugin.yaml").write_text("name: my-plugin\npip_dependencies: [left-pad-py]\n", encoding="utf-8")
        root = tmp_path / f"gen-{home}"
        root.mkdir()
        members.append(_workspace_member(plugin, root, identity=plugin))
    names = [tomllib.loads((m / "pyproject.toml").read_text(encoding="utf-8"))["project"]["name"] for m in members]
    assert all(name.startswith("hermes-plugin-my-plugin-") for name in names), names
    assert names[0] != names[1]
    assert members[0].name != members[1].name


def test_pyproject_member_is_renamed_by_its_key_and_stays_unique(tmp_path):
    """A plugin that ships its own pyproject declares its real [project].name, and
    uv identifies workspace members by that name — enabling the plugin in two
    profiles then fails ``uv lock`` with "Two workspace members are both named
    …". Rename metadata-only members by their unique key, like manifest-only
    ones; a buildable member keeps the name its package metadata reports."""
    import tomllib
    from pm.workspace import _workspace_member

    members = []
    for home in ("home-a", "home-b"):
        plugin = tmp_path / home / "plugins" / "hindsight"
        plugin.mkdir(parents=True)
        (plugin / "pyproject.toml").write_text(
            '[project]\nname = "hermes-plugin-hindsight"\nversion = "1.0.0"\n'
            'dependencies = ["hindsight-client>=0.10.1"]\n',
            encoding="utf-8",
        )
        root = tmp_path / f"gen-{home}"
        root.mkdir()
        members.append(_workspace_member(plugin, root, identity=plugin))
    names = [tomllib.loads((m / "pyproject.toml").read_text(encoding="utf-8"))["project"]["name"] for m in members]
    assert all(name.startswith("hermes-plugin-hindsight-") for name in names), names
    assert names[0] != names[1]
    document = tomllib.loads((members[0] / "pyproject.toml").read_text(encoding="utf-8"))
    assert document["project"]["version"] == "1.0.0"
    assert document["project"]["dependencies"] == ["hindsight-client>=0.10.1"]


def test_buildable_pyproject_member_keeps_its_declared_name(tmp_path):
    """uv verifies a buildable member's [project].name against the package metadata
    its backend produces, so renaming it breaks the build ("Package metadata name
    … does not match given name"); only metadata-only members may be renamed."""
    import tomllib
    from pm.workspace import _workspace_member

    plugin = tmp_path / "home" / "plugins" / "replay"
    plugin.mkdir(parents=True)
    (plugin / "pyproject.toml").write_text(
        '[project]\nname = "replay-plugin"\nversion = "1.0"\n'
        '[build-system]\nrequires = []\nbuild-backend = "backend"\n',
        encoding="utf-8",
    )
    root = tmp_path / "gen"
    root.mkdir()
    member = _workspace_member(plugin, root, identity=plugin)
    assert (member / "pyproject.toml").read_text(encoding="utf-8") == (
        plugin / "pyproject.toml").read_text(encoding="utf-8")


def test_virtual_member_without_project_metadata_gets_a_version(tmp_path):
    """A metadata-only member whose pyproject declares no [project] (lint config
    only) is emitted as ``[project] name = …``; uv rejects a [project] table
    without ``version`` (or ``dynamic``) at ``uv lock`` time, aborting the whole
    update. Generated virtual members carry the same inert version plugin-deps
    use."""
    import tomllib
    from pm.workspace import _workspace_member

    plugin = tmp_path / "home" / "plugins" / "hermes-lcm"
    plugin.mkdir(parents=True)
    (plugin / "pyproject.toml").write_text('[tool.ruff]\ntarget-version = "py311"\n', encoding="utf-8")
    root = tmp_path / "gen"
    root.mkdir()
    member = _workspace_member(plugin, root, identity=plugin)
    project = tomllib.loads((member / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert project["version"] == "0.0.0"


def test_virtual_member_declaring_dynamic_version_is_left_alone(tmp_path):
    """PEP 621 forbids supplying ``project.version`` while it is listed under
    ``project.dynamic`` ("You cannot provide a value for project.version and
    list it under project.dynamic at the same time"). A metadata-only member
    may legitimately declare ``dynamic = ["version"]``; the generated member
    must not gain a static version in that case, or ``uv lock`` rejects the
    conflicted manifest."""
    import tomllib
    from pm.workspace import _workspace_member

    plugin = tmp_path / "home" / "plugins" / "dynver"
    plugin.mkdir(parents=True)
    (plugin / "pyproject.toml").write_text(
        '[project]\nname = "dynver"\ndynamic = ["version"]\n', encoding="utf-8"
    )
    root = tmp_path / "gen"
    root.mkdir()
    member = _workspace_member(plugin, root, identity=plugin)
    project = tomllib.loads((member / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert "version" not in project
    assert project["dynamic"] == ["version"]


def test_buildable_member_without_a_version_gets_one_and_keeps_its_name(tmp_path):
    """uv rejects a [project] table with no version at parse time, buildable or not.
    A buildable member still keeps its declared name (uv checks it against its backend)."""
    import tomllib
    from pm.workspace import _workspace_member

    plugin = tmp_path / "home" / "plugins" / "noversion"
    plugin.mkdir(parents=True)
    (plugin / "pyproject.toml").write_text(
        '[project]\nname = "noversion-plugin"\n'
        '[build-system]\nrequires = []\nbuild-backend = "backend"\n',
        encoding="utf-8",
    )
    root = tmp_path / "gen"
    root.mkdir()
    member = _workspace_member(plugin, root, identity=plugin)
    project = tomllib.loads((member / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert project == {"name": "noversion-plugin", "version": "0.0.0"}


def test_tooling_only_pyproject_with_dev_group_stages_the_manifest_dependencies(tmp_path):
    """A pyproject with no [project] table (lint config plus a dev group) is not the plugin's
    package: the generated member carries plugin.yaml's dependencies, not the dev group."""
    import tomllib
    from pm.workspace import _workspace_member

    plugin = tmp_path / "home" / "plugins" / "hermes-lcm"
    plugin.mkdir(parents=True)
    (plugin / "plugin.yaml").write_text("name: hermes-lcm\npip_dependencies: ['fixturedep>=1,<2']\n",
                                        encoding="utf-8")
    (plugin / "pyproject.toml").write_text(
        '[tool.ruff]\ntarget-version = "py311"\n[dependency-groups]\ndev = ["ruff"]\n', encoding="utf-8")
    root = tmp_path / "gen"
    root.mkdir()
    document = tomllib.loads((_workspace_member(plugin, root, identity=plugin) / "pyproject.toml").read_text(
        encoding="utf-8"))
    assert document["project"]["dependencies"] == ["fixturedep>=1,<2"]
    assert "dependency-groups" not in document


@pytest.mark.parametrize("kind,readonly", [
    ("buildable", None), ("package", None), ("virtual", None), ("manifest", None),
    ("buildable", "root"), ("buildable", "child"),
])
def test_workspace_shares_buildable_inputs_without_merging_home_selections(tmp_path, monkeypatch, kind, readonly):
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    homes = [home, home / "profiles" / "first", home / "profiles" / "second",
             home / "profiles" / "disabled"]
    plugins = []
    read_only_sources = {}
    for index, owner in enumerate(homes):
        plugin = owner / "plugins" / "shared"
        plugin.mkdir(parents=True)
        disabled = "[shared]" if index == 3 else "[]"
        (owner / "config.yaml").write_text(
            f"plugins:\n  enabled: [shared]\n  disabled: {disabled}\n", encoding="utf-8",
        )
        (owner / ".env").write_text("SENTINEL=unchanged\n", encoding="utf-8")
        if kind == "manifest":
            (plugin / "plugin.yaml").write_text(
                'name: shared\npython_dependencies: ["example>=1,<2"]\n', encoding="utf-8",
            )
        else:
            backend = {
                "buildable": '[build-system]\nrequires=[]\nbuild-backend="backend"\n',
                "package": "[tool.uv]\npackage=true\n",
                "virtual": "",
            }[kind]
            (plugin / "pyproject.toml").write_text(
                '[project]\nname="Shared_Plugin"\nversion="1"\n'
                'dependencies=["example>=1,<2"]\n' + backend, encoding="utf-8",
            )
            (plugin / "code.py").write_text("VALUE = 1\n", encoding="utf-8")
        if readonly:
            directory = plugin if readonly == "root" else plugin / "package"
            directory.mkdir(exist_ok=True)
            (directory / "readonly.py").write_text("VALUE = 2\n", encoding="utf-8")
            directory.chmod(0o555)
            read_only_sources[directory] = directory.stat().st_mode
        plugins.append(plugin)
    before = {path: path.read_bytes() for owner in homes
              for path in (owner / "config.yaml", owner / ".env")}
    core = tmp_path / "core"
    core.mkdir()
    (core / "pyproject.toml").write_text(
        '[project]\nname="core"\nversion="1"\n[tool.uv]\npackage=false\n', encoding="utf-8",
    )

    selected = enabled_member_dirs()
    assert set(selected) == set(plugins[:3])
    root = tmp_path / "workspace"
    workspace._generate_pyproject(selected, root, source=core, python=Path(sys.executable))
    members = tomllib.loads((root / "pyproject.toml").read_text())["tool"]["uv"]["workspace"]["members"]
    documents = [tomllib.loads((root / member / "pyproject.toml").read_text()) for member in members]
    if kind in {"buildable", "package"}:
        assert [document["project"]["name"] for document in documents] == ["Shared_Plugin"]
    else:
        assert len({document["project"]["name"] for document in documents}) == 3
    assert all(document["project"]["dependencies"] == ["example>=1,<2"] for document in documents)
    assert set(enabled_member_dirs()) == set(plugins[:3])
    assert {entry[2] for entry in workspace.enabled_plugin_entries(
        proposed_home=homes[1], enabled=["shared"], disabled=["shared"],
    )} == {plugins[0], plugins[2]}
    assert all(path.read_bytes() == data for path, data in before.items())
    assert all(directory.stat().st_mode == mode for directory, mode in read_only_sources.items())
