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

## What to say on the call

1. **Answer and say hello**, then listen to the short completion report.
2. **Talk normally**: ask “What did you finish?”, “What still needs work?”, “Why did you change that?”, or “How do these two options compare?” You can also say “Repeat that” or “Explain it more simply.” Ask questions before deciding; discussion alone is not a request to change the project.
3. **When you want work executed, say:**

   > The next task is: XXXXX.

   For a Chinese-language call, use: **“下一步的任务是：XXXXX。”** For example: “The next task is: check every link on the homepage, fix broken links, and report which ones changed.” State the action, scope, and expected result.
4. **Listen for the receipt.** After confirmed delivery, you may hang up and wait for the next completion call. If execution is unconfirmed, check the original task; delivery does not mean work has started.

Each call submits one execution command. Make additions, changes or cancellations after submission in the original Codex task. You can also just ask questions and hang up without assigning work.

Before the first test call, Codex explains these steps and asks whether to dial.

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

On first use, Codex asks you to choose **system speech or Doubao speech**. Nothing is selected on your behalf.

- **System speech:** choose an installed Mac voice; no additional TTS-provider fee.
- **Doubao speech:** choose a model and voice through the steps below. Synthesis text is sent to Volcengine, and usage may be billed.
- Recognition and answers use your signed-in Codex account and its limits. Calls use your mobile plan; this path does not require an OpenAI API Key.

### Set up Doubao

You can also choose **3. Choose another speech model yourself**. Send Codex its name or ID for a compatibility check before activation.

1. Open the [Volcengine console](https://console.volcengine.com/ark/region:cn-beijing/overview) and sign in or register.
2. Open [TTS model activation](https://console.volcengine.com/ark/region:cn-beijing/openManagement?advancedActiveKey=model&tab=TTS). For catalog voices, start with **Doubao speech synthesis 2.0** (`seed-tts-2.0`). If you already have an authorized cloned voice, choose **Doubao voice cloning 2.0** (`seed-icl-2.0`). Review prices and choose yourself; cloning is not included in this skill.
3. Open the [voice library](https://console.volcengine.com/speech/new/voices?projectName=default), filter by your model, listen, and copy the exact voice ID. For standard 2.0, try 甜美小源 2.0, Vivi 2.0, or 云舟 2.0. You make the final choice.
4. Create your speech-service key in [API Key management](https://console.volcengine.com/speech/new/setting/apikeys?projectName=default). Enter it only in the private hidden-input terminal Codex opens—never in chat or the repository.
5. Confirm the model, voice and possible charges. Codex saves your choice, checks synthesis and offers a sample before activation. Testing does not place a call.

The adapter supports the two 2.0 resources above; other models need a compatibility check before activation. See [Doubao setup](skills/codex-call-the-boss/references/doubao.md) for voice IDs and details. Existing users keep their selected voice. No keys, phone numbers, logins or recordings are distributed.

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
