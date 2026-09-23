# Optional owner-authorized Doubao TTS

During first-time setup, ask the owner to choose system speech or this external,
potentially billed provider. Wait for their choice; do not choose automatically.
Synthesized report and answer text is sent to the chosen provider; caller audio,
source-task history and credentials are not included in TTS requests.

Existing owners keep their saved profile unchanged. New owners explicitly choose
the model and voice; neither is copied from the maintainer. A model/voice mismatch
stops with the provider error, without automatic retries or substitutions.

## Guided first-time choice

Present these steps to the user in their language, one decision at a time:

1. Open the [Volcengine console](https://console.volcengine.com/ark/region:cn-beijing/overview)
   and let the user sign in or register themselves. Do not claim account access
   or service activation from a public-page check.
2. Open [TTS model activation](https://console.volcengine.com/ark/region:cn-beijing/openManagement?advancedActiveKey=model&tab=TTS).
   Recommend **Doubao speech synthesis 2.0** (`seed-tts-2.0`) for catalog
   voices. For users who already have a voice they own or are authorized to
   use, offer **Doubao voice cloning 2.0** (`seed-icl-2.0`); cloning and
   acquiring a voice are separate services, not supplied by this skill.
   Let the user choose and review console pricing before activation.
   These are the two resources documented for the current
   [bidirectional WebSocket endpoint](https://www.volcengine.com/docs/6561/2532486?lang=zh).
   Do not recommend 1.0, concurrency products, dialogue models or clone
   expressive parameters as drop-in choices for this adapter. Recheck official
   compatibility if the user requests another model; do not enable or bill it
   before support is established.
3. Open the [voice library](https://console.volcengine.com/speech/new/voices?projectName=default).
   Ask the user to filter for the chosen model, listen, then copy the exact
   voice ID. For standard 2.0, suggest these starting points from the
   [official catalog](https://docs.volcengine.com/docs/6561/1257544?lang=zh):
   **甜美小源 2.0** (`zh_female_tianmeixiaoyuan_uranus_bigtts`),
   **Vivi 2.0** (`zh_female_vv_uranus_bigtts`),
   **云舟 2.0** (`zh_male_m191_uranus_bigtts`).
   These are audition suggestions, not an automatic selection or a quality
   guarantee. For voice cloning, use only the owner's authorized clone ID,
   not a standard catalog ID. Never infer an ID from a display name.
4. Open [API Key management](https://console.volcengine.com/speech/new/setting/apikeys?projectName=default).
   Ask the user to create an API Key for the speech service in their chosen
   project. Console labels and eligibility can change; follow the visible
   page rather than inventing a button name. Keep the key out of chat,
   screenshots, command arguments and public files. Enter it only through
   the private hidden-input terminal below.
5. Confirm the selected resource, exact voice ID, possible synthesis charges,
   and text disclosure. Save that choice privately, then ask permission for
   a short no-dial synthesis sample and required notice preparation. An API
   connection does not establish intelligibility: let the user hear the sample
   before activating the renderer. Do not call until separately authorized.

The configurable profile is versioned. Legacy unversioned 2.0/甜美小源 settings
keep their strict identity checks and old cache keys. New configurations support
the two resources above; profile validation is not proof of account entitlement
or model/voice compatibility, which must also pass the provider's request.

## Configuration and activation

1. Follow the ordinary idle transactional runtime update in `operations.md` if
   these commands are not yet installed. Preserve the previous runtime and all
   selected native audio. Installing support does not select the new renderer.
2. Run `python3 scripts/call_the_boss.py configure-doubao` in a private interactive
   terminal. It requires a supported resource ID and exact voice ID, displays
   the non-secret selection, and requires `YES` before hidden API-key input.
   The profile is
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
  use the same owner-selected voice. Never splice a previous native “老板” or receipt
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
