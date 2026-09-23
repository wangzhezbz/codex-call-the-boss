# Optional owner-authorized Doubao TTS

Use only when the owner explicitly chooses this external, potentially billed
provider. Ordinary Mac/iPhone setup remains on the existing account/local path.
Synthesized report and answer text is sent to the chosen provider; caller audio,
source-task history and credentials are not included in TTS requests.

The supported profile is pinned: `Doubao-语音合成-2.0`, resource `seed-tts-2.0`,
voice `zh_female_tianmeixiaoyuan_uranus_bigtts` (甜美小源 2.0). Do not downgrade
the resource or silently substitute the older `_moon_bigtts` voice. A model or
voice mismatch stops with the provider error, without automatic retries.

## Configuration and activation

1. Follow the ordinary idle transactional runtime update in `operations.md` if
   these commands are not yet installed. Preserve the previous runtime and all
   selected native audio. Installing support does not select the new renderer.
2. Run `python3 scripts/call_the_boss.py configure-doubao` in a private interactive
   terminal. The owner enters their own key through hidden input. The profile is
   saved mode 600 under `~/.codex-phone/doubao-tts.json`, never in the Skill,
   environment, command arguments, shared source or share ZIP. Existing files
   are not overwritten. Do not ask for the key again when it is already stored.
3. Run `python3 scripts/call_the_boss.py prepare-doubao-voice`. This is an explicit
   bounded, potentially billed synthesis of missing required notices. It does
   not change the selected renderer or dial. Every complete waveform must pass
   existing output-only offline content QA. Inspect the saved audio and offer
   an actual playable sample; neither a handshake nor QA proves human hearing.
4. With the owner's voice choice and all checks complete, run
   `python3 scripts/call_the_boss.py set-doubao-voice --confirm`. It requires an
   idle, unguarded line/queue and an intact complete notice library. It changes
   only the renderer; it does not dial or subscribe another task. Run `doctor`.
5. For every enabled root completion, write the current dedicated status and
   use ordinary `stage-report`. It generates and validates this opening before
   queuing, then pickup detection releases its prepared PCM after “喂”. The
   current report must never be replaced with an obsolete cached report.

The old `_moon_bigtts` private connectivity-test profile may migrate only after
the owner explicitly approves the `_uranus_bigtts` ID:
`<runtime-python> <runtime>/doubao_tts.py select-approved-speaker --confirm`.
This keeps a private backup and preserves the exact key/model. It does not
select the production phone renderer.

## Playback contract

- Opening, ordinary replies, complete task receipt, and all failure notices
  use the same pinned voice. Never splice a previous native “老板” or receipt
  into a Doubao utterance. The former assets remain untouched for rollback.
- Codex still recognizes the caller, classifies the complete input, answers in
  the exact-source voice context, and sends actual actions back to that source.
  Doubao reads only finalized answer sentences; it is not the answering model.
- Suppress native Realtime PCM from phone output in this mode. Generate each
  allowed answer sentence once and retain complete returned PCM without pitch
  or speed processing. Check cancellation/expiry before and after synthesis.
  Barge-in cancels the owned in-flight synthesis and clears queued old speech.
- Required notices are cache-only during a call. A missing prepared asset
  stops before dialing. A live synthesis failure produces one prepared
  same-voice failure notice, not another voice, a retry loop or a new phone call.
- Play the full started receipt only after the existing exact-command target
  verification. Delivery without verified processing still gets the distinct
  delivery-only notice; never claim execution merely because TTS succeeded.

PCM, WAV, provider timing/usage, content QA and failed-attempt evidence are
stored privately under `~/.codex-phone/doubao-speech`. Cached audio is bound to
literal text, exact model/voice/format, QA revision and PCM hash. Cache-only
access makes no provider requests. Keys and this audio library are excluded
from reusable packages. No cleanup, spending limit, billing amount, human
identity verification or production SLA is implied.

TTS connectivity and local output tests do not fix upstream caller-ASR mistakes
or prove phone delivery. Keep receiving-phone hearing, real action execution,
long answers and interruption acceptance as separate evidence.
