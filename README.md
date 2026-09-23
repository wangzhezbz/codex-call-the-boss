<p align="center"><img src="docs/assets/hero-en.svg" alt="Call the Boss — The work is done. Your phone rings." width="100%"></p>

<p align="center"><strong>English</strong> · <a href="docs/README.zh-CN.md">简体中文</a> · <a href="docs/README.ru.md">Русский</a> · <a href="docs/README.ja.md">日本語</a> · <a href="docs/README.ko.md">한국어</a></p>

<p align="center"><a href="dist/codex-call-the-boss.skill.zip">Download the skill</a> · <a href="#get-started">Get started</a> · <a href="#current-limits">Current limits</a> · <a href="docs/ROADMAP.md">Roadmap / 路线图 / План / ロードマップ / 로드맵</a> · <a href="LICENSE">MIT</a></p>

# Call the Boss

Let Codex call you when the work is done. Get a brief update, ask questions, and give your next task by phone—all connected to the original Codex task.

**Experimental · macOS + iPhone · Explicit per-task opt-in**

| macOS + iPhone | Windows | Linux |
| :-- | :-- | :-- |
| Local skill; setup required | Not supported | Not supported |

Use the calling features already on your Mac and iPhone—no extra mobile app needed. Codex guides you through the initial setup.

## How it works

1. Codex finishes work in an enabled task and prepares a short spoken report.
2. Your Mac places one call using iPhone call relay. After pickup and your greeting, it plays that report.
3. Ask questions normally. Recognition and answers use your existing Codex login; the selected speech renderer supplies the voice.
4. Give one next task. Its original words return to the **same task**, where Codex executes within its normal permissions.
5. When that work finishes, the next completion can trigger one new call.

Work assigned by phone returns to your original Codex task. Open that task anytime to check progress and results.

## Get started

### 1. Prepare your devices

You need a Mac with Phone.app, the Codex desktop app signed in, and an iPhone with call relay enabled. You also need a receiving number different from the outgoing phone line.

Setup requires Python 3.11+, BlackHole 2ch/16ch, and Accessibility permissions. Codex checks these and guides you through anything missing. The receiving number must appear in Phone.app's recents with an available Call action. Keep your Mac and Codex running and awake during use.

### 2. Download and set up with Codex

Download the [skill ZIP](dist/codex-call-the-boss.skill.zip), extract it, give Codex access to the extracted `codex-call-the-boss` folder, and send:

> This is my first time using Call the Boss. Read SKILL.md in this folder, check my devices and environment, and guide me through installation, iPhone call relay, the receiving number, audio, and permissions. Do not assume I have any phone configuration. Ask before installing components, changing permissions, or enabling paid services. Once configured, enable phone reports only for the current task. Wait for my confirmation before placing a test call.

Follow Codex's setup prompts; you do not need to edit parameters in the code yourself. See the [setup guide](skills/codex-call-the-boss/references/setup.md) for details.

### 3. Make a test call

Once setup checks pass, ask Codex to call you once for a test. Answer and say hello. Check that you can hear the report, ask questions, and give a simple task. Return to the task window to check that your instruction arrived and was executed.

### Use it in another task later

After the initial installation, send this in each Codex task where you want phone reports:

> Use $codex-call-the-boss. Check the saved configuration and enable completion calls and phone commands only for this task. Guide me through anything missing without changing other tasks' settings.

Enable each task separately. Finish calls and wait for pending work before updating or reconfiguring.

## Voice and costs

- Recognition and answers use the signed-in Codex account and its limits; no OpenAI API key is required by this path.
- Calls use your existing mobile service. This is not a promise of free calls or unlimited account usage.
- System speech is the default. Optional Doubao TTS can incur separate charges and requires your own credentials and approval. Its integration pins **Doubao speech synthesis 2.0 / `seed-tts-2.0`**; see [Doubao setup](skills/codex-call-the-boss/references/doubao.md).
- No credentials, phone numbers, login sessions, voice caches, or call recordings are distributed.

## Current limits

- **Experimental; not intended for critical workflows that depend on phone notifications.** Try it on your own devices first.
- Keep your Mac and Codex running and awake. The phone-command window lasts up to 8 minutes by default, with one execution command per call. To cancel a submitted command, return to the original task.
- Delivery does not mean execution has started. If the call cannot confirm execution status, check progress in the original task.
- One call attempt per completion, with no automatic redial. Ask Codex to try again if you miss a call or it fails.
- Call quality depends on your network, devices, Codex version, and account limits. Documentation is available in five languages; test your preferred spoken language before relying on it.
- The receiving person is not authenticated. Do not use shared or forwarded numbers for unattended sensitive commands.
- Phone records remain in local private state until the owner removes them; no automatic retention policy is implied. Do not upload that state directory.
- Phone instructions use the tools and permissions of the original task. Actions requiring additional approval still need your confirmation.

## Develop and report problems

The reusable skill is under [`skills/codex-call-the-boss`](skills/codex-call-the-boss). Its runtime and tests are under `assets/runtime`. [Development, validation, and packaging](docs/DEVELOPMENT.md) describes the no-call checks. [Security guidance](SECURITY.md) explains what **not** to put in an issue.

Bug reports, suggestions, and translation improvements are welcome.

## License and attribution

Licensed under [MIT](LICENSE). External dependencies and vendor examples have their own terms; see [third-party notices](THIRD_PARTY_NOTICES.md). This is an independent project, not an official OpenAI, Apple, or ByteDance product.
