# Development and package verification

These instructions do not dial, subscribe tasks, modify phone settings or run a
second writer against a desktop-owned task. Use Python 3.11+.

## Layout

```text
README.md                         English entry
docs/README.{zh-CN,ru,ja,ko}.md     Four translated entries
docs/assets/                      Five self-contained SVG headers
skills/codex-call-the-boss/        Reusable skill and installer
  assets/runtime/                 Runtime source and regression tests
scripts/                          Header renderer and allowlisted package builder
dist/                             Downloadable skill ZIP and SHA-256 checksum
```

The shipped runtime is the reviewed 2026-09-22 snapshot; 923 tests passed on the
maintainer's Mac. That is historical software-layer evidence, not a claim that
this repository has CI coverage, that every Codex release is compatible, or that
another machine/handset has passed acceptance. The observed host was newer than
the old reviewed CLI baseline; compatibility still needs explicit verification.

## Rebuild public artifacts

From the repository root:

```bash
python3 scripts/render_headers.py
python3 scripts/build_skill.py
python3 scripts/verify_public.py
```

The package builder includes only named source/reference files and the installer's
runtime/test allowlists. It excludes private state, `.venv`, Python caches, audio,
logs and task IDs. Building replaces only the generated `dist` ZIP/checksum and
header SVGs; it does not delete folders. Fixed ZIP metadata makes rebuilding the
same input deterministic. All file hashes are recorded inside the archive.

## Runtime tests

Use an isolated environment with the runtime's requirements installed, then run
`python -m unittest discover -s tests` from `skills/codex-call-the-boss/assets/runtime`.
Tests use simulated calls, not a cellular test. Some require macOS audio/system
libraries; do not advertise a Linux pass based only on Python compilation.
If recursive fixture cleanup is forbidden locally, use the existing transactional
installer's `CODEX_PHONE_RETAIN_TEST_FIXTURES=1` mode rather than changing the policy.
Do not install or stop a working phone service just to run documentation checks.

## Keep the translations honest

All five public entries need the same platform requirements, opt-in semantics,
optional provider costs, six-second confirmation limitation, one-action window,
privacy warnings and experimental label. Technical references are maintained in
English (with a Chinese getting-started guide). Five document languages must not
be presented as verified multilingual phone support.

## Before publishing changes

Check the staged file list, archive manifest and local links; run the public
validator and review changes for private data. Preserve the existing LICENSE and
review third-party provenance. Never copy the developer's entire working directory.
Use normal fast-forward updates; do not force-push over someone else's changes.
