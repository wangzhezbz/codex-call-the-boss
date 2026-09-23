"""Create a deterministic, allowlisted skill ZIP; never include local state."""
import ast
import hashlib
import json
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / 'skills/codex-call-the-boss'


def build():
    constants = {}
    wrapper = SKILL / 'scripts/call_the_boss.py'
    for node in ast.parse(wrapper.read_text()).body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name) and target.id in {'RUNTIME_FILES', 'TEST_FILES'}:
                constants[target.id] = ast.literal_eval(node.value)
    assert len(constants) == 2
    names = ['SKILL.md', 'agents/openai.yaml', 'scripts/call_the_boss.py']
    names += ['references/' + name for name in (
        'quick-start-zh.md', 'setup.md', 'operations.md', 'synchronous-stop.md', 'doubao.md')]
    names += ['assets/runtime/' + n for n in constants['RUNTIME_FILES']]
    names += ['assets/runtime/tests/' + n for n in constants['TEST_FILES']]
    data = {}
    for name in names:
        source = SKILL / name
        assert not source.is_symlink() and source.resolve().is_relative_to(SKILL.resolve())
        data[name] = source.read_bytes()
    for name in ('LICENSE', 'THIRD_PARTY_NOTICES.md'):
        data[name] = (ROOT / name).read_bytes()
    manifest = {'skill': 'codex-call-the-boss', 'release': '2026-09-23-preview',
        'private_state_included': False,
        'files': {name: hashlib.sha256(value).hexdigest() for name, value in sorted(data.items())}}
    data['package-manifest.json'] = (json.dumps(manifest, ensure_ascii=False, indent=2) + '\n').encode()
    output = ROOT / 'dist/codex-call-the-boss.skill.zip'
    output.parent.mkdir(exist_ok=True)
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for name, value in sorted(data.items()):
            info = zipfile.ZipInfo('codex-call-the-boss/' + name, (2026, 9, 23, 0, 0, 0))
            info.external_attr = 0o100644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, value)
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    (ROOT / 'dist/SHA256SUMS').write_text(digest + '  ' + output.name + '\n')
    print(json.dumps({'files': len(data), 'bytes': output.stat().st_size, 'sha256': digest}))


if __name__ == '__main__':
    build()
