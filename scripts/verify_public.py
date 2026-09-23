"""Check local docs, archive integrity and distribution boundaries without calls."""
import hashlib
import json
from pathlib import Path
import re
import xml.etree.ElementTree as ET
import zipfile

ROOT = Path(__file__).resolve().parents[1]
errors = []
count = 0
for path in ROOT.rglob('*'):
    relative = path.relative_to(ROOT)
    if '.git' in relative.parts or '__pycache__' in relative.parts or not path.is_file():
        continue
    count += 1
    if path.suffix in {'.pcm', '.wav', '.mp3', '.m4a', '.sqlite', '.db'}:
        errors.append(f'private artifact type: {relative}')
    if path.suffix in {'.py', '.md', '.svg', '.json', '.yaml', '.mjs', '.swift'}:
        text = path.read_text()
        if re.search('/' + r'Users/[^/\s]+/', text):
            errors.append(f'personal path: {relative}')
        if path.suffix == '.py':
            compile(text, str(relative), 'exec')
        if path.suffix == '.svg':
            ET.fromstring(text)
            if '<script' in text or '<foreignObject' in text:
                errors.append(f'nonstatic SVG: {relative}')
        if path.suffix == '.md':
            links = re.findall(r'\]\(([^)]+)\)|(?:src|href)="([^"]+)"', text)
            for pair in links:
                link = next(x for x in pair if x).split('#', 1)[0]
                if not link or re.match(r'[a-zA-Z]+:', link):
                    continue
                if not (path.parent / link).exists():
                    errors.append(f'missing local link: {relative}: {link}')

archive_path = ROOT / 'dist/codex-call-the-boss.skill.zip'
with zipfile.ZipFile(archive_path) as archive:
    assert archive.testzip() is None
    names = archive.namelist()
    assert len(names) == len(set(names))
    for name in names:
        path = Path(name)
        assert not path.is_absolute() and '..' not in path.parts
        assert path.parts[0] == 'codex-call-the-boss'
        assert not any(p in path.parts for p in ('.venv', '__pycache__', '.codex-phone', 'conversations'))
    manifest = json.loads(archive.read('codex-call-the-boss/package-manifest.json'))
    assert archive.read('codex-call-the-boss/package-manifest.json') == (
        ROOT / 'skills/codex-call-the-boss/package-manifest.json').read_bytes()
    assert len(names) == len(manifest['files']) + 1
    for name, digest in manifest['files'].items():
        assert hashlib.sha256(archive.read('codex-call-the-boss/' + name)).hexdigest() == digest
        source = (ROOT / name if name in {'LICENSE', 'THIRD_PARTY_NOTICES.md'}
                  else ROOT / 'skills/codex-call-the-boss' / name)
        assert hashlib.sha256(source.read_bytes()).hexdigest() == digest
    assert archive.read('codex-call-the-boss/LICENSE') == (ROOT / 'LICENSE').read_bytes()
expected = (ROOT / 'dist/SHA256SUMS').read_text().split()[0]
assert hashlib.sha256(archive_path.read_bytes()).hexdigest() == expected
assert len(list((ROOT / 'docs/assets').glob('hero-*.svg'))) == 5
assert all((ROOT / 'docs' / f'README.{lang}.md').exists() for lang in ('zh-CN', 'ru', 'ja', 'ko'))
if errors:
    raise SystemExit('\n'.join(errors))
print(json.dumps({'files_checked': count, 'archive_members': len(names),
                  'local_links': 'passed', 'python_syntax': 'passed', 'svg_xml': 'passed',
                  'archive_hashes': 'passed', 'real_call_verified': False}))
