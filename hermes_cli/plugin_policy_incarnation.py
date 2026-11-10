"""Pin private operation authority to the policy/package filesystem incarnation.

Boolean enablement sampling misses disable/re-enable between checks. Until a
policy-specific durable revision exists, any policy rewrite conservatively
invalidates accepted work, even when the final bytes equal the original bytes.
Selection changes do not write these files and therefore do not revoke work.
"""
from pathlib import Path
import stat as stat_module


def policy_incarnation(home, dashboard_dir, api_file):
    from hermes_cli.managed_scope import get_managed_dir

    dashboard = Path(dashboard_dir)
    paths = [Path(home), Path(home) / 'config.yaml', dashboard.parent,
             dashboard / 'manifest.json', dashboard / api_file]
    managed = get_managed_dir()
    if managed is not None:
        paths.extend((Path(managed), Path(managed) / 'config.yaml'))
    result: list[object] = [str(managed) if managed is not None else None]
    for path in paths:
        try:
            stat = path.lstat()
        except FileNotFoundError:
            result.append(None)
        else:
            # ctime catches in-place ABA even if mtime and contents are restored;
            # inode/device catch atomic replacements and package reincarnations.
            if stat_module.S_ISDIR(stat.st_mode):
                # Creating unrelated state beneath a home is not policy revocation.
                result.append((stat.st_dev, stat.st_ino, stat.st_mode))
            else:
                result.append((stat.st_dev, stat.st_ino, stat.st_mode, stat.st_size,
                               stat.st_mtime_ns, stat.st_ctime_ns))
    return tuple(result)
