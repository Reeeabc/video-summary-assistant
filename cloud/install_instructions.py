"""Install a scoped entry point where cloud tasks start, preserving other rules."""
import argparse
import os
from pathlib import Path
import tempfile

BEGIN = '<!-- video-summary-assistant:begin -->'
END = '<!-- video-summary-assistant:end -->'
ROOT = Path(__file__).resolve().parent.parent


def install(workspace, project=ROOT):
    workspace, project = Path(workspace).resolve(), Path(project).resolve()
    if not (project / 'AGENTS.md').is_file():
        raise ValueError('Project AGENTS.md is missing; refusing to install an entry point.')
    path = workspace / 'AGENTS.md'
    if path.is_symlink():
        raise ValueError('Workspace AGENTS.md is a symlink; review it manually.')
    original = path.read_text(encoding='utf-8') if path.exists() else ''
    if (original.count(BEGIN), original.count(END)) not in {(0, 0), (1, 1)}:
        raise ValueError('Workspace instruction markers are inconsistent; existing rules preserved.')
    fragment = (ROOT / 'cloud/workspace-AGENTS.md').read_text(encoding='utf-8')
    block = BEGIN + '\n' + fragment.replace('{{PROJECT_ROOT}}', str(project)).rstrip() + '\n' + END
    if BEGIN in original:
        start, end = original.index(BEGIN), original.index(END)
        if end < start:
            raise ValueError('Workspace instruction markers are reversed; existing rules preserved.')
        updated = original[:start] + block + original[end + len(END):]
    else:
        updated = original + ('\n\n' if original and not original.endswith('\n\n') else '') + block + '\n'
    if updated == original:
        return path
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o644
    fd, temporary = tempfile.mkstemp(prefix='.video-instructions-', dir=workspace)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(updated)
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return path


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace', type=Path, default=Path('/workspace'))
    args = parser.parse_args()
    print(install(args.workspace))
