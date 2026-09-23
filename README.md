<p align="center"><img src="docs/assets/hero-en.svg" alt="Call the Boss — The work is done. Your phone rings." width="100%"></p>

<p align="center"><strong>English</strong> · <a href="docs/README.zh-CN.md">简体中文</a> · <a href="docs/README.ru.md">Русский</a> · <a href="docs/README.ja.md">日本語</a> · <a href="docs/README.ko.md">한국어</a></p>

<p align="center"><a href="dist/codex-call-the-boss.skill.zip">Download the skill</a> · <a href="#get-started">Get started</a> · <a href="#current-limits">Current limits</a> · <a href="docs/ROADMAP.md">Roadmap / 路线图 / План / ロードマップ / 로드맵</a> · <a href="LICENSE">MIT</a></p>

# Call the Boss

Let Codex call you when the work is done. Get a brief update, ask questions, and give your next task by phone—all connected to the original Codex task.

**Experimental · macOS + iPhone · Explicit per-task opt-in**

| macOS + iPhone | Windows | Linux |
| :-- | :-- | :-- |
| Local skill; setup required | Not supported | Not supported |

This is a local phone bridge, not a hosted calling service or a standalone mobile app. The five languages above cover public documentation, **not verified five-language phone conversations**. Internal maintenance references remain primarily English.

## How it works

1. Codex finishes work in an enabled task and prepares a short spoken report.
2. Your Mac places one call using iPhone call relay. After pickup and your greeting, it plays that report.
3. Ask questions normally. Recognition and answers use your existing Codex login; the selected speech renderer supplies the voice.
4. Give one next task. Its original words return to the **same task**, where Codex executes within its normal permissions.
5. When that work finishes, the next completion can trigger one new call.

The voice context answers questions; it does not become a separate task executor. Call delivery, command delivery, execution, and completion are tracked separately.

## Get started

### Another task on your configured Mac

Download the [skill ZIP](dist/codex-call-the-boss.skill.zip), attach it to the target Codex task, and say:

> Read the attached SKILL.md. Reuse this Mac's existing phone configuration and enable completion calls and phone commands only for this task. Check readiness first; guide me through anything missing. Do not change other tasks' subscriptions.

If the skill is already installed, you can simply invoke `$codex-call-the-boss` with that request. Importing the archive alone does not enable calling. A running call or pending queue is not a reason to overwrite or restart the shared runtime.

### A new Mac

You need Phone.app, a signed-in Codex desktop app, Python 3.11+, iPhone call relay, a reachable receiving number different from the outgoing line, BlackHole 2ch/16ch, and the necessary Accessibility permissions. The target number must have an identifiable Call action in Phone.app's recents. The Mac and Codex must remain awake and running.

Extract the ZIP and ask Codex to read its `SKILL.md` and guide setup. The installer uses pinned runtime dependencies and validates a new version before activation. Installing drivers or dependencies, changing permissions, and selecting a potentially billed speech provider require your approval.

For a read-only inventory, from the extracted `codex-call-the-boss` folder:

```bash
python3 scripts/call_the_boss.py plan
```

After installation, `doctor` checks local readiness. Only after configuration and an explicit request does `enable` subscribe the current task. Follow [setup](skills/codex-call-the-boss/references/setup.md) and the [synchronous Stop route](skills/codex-call-the-boss/references/synchronous-stop.md); do not use a rejected private relay or a second writer for the source task.

## Voice and costs

- Recognition and answers use the signed-in Codex account and its limits; no OpenAI API key is required by this path.
- Calls use your existing mobile service. This is not a promise of free calls or unlimited account usage.
- System speech is the default. Optional Doubao TTS can incur separate charges and requires your own credentials and approval. Its integration pins **Doubao speech synthesis 2.0 / `seed-tts-2.0`**; see [Doubao setup](skills/codex-call-the-boss/references/doubao.md).
- No credentials, phone numbers, login sessions, voice caches, or call recordings are distributed.

## Current limits

- **Not production-ready.** Runtime tests are not handset or clean-machine acceptance.
- The official synchronous Stop route waits for up to 480 seconds by default. It cannot wake a closed or sleeping Codex task. Each call can submit one action; an already submitted command cannot be immediately cancelled through that one-shot window.
- The full “started” receipt requires exact-task processing evidence within six seconds. Slow host reporting may still produce a delivery-only receipt even though the task later starts.
- One attempt per completion, no automatic redial of a failed or uncertain call. Routine “still processing” filler is off; real failures are still reported.
- Speech, interruption handling, network conditions, account quotas, and host-version compatibility still need real-call verification.
- The receiving person is not authenticated. Do not use shared or forwarded numbers for unattended sensitive commands.
- Phone records remain in local private state until the owner removes them; no automatic retention policy is implied. Do not upload that state directory.
- The skill does not supply unrelated task tools, such as a native fullscreen celebration effect.

## Develop and report problems

The reusable skill is under [`skills/codex-call-the-boss`](skills/codex-call-the-boss). Its runtime and tests are under `assets/runtime`. [Development, validation, and packaging](docs/DEVELOPMENT.md) describes the no-call checks. [Security guidance](SECURITY.md) explains what **not** to put in an issue.

Document translations are welcome. Keep limitations and setup steps aligned across all five editions; do not translate feature claims into stronger promises.

## License and attribution

The repository's existing [MIT license](LICENSE) is preserved. External dependencies and vendor examples have their own terms; see [third-party notices](THIRD_PARTY_NOTICES.md). This is an independent project, not an official OpenAI, Apple, or ByteDance product.
