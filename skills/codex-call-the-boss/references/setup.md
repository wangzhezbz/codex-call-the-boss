# First-time setup

Read this file only for initial setup, repair, or migration to another Mac.

Do not assume that a first-time user has the maintainer's configuration or any
installed runtime. Check prerequisites before choosing the installation path.
For the public package, configure the synchronous Stop route described in
section 5.1 after approval. Legacy relay instructions are mode-specific, not
additional first-time setup steps. Preserve an existing mode unless migration
is authorized.

## 1. Confirm the supported path

The no-extra-service-cost path requires all of the following:

- A Mac with `Phone.app` and the Codex desktop app logged in with a ChatGPT account.
- An iPhone on the same Apple Account with “Calls on Other Devices” enabled and an ordinary SIM able to place calls.
- The receiving phone must be a different reachable number; a number cannot call itself.
- `BlackHole 2ch` and `BlackHole 16ch` installed. In Phone.app, use 2ch as speaker and 16ch as microphone.
- Accessibility permission for the terminal/Codex host that runs the bridge.
- The receiving number must already have an exact row in Phone.app's recent calls with one exposed named “呼叫”/“Call” action. This implementation invokes that exact action once and confirms only when needed. On a new Mac, explain this constraint and ask the user to create the recent-call entry manually; configuring a new number alone does not populate it. Never invent a Finder or `.xpc` workaround, a coordinate click, or a generic row `AXPress` fallback.

Do not silently switch to Plivo or an OpenAI API-key path. Those change the user’s cost boundary.

## 2. Inspect before changing anything

Run:

```bash
python3 scripts/call_the_boss.py plan
```

Explain missing prerequisites. Installing BlackHole, changing Phone.app routing, signing into an Apple Account, or enabling system permissions requires the user’s participation. A Core Audio driver install may require a restart; do not claim it is usable until both BlackHole devices appear.

## 3. Install the bundled runtime

After the user approves local installation and Python dependency download, run:

```bash
python3 scripts/call_the_boss.py install --yes
```

Python 3.11 or newer is required. The installer first checks the bundle and interpreter, then creates a candidate in a permanent private version directory, installs pinned packages, runs dependency checks and the bundled tests, and finally switches the stable `runtime` link. It preserves the old runtime and failed candidates; no recursive cleanup is performed. It does not configure a number, subscribe a session, or place a call. Updates require a stopped idle daemon and no pending/unconfirmed call; follow [operations.md](operations.md). A versioned virtualenv is never relocated after creation.

## 4. Configure the iPhone line privately

Before configuring speech, ask: “使用系统语音，还是豆包语音？” Explain
the additional provider cost and synthesis-text disclosure for Doubao. Wait for
the user's answer. System speech uses their installed macOS voice inventory;
Doubao requires the guided model, voice-ID and private API-key steps in
[doubao.md](doubao.md). Do not silently choose system speech because it is
the runtime's technical default. Existing configured users retain their choice.
The optional native Realtime details below are not an automatic third choice
or a fallback from the user's selected provider.

Ask the user directly: “请把用于接收汇报电话的手机号码发给我，不能与 iPhone 拨出电话的号码相同。” They may provide it in the current private conversation. Do not echo it in summaries or logs, copy it into public documents, or put it in command arguments. Supply it through the following command's private interactive input; the user may also type it there themselves:

```bash
python3 scripts/call_the_boss.py configure-iphone
```

The number is stored only in `~/.codex-phone/config.json` with mode `600`.

When the owner chooses system speech, conversation uses their selected installed macOS voice, while recognition and answers use the current Codex login. This uses the existing account allowance, not a new API-key bill. Native Codex Realtime output is optional: list only voices the live phone v3 transport accepts and verify without dialing before switching:

```bash
python3 scripts/call_the_boss.py list-realtime-voices
python3 scripts/call_the_boss.py voice-self-test --voice "cove" --output "/absolute/path/realtime-check.wav"
python3 scripts/call_the_boss.py set-realtime-voice --voice "cove"
python3 scripts/call_the_boss.py prepare-voice
```

Run `set-realtime-voice` only after the self-test passes and the saved audio is intelligible. The test must observe an assistant `turn.done`, remove generation-time zero PCM, verify a real 48kHz non-silent waveform, and reject audio that is implausibly short for the completed Chinese transcript. Non-silent garble is still a failure. Reject a voice listed only under v2 because the telephone input path uses v3. Keep the selected local renderer when this gate fails; Codex still supplies recognition and answers.

The default local mode uses the selected macOS voice throughout. Explicit `set-realtime-voice` selects `realtime-unified`: opening, receipts, failure notices and conversation all use the same native voice. Run `prepare-voice` explicitly on first use, voice change, or a notice-library update. It prepares missing clips one at a time, prints progress, and gives each clip a bounded deadline. Validated clips remain private under `~/.codex-phone/native-speech`; integrity and voice are checked on reuse. Ordinary `stage-report` verifies this library and generates only its dedicated opening. A queued staged call reads cached audio only. Missing clips stop before dialing, not after pickup; no automatic system-voice substitution or hidden full-library generation is allowed.

Native mode additionally requires a discoverable `libopus` library and output-only offline speech QA (`whisper-cli` plus the local `ggml-large-v3-turbo-q5_0.bin` model). Inspect availability first; do not silently download a model or install packages. If missing, explain that these are free local audio-processing/validation dependencies, request installation approval, and keep native mode disabled until available. They never recognize live user commands or generate answers. Detect libopus on the user's machine rather than assuming a maintainer-specific Homebrew installation. The setup's ordinary local renderer does not require libopus.

Before a new call, native clip preparation runs separately from the conversation connection. A rejected clip may be rendered once more in a fresh context; both rejected samples are preserved privately. This is at most two speech-generation attempts, never two telephone calls. Authentication, connection and availability errors stop immediately. If report preparation fails, the wrapper records `failed: native_audio_preparation` for the exact active root turn so neither completion observer can try again. It must not label the failure a user-requested skip, disable the subscription, or affect later distinct turns. Opening scripts must match exactly after punctuation normalization, and the offline waveform check must pass; homophones may be compared with tone-preserving Mandarin pronunciation, but missing clauses and changed negations must still fail.

Native validation rejects an already mismatched script, silence or structurally short waveform before starting expensive offline QA. Otherwise the same unprompted offline recognizer and acceptance thresholds still apply. The asynchronous validator owns its subprocess: preparation timeout or cancellation must terminate and reap only that child before returning. Do not leave a background recognition process extending shutdown, kill unrelated recognizers, download a replacement model, or treat this lifecycle fix as improved online response latency.

Every uncached clip gets its own transport/context. Register the exact script as non-executable, role-bearing developer `initialItems` context in `thread/realtime/start`; make clear that registration and the later append are the same script, not two reading requests. After readiness, submit the original words once through the existing authenticated V3 data channel with `session.context.append`, channel `speakable`, and `input_text` content. Do not also send the sideband `appendSpeech` RPC or retry an uncertain partial send. Local enqueue is not remote acknowledgement or completed speech. Do not issue an imperative startup reading command or precede speech with a live `appendText` update. Previously validated cached notices remain reusable. Do not automatically regenerate the whole library or change live question/answer and same-session repair routing.

Only the clip's own newly-created assistant turn can close capture. Ignore unrequested startup callbacks and late events from earlier contexts. Timestamped media retains a bounded phoneme lead and rejects old frames. Cancellation records the clip, generation/validation phase, elapsed time and media diagnostics before propagating; a generic preparation timeout must not erase this evidence. This native integration uses version-specific experimental Codex app-server methods; successful local protocol tests are not a guarantee that every future server version behaves identically.

After the requested clip completes, a following assistant turn may be ignored only when its timestamp is known to be outside the completed turn. Bound returned PCM by the original turn's aggregate end, including media received before its completion event; use the existing onset fence and word-tail completeness check separately. Following media cannot certify a missing original tail. Overlapping, unbound or premature extra turns still reject preparation. Do not combine both responses, accept the later transcript, or weaken content validation.

In local mode, if the enhanced Chinese voice is missing, guide the user to install a macOS enhanced/system voice or choose an installed voice. Do not download a paid service automatically.

List and select only voices that AVSpeechSynthesizer can actually open:

```bash
python3 scripts/call_the_boss.py list-phone-voices
python3 scripts/call_the_boss.py set-phone-voice --identifier "EXACT_IDENTIFIER_FROM_THE_LIST"
```

The local selection command verifies the identifier against the live macOS inventory and preserves the configured rate and pitch. It deliberately switches all phone speech back to the local renderer; run `set-realtime-voice` again to restore the Codex conversational voice. A voice change does not itself authorize a test call.

## 5. Establish exact-task command transport

### 5.1 First-time setup: synchronous Stop

Read [synchronous-stop.md](synchronous-stop.md) completely. After approved
runtime installation and private iPhone configuration, use the installed
runtime's own interpreter to select `synchronous_stop` and install its hook,
following that reference's idle-line, stopped-daemon and trust checks.
Transport selection does not subscribe a task or authorize a call.

This route needs no fixed relay identity and no `start-relay` process.
Check the real host's hook support; if it is unavailable, stop and explain the
missing requirement rather than switching to a private entry point. For
subscription, report staging, tests and command receipt, that reference takes
precedence over the legacy descriptions in sections 5.2 and 6 and over
legacy watcher acceptance checks. Shared voice and audio checks still apply.

If the user requests configuration without an immediate call, do not dial.
If they also explicitly requested subscription, finish setup with the
one-turn `skip-call` exemption; future completed turns remain enabled.

### 5.2 Existing legacy desktop-relay installations only

The following relay-identity and startup instructions are not required for
synchronous Stop. Do not create a relay task during first-time setup of the
public package. Use this subsection only for an existing, explicitly retained
legacy desktop-relay installation.

The phone conversation uses an ephemeral read-only fork for questions. An accepted action request must return to the exact persisted source task through Codex desktop's `send_message_to_thread` tool. Do not use `codex exec resume`: a desktop-owned task already has an active writer, so the CLI can acknowledge the phone request yet fail with a thread-store conflict.

If Codex's paginated-history projection itself is broken, the narrowly scoped recovery described in [operations.md](operations.md) uses a verified, bounded read-only view of this same source. It neither creates a replacement execution task nor changes the source's model or history. Generic fork failures must not fall back to a blank task.

This transport needs one fixed, persisted Codex task as its caller identity. If the config does not already contain `relay_caller_thread_id`, ask for permission to create or fork one dedicated relay task, archive it, and register its ID:

```bash
python3 scripts/call_the_boss.py set-relay-identity --thread-id "RELAY_THREAD_ID"
```

The relay task is transport identity only. It never answers the caller and never executes the requested work. It must differ from every subscribed source task.

Start the restricted desktop relay directly inside a live Codex local command session and keep that command session running:

```bash
exec python3 scripts/call_the_boss.py start-relay
```

The relay exposes only health, task-status reads, bounded recent user/final-answer context, and `send_message_to_thread` on a mode-`600` Unix socket. Do not start it from the background phone daemon or a LaunchAgent: the Codex desktop app rejects a detached child that tries to acquire the app-tools pipe. If Codex or the managed relay process restarts, launch the relay again before staging a report or dialing. A new relay verifies fresh desktop read/send capability before listening and announcing readiness. A second start verifies the existing listener's correlated health response, not just a successful socket connection; an unhealthy or unrecognized live listener is preserved and startup fails, requiring inspection before an idle restart. Health continues to probe the desktop afresh. This remains a desktop-session prerequisite, not an unattended machine-reboot service.

The launcher must replace its managed process with Node using `exec`, preserving the PID recognized by the desktop. A subprocess-supervisor experiment passed fake-pipe tests but was rejected by the real desktop; it is not an accepted recovery mechanism. Do not detach or proxy around that ownership check. Uncaught exceptions now leave a content-free lifecycle record while retaining Node's normal fatal exit. A process killed without a record still has an unknown cause; restart it through a new managed command after checking call state, without resending any uncertain command.

Use a managed non-interactive command session for the relay; a terminal tab is not required. Its private `app-tools-relay-lifecycle.jsonl` records starts, pipe disconnects, signals and normal exits without task contents or numbers. A missing old log is not evidence of why the process exited; do not claim that merely restarting it fixes every future lifecycle issue.

The phone bridge assembles complete user turns; v3 `input_transcript.added` events are word fragments. It sends original words once, then checks the target turn. A durable journal distinguishes accepted, verified, and uncertain delivery. “Accepted by the app” is not proof the task has executed; inspect the resulting target action for acceptance. Dialogue archives live under `~/.codex-phone/conversations/<source-thread>/`, and quoted dialogue travels with subsequent commands.

The desktop reader may return recent turn IDs with empty message items. When available, the bridge therefore prefers bounded local rollout records whose session header and item source IDs both match the exact source task. Only original user messages and final assistant answers are included; reasoning, intermediate commentary, tool output, other tasks, and binary attachments are excluded. If those records are unavailable, retain the bounded desktop context without claiming it is complete.

Live conversation uses automatic backing response handoffs. A `delegation.created` event is not an action classifier. A warmed read-only Codex context classifies every complete nontrivial utterance while audio generation proceeds in parallel; playback waits for that decision so indirect approvals cannot leak a premature acknowledgement. Each delivered command carries a unique correlation marker. A new turn or changed timestamp alone proves nothing about that command: only its exact marker in an exact-source user record or validated fixed-relay phone envelope, together with target state, can justify a processing receipt. Otherwise say delivery was accepted but execution is unconfirmed. The source model and effort are unchanged.

Keep that classifier's capabilities minimal using per-thread `thread/start` overrides only: no selected capability roots or environments, a one-token skill-catalog budget, disabled apps/plugins/subagents/web search, plus the existing read-only policy and explicit classifier instructions. It classifies supplied original words and quoted phone history, not external data. These overrides must never be written to global config or applied to the source task/live question-answer context. Do not change its model, effort or structured decision gate for speed. Record numeric input/cached/output/reasoning token counts only from the exact classification turn, never raw model reasoning or unrelated turn metrics. Lower input counts alone do not establish a latency or phone-experience pass.

Empty capability roots do not prevent user-configured MCP services from starting. Read the effective config for the classifier cwd and disable those servers only in this temporary classifier's `thread/start.config.mcp_servers` table, preserving their names as nested keys. Do not guess quoted dotted override keys, change global files, or remove source-task/live-answer tools. A failed config read must stop classifier preparation. Record the excluded server count and the first user-item processing time so MCP startup delay remains distinguishable from model response time.

Unsent actions wait while newer speech is unresolved, allowing cancellation to be classified before dispatch even when the classifier serializes requests. Preserve original action order, one-time delivery journals and uncertainty guards. A cancellation after the external-send boundary is not an undo. At hangup, drain a confirmed final utterance; retain an incomplete fragment as incomplete, never as an executable command. Bounded source user/final records and true delivery receipts refresh the temporary voice context without initiating a spoken answer or treating quoted history as a new task.

V3 can keep one server user turn open across separate utterances. The bridge segments timestamped input fragments and speech boundaries, cancels outdated read-only backing queries only in the temporary voice context, and gives a bounded timeout response instead of waiting indefinitely. The short opening ends the greeting phase; later greetings must be answered normally. Playback receipts distinguish queued, playing, cancelled, partial, and device-output-complete. A device callback is not proof of what a receiving phone heard.

Later aggregate deltas update the pending utterance's original words and reset its settle clock just like word fragments. Ignore tails tied to an older completed acoustic utterance. A server completion may use the fast settle path only when its verified alias matches the pending utterance, or its media end covers the current tail and completed text ends with that same tail; preserve the narrowly scoped first-utterance exact-text fallback when no alias was emitted. An old completion is not permission to close a newer utterance. Build classifier and command-history context only before the exact caller identity. Earlier assistant text is eligible only with nonzero, complete device output; omit unplayed, queued and partial claims while retaining their raw diagnostic records. Freeze that history before asynchronous delivery reads so subsequent speech cannot become prior context; a legacy input with ambiguous repeated words must not guess its boundary. Device output still does not prove human hearing. These checks do not shorten the caller's ASR settle window.

The ordinary ASR grace starts after the later of the last recognized fragment and actual local caller voice end. An old prefix cannot exhaust that grace while the caller is still saying its final clause. On the observed-PCM path, neither a word timestamp gap nor a re-armed VAD burst may split a still-pending utterance: a short pause can precede a condition or cancellation. Settling or an explicit server boundary completes the input; fresh acoustic evidence alone only validates recognition. Keep the completed-input silence guard and genuinely later cancellation boundaries; do not reopen a settled command or substitute a local recognizer. Verify the same saved caller waveform and event timing before a new online sample, preserving the exact server-recognized words rather than inventing missing text.

For high media loss on a machine already using a loopback SOCKS proxy, compare no-dial samples via that proxy. `python3 scripts/call_the_boss.py set-media-route --route system-socks` selects it for telephone media only; `--route direct` restores direct media. No global settings or proxy software are changed. The per-connection adapter requires pinned aioice 0.10.2, retains encrypted WebRTC payloads, refuses unknown remote proxy endpoints, and does not silently fall back to direct when the selected proxy is missing. New users without an existing proxy retain direct media and must not be told to install one without consent.

Realtime quota errors stop preparation immediately and are distinct from network failure. Do not infer their reset time from ordinary Codex limits, spend a reset, switch to API-key billing, or retry repeatedly. Preserve the quota error under the current failed root turn and leave later session subscriptions enabled. Resume live acceptance only when the service becomes available; test results and package validation alone cannot certify a real phone conversation.

A delegated query that has not produced audio after four seconds gets at most one prevalidated same-voice waiting notice. That notice is feedback, not a successful answer. Preserve all caller-end and first-audio timing events so a follow-up cannot hide an earlier long silent wait. An empty or slow web result remains a failed/slow lookup even if the voice transport stays connected.

Query filler requires a confirmed question intent; unresolved input may still be an action. As soon as classification yields an action, cancel its query timers before waiting for desktop delivery. After delivery, play one fixed receipt only: the requested started-and-callback wording for a correlated new/steered target turn, or the shorter delivery-only wording when processing remains unconfirmed. Never add model acknowledgements, suggestions or invitations. Keep ordinary follow-up questions responsive.

The delivery-only branch now explicitly says “指令已经送达，执行状态暂未确认。” Prepare this notice before upgrading the runtime. Retained “请您耐心等待，执行完成后会电话通知您。” excerpts are no longer automatic substitutes for a complete selected recording. Keep the exact literal generation gate and archive whichever words actually supplied the audio.

Opening clips and live conversation are separate native contexts even when both select the same voice. The approved opening's exact speech-style block is shared with live speech and its backing instructions. Preserve the validated literal prompt/cache when only live guidance changes; regenerate only changed receipt scripts before deploying. Do not change sample rate, pitch processing, model or voice to disguise a style difference. Matching parameters and passing transcript checks do not establish perceptual voice consistency; retain receiving-phone acceptance.

If classification is still finishing at the waiting-notice boundary, allow at most half a second for its real reply/receipt before queuing filler. If it remains unresolved, wait for that input's published decision within the original absolute deadline, including when its timer predates the classifier task. A later confirmed question gets at most one notice only if no answer is already queued or sufficiently buffered. Recheck the current input, interruption state, buffered output and deadline after acquiring the speech lock and loading the cached notice; stale feedback must not enter the output queue. Notice preparation shares the same deadline. Neither waiting nor expiry may cancel the classifier or restart the question timeout; actions still get only their factual receipt.

Each question owns its own response deadline; unrelated opening/receipt audio cannot satisfy it or suppress its timeout. Timeout notices are included in the conversation archive. A native answer that has started but then supplies no voiced audio for the bounded stall interval gets one same-voice failure notice, with its stale tail suppressed. Do not declare a stall while already-buffered voice is still being consumed. A terminal voice failure ends that question's waiting timers. Skip the waiting filler when native audio is already buffered while classification finishes. The minimum duration check scales with text; complete short replies such as “在的” are not replaced just because they are under one second.

An entirely unplayed incomplete answer can be repaired once by appending its exact words on the existing native connection. No new connection or different voice is created. Keep the original response deadline, check complete repaired text before playback, and suppress repair audio superseded by caller speech. A second failure becomes a single same-voice failure notice, never an infinite generation loop or a redial.

Aggregate assistant-turn timestamps can trail word-level timestamps; word timestamps can in turn trail the phoneme onset by over a second after a long idle. Preserve a bounded connected voiced onset, requiring a preceding quiet region and excluding a separate old tail. The media fence must handle both media-before-data and data-before-media; otherwise the beginning can be lost even when every RTP packet arrived. Require a fresh physical acoustic utterance before accepting a separated new transcript, including the aggregate-only event path. Silence hallucinations cannot interrupt the opening or dispatch tasks.

A short introductory clause can contain a natural pause before the rest of the answer. Preserve that earlier clause only when the previous assistant completion is known to precede the entire bounded lookback window by a safety margin (or in a fresh single-clip context). If previous-turn ownership is unknown or nearby, retain strict old-tail exclusion. Validate with the same saved waveform; a newly generated better-sounding sample is not proof the original clipping bug was repaired.

A native model backchannel is not an input-end event. Keep the caller's pending words intact when an assistant turn starts or finishes, and defer its audio until the full caller utterance has been classified. Already-authorized prepared delivery receipts and opening reports are not speculative model acknowledgements; later classification must not suppress their actual playback/archive records.

When the first acoustic greeting has recognized text, honor its completed structured greeting decision, including short interjections outside the fast greeting matcher. Resolve server aliases to the same caller identity in hold, release and suppression paths; a late initial reply must not escape the gate or be assigned to a later caller turn. When there is no recognized text, bind an unowned opening reply to that acoustic sequence at creation. Wait for its complete text before suppressing a pure greeting: a greeting prefix may precede a real answer. Reception acknowledgements, listening backchannels and a turn handover are still pure greetings; require the whole reply to match, never a substring. A later acoustic utterance must not be treated as the initial greeting. Do not invent a caller transcript or relax short-audio checks. Do not add an extra online assistant-output classifier to this path: the tested prototype often exhausted four seconds and delayed playback without reliable coverage. Completed greeting/farewell classification must also release or suppress responses held while input was pending.

Clarification prompts belong to their originating input and speech generation. Drop them when newer input supersedes them; do not resume a cancelled clarification as a delivery receipt. Real delivery/error receipts retain their original command identity when resumed. A stale prompt, a classification timeout and a failed external send are distinct events, not interchangeable failure announcements.

Cancellation before dispatch stops the unsent action. Once sent, forward the caller's cancellation as a new correlated source-task request and explicitly keep undo status unconfirmed. Do not silently drop a cancellation or resend the original action.

Classifier preflight and the media connection are both mandatory. The classifier owns a minimal app-server process whose application/plugin catalog and shell-snapshot exclusions apply from process launch; ordinary answers and the source task keep their original capabilities. Verify its existing ChatGPT login and complete its warmup before starting the heavier answer fork/media connection. The 25-second classifier deadline includes its process startup, and the original 45-second total and 30-second media limits remain unchanged. A failure or cancellation stops this attempt without dialing or restarting a gate. The private job records `phone_intent_warmup`, `preflight_order` and per-utterance `classification_stages`: context readiness, request acceptance, first output, completion, failure phase and content-free service error code. A missing turn ID must fail before replaying buffered notifications, and only exactly correlated events may finish a classification. A closed service wakes the waiter immediately; an uncertain interrupted context is discarded for the next distinct input, never automatically retried. A passed warmup is specific to this call and is not an authorization decision for later caller words. Drain completed caller commands before closing only the private classifier process.

Output underruns count starvation of an unsealed stream, not ordinary silence after a sealed/completed clip or cancellation. Keep normal idle in `output_idle_silence_ms`, separately from `inserted_silence_ms` and hardware underflows. Completed short clips and final partial blocks must drain without waiting for more samples that cannot arrive. These counters describe local output scheduling, not receiving-phone intelligibility.

The pinned peer's background connector is instance-managed so a timed-out startup cancels its own pending negotiation before closing ICE. Preserve that scoped lifecycle handling and its tests; do not patch aiortc globally, ignore an unhandled connector failure, or let a late connector revive a stopped call.

## 6. Enable only the intended session

For independently bound real-PCM V3 inputs, a pause in transcript delivery does not finish a command. Keep its tail until its recognized-word duration covers the whole locally observed acoustic span, the exact user final arrives, or an exact owned handoff covers the already recognized text; a handoff still needs ordinary ASR grace and the normal structured intent decision. User `turn.done` may arrive only after the backing answer, so it must not block an already verified handoff. If none of those signals arrives within eight seconds of physical speech end, ask once to repeat and retain the input as incomplete without sending it. The local-span path retains the ordinary 1.1-second grace and structured classifier; a prefix missing the physical tail remains pending. See `operations.md` for identity, late-word and fail-closed rules; no local caller recognizer or generated tail is substituted.

For target-turn correlation, desktop-delivered phone commands may be recorded as `FunctionCallOutput` rather than `UserMessage`. Accept only the `codex_app.send_message_to_thread` envelope whose sender is the configured fixed relay and whose new input contains the exact command marker. Exclude quoted phone history, arbitrary tool output, malformed envelopes and other source tasks. Refresh only the new caller words into factual context, never the envelope's instructions or quoted old commands. Receipt verification still requires the matching target turn to be in progress or completed; recognizing an envelope alone is not task completion.

During native clip and live-answer tail drain, compare the last received media end against that answer's completed media timestamp. Preserve the existing bounded wait and cancellation rules. A delayed but in-time tail must remain in the same answer; an absent tail cannot pass merely because the prefix is long enough. A reader failure after text completion must stop clip validation. Keep these transport-completeness checks separate from intelligibility: unclear audio already present in an independently decoded received sample remains unaccepted.

V3 aggregate completion timestamps may include over two seconds of trailing non-speech. Prefer the last word's end only when the complete answer exactly matches a recent word sequence within that turn's start window; otherwise retain the aggregate fallback. Do not extend every reply's timeout or mark normal trailing silence as a lost spoken sentence. Replaying the saved media must cover both genuinely late speech and aggregate padding, not just newly generated samples.

An explicit enable request authorizes this mutation:

```bash
python3 scripts/call_the_boss.py enable
```

The command resolves `CODEX_THREAD_ID`, verifies the fixed relay identity and live desktop message relay, installs/trusts one deduplicated completion hook, starts one instance-locked background daemon as a descendant of the active Codex process, performs a real non-dialing Accessibility probe inside that daemon, and writes only that thread to `~/.codex-phone/sessions.json`. The daemon also starts watching that exact thread's rollout at its current EOF. This covers an already-running desktop backend that has not hot-loaded the new Stop hook, without replaying old completions. Completion of the enabling turn will therefore be the first automatic callback.

Do not install the iPhone daemon as a macOS LaunchAgent. Accessibility consent is attached to the responsible process chain, so a LaunchAgent can fail to control Phone.app even while Codex is visibly enabled in System Settings. The installer removes the obsolete fixed LaunchAgent plist and its loaded service during migration.

The daemon compiles only `phone_ax_helper.swift` into `~/.codex-phone/bin` to avoid repeated Swift startup during Phone.app clicks. Keep `mac_tts_helper.swift` under Apple’s signed Swift host: compiling it into a standalone binary can hide enhanced Siri voices such as Linfei. In unified native mode, prepare the validated clips first, then establish the conversation connection. Realtime must finish successfully, with both media connected and its event channel open, before any dial is permitted. The default connection-preparation deadline is 30 seconds (`phone_realtime_start_timeout_seconds`, capped at 45). On timeout, cancel preparation and record `phone_startup_failure` without dialing. Do not revive that job if a late response arrives. A later distinct source completion remains eligible for its own single attempt.

Recheck current connection health before the dialer begins and again immediately before its one confirmation click. There is no longer a ten-second handshake deadline after pickup. Record `phone_latency.realtime_ready_at`, `realtime_ready_before_dial`, and `realtime_start_stages` to distinguish offer creation, service start, connection negotiation and event-channel readiness. These transport checks do not certify audible speech quality; keep the separate audio and receiving-phone acceptance checks.

Start the CallServices observation window and persist its line guard immediately before pressing the matching recent-call row. Phone.app can create this call's `Sending` record before its separate confirmation button is clicked; starting observation at confirmation loses the UUID and can leave an answered call silent. Watch the bound call concurrently with UI actions from this first possible click. A confirmation timeout/error stops further UI actions, not the prepared voice bridge: keep observing the same call within the original connection deadline. The last pre-confirmation health check remains mandatory. An observation start is not evidence that confirmation or cellular connection occurred. Do not repair this by accepting an unbound `Active`, guessing a lookback interval, or treating an AX timer as proof. Replay a confirmation error followed by later `Active`, greeting capture, opening device output, and `Disconnected`; include unrelated and older calls as negative cases. Observation start and confirmation request have separate latency fields.

During pickup detection, the bound CallServices state does not wait for accessibility or process-tree scans. On `Active`, cancel pending UI steps and start caller input/greeting immediately. On `Disconnected`, stop the attempt. If state stays unknown or Sending, preserve the original bounded wait and unconfirmed-line protection; do not repeat the click, extend the deadline or let a late helper result resume a cancelled UI step. A stale failure banner cannot terminate a newer pending call, and an AX timer cannot prove pickup.

Realtime conversation audio ignores v3 media-timestamp gaps, drops generation-time digital zero, waits for a completed sentence plus a safe PCM lead, and then streams compact audio. A short remaining turn is released whole at `turn.done`. Unified mode validates the voice before dialing and checks streamed duration against completed text; it does not hold each full reply for a blocking ASR pass. A blank/truncated turn or disconnected service uses its prepared same-voice notice, not macOS speech. This is not a guarantee against every acoustic defect; verify long answers and the receiving phone's sound.

Unified mode also uses the pinned per-receiver Opus FEC/PLC decoder. It conceals only one to five missing 20 ms packets when sequence and media duration agree; arbitrary timestamp jumps and large gaps are never filled. FEC uses following-packet redundancy when present, otherwise libopus concealment is used. This does not recreate the original lost signal or prove an audible improvement by itself. Keep the no-extra-discard reorder buffer, packet replay tests, and real-call acceptance. Never patch aiortc globally or upgrade its pinned version without updating this receiver adapter.

Synthetic-output checks retain raw character alignment and optionally compare tone-preserving Mandarin syllables for ASR homophones. On a mismatched output-QA pair, the existing Apple helper may normalize Traditional/Simplified orthography before repeating the same content checks; retain the original transcript and raw score alongside a separate normalized comparison score. Do not apply this conversion to caller words, generated audio or the generator's exact-script gate. Numbers remain strict; changed negation and missing/different status must fail after conversion as well. Protected status includes bare 开始/完成/修复, so “没有完成” cannot become “没有修复”. Affirmative status labels such as 已修复/以修复 require the complete tone-identical phrase, all protected status phrases in the same order, and the existing whole-utterance phonetic/length thresholds. No score threshold is lowered. Unavailable/malformed helpers provide no presumed normalization or phonetic success. Missing sentences and unrelated speech still fail. This offline check is not the caller's recognizer or a local answering model. If offline QA is unavailable, stop for explicit listening/validation rather than automatically weakening the gate. Pure speech-rendering contexts use the supported `clientManagedHandoffs` setting so backing-agent responses are not automatically injected into the clip stream.

Immediately before a subscribed turn ends, `stage-report` verifies the live relay and daemon, then writes its dedicated copy to a private per-session sidecar. Hook and watcher atomically consume it with shared turn-ID deduplication. Nothing is embedded in the visible answer. For an explicit one-turn exemption, run `skip-call` as the final command without disabling future callbacks. A manual test already consumes the current turn's call; later staging must not erase that guard. If staging was omitted, a bounded read-only Codex summarization in the ephemeral phone fork generates separate copy; it never substitutes “看屏幕” or claims a missing summary is success. This fallback is slower, so stage normally.

The sidecar is now keyed by session **and active root turn**, resolved from that source rollout's lifecycle metadata. Bound directives have no fifteen-minute expiry, cannot spill into the next turn, and are consumed once. Legacy unbound directives retain their short compatibility expiry.

Save the completion-hook command through the installation's stable `runtime` link, never an individual version path. The versioned entry forwards a cached older command to the active version before processing its event; update verification must check this old-entry path too. The daemon-instance lock and original completion deduplication still apply. See the narrowly scoped legacy-entry migration in [operations.md](operations.md); do not clear a failed root or redial to verify an update.

Verify:

```bash
python3 scripts/call_the_boss.py status
python3 scripts/call_the_boss.py doctor
```

Do not place a test call unless the user separately asks for one.

Before that request, walk the user through the call: say “喂” after pickup,
listen to the report, then freely ask about results, explanations or options.
For a task to execute, teach the wording “下一步的任务是：XXXXX” with a concrete
example and expected result. Explain that discussion alone does not request
project changes. After a delivery receipt the caller may hang up; the next
completion is reported by another call. An unconfirmed execution receipt still
requires checking the original task. One call submits one action; corrections
or cancellation after submission go through the original task. Refer to the
Chinese walkthrough's call examples when useful, and explain in the user's
language. Do not treat the suggested phrase as a magic authorization token.

## 7. Disable

For “别再打了” or equivalent, disable the current session immediately:

```bash
python3 scripts/call_the_boss.py disable
```

The registry retains a disabled audit record. If no enabled sessions remain, the global call switch is paused; the Codex-descended daemon may remain running but idle.

## Acceptance checks

- An unregistered test thread completion creates no queue file.
- The registered source completion creates one queue file with its exact thread and turn IDs.
- A daemon started against a rollout containing old completed turns begins at EOF and never backfills them; one newly appended root completion queues once even if the Stop hook also runs.
- `doctor` reports a running Codex-child daemon, a passed Accessibility probe, one completion hook, and no loaded legacy LaunchAgent.
- A completed call record contains `phone_latency` stage data; use `prepare_total_ms` for local preparation and the dialer’s `system_dial_requested`/`system_call_active` values to distinguish Phone.app work from carrier ringing.
- A slow Realtime startup must not invoke the dialer until ready. A startup timeout/error or cancellation before the first possible call action must make zero physical calls. Loss of readiness before confirmation must forbid that confirmation click, but cannot be described as zero calls if the recent-row action already dispatched; continue observing the original possible call without redialing. Replay a success arriving after the old ten-second threshold, and confirm that a ready call is not rejected by the removed pickup-startup timer. A real post-readiness disconnect must still trigger the prepared failure notice and retain the call slot until actual hangup.
- A failed dial records bounded, sanitized CallServices/IDS facts in `phone_dial_failure`. `IDSSessionEndedReasonRemoteUnanswered` before any `Active` indicates an unanswered Mac/iPhone relay setup, not proof the receiving person ignored a ringing phone. Do not blame a setting without checking it, invent a carrier diagnosis, or redial automatically. Mac-side readiness cannot guarantee that the remote iPhone will accept continuity.
- `IDSSessionEndedReasonNoRemoteNetwork` with one Sending/Disconnected call and no Active is classified separately as remote relay network unavailable. It does not identify a particular iPhone setting, carrier fault, or audio defect by itself.
- A second completion during an open call remains queued for after hangup.
- The queued job contains a purpose-written `spoken_report` separate from the visible `report`.
- Pickup plus “喂” produces one complete sentence beginning with “老板”; local PCM detection releases it without waiting for ASR, and missed detection falls back quickly instead of waiting six seconds.
- The opening report is one short sentence; an action gets one fixed truthful receipt without query filler or extra commentary. Subsequent questions get normal, complete Codex answers rather than one- or two-word fragments.
- A no-dial `voice-self-test` rejects pure silence and implausibly compressed speech, observes a completed assistant turn, and produces compact 48kHz mono PCM. The saved waveform must also be intelligible before Realtime output is enabled.
- Unified native mode prepares the opening and every receipt/failure notice before any dial. A disconnected speech generator after pickup must still allow the cached failure notice; there must be no surprise system-voice fallback. Cached receipts play only after actual desktop acceptance, and the dialogue archive records what was played rather than a suppressed model acknowledgement.
- An explicit one-turn `skip-call` creates one done record, no queue/calling/failed job, and a second observer cannot queue the same completion. The exact session remains enabled afterward.
- One spoken action request appears once in the subscribed source task and starts or steers a real target turn. A final acknowledgement arriving just before hangup is reconciled and flushed before the call record closes.
- Replay a missing-information question, a real-time lookup, an explicit action, and an acknowledgement arriving before final transcription. The question must not dispatch work, a waiting notice must not pass answer validation, and the action must execute only once with all original words preserved.
- The next source completion causes one new call.
- A failed dial does not retry within the same job, but the next distinct completed root turn still queues exactly one new attempt.
- No `codex-phone-report`, `codex-phone-skip-call`, or other control marker appears in the visible answer.
- Restart/crash recovery marks an interrupted in-progress call as failed without redialing. A failed queue worker cannot leave a falsely healthy daemon running.
- Test a natural long answer, barge-in with late old deltas, and an idle interval longer than 90 seconds. Record actual caller-end-to-first-audio latency separately from generation/synthesis timing.
- A no-dial test cannot certify cellular call delivery or the receiving phone's final sound. Reserve “end-to-end verified” for one real answered call with an actually executed source task and a subsequent one-call completion report.
- Replay the exact same captured packets through old/new receive buffering. Confirm that received packets are not additionally discarded after a gap; count actual missing, reordered, late and duplicate packets separately. The source implementation pins aiortc and PyAV and changes only its own audio receiver, never installed third-party library files.
- Kill/fault a simulated voice reader and close the Realtime event channel while a fake phone remains active. The call should announce failure once, retain the line until real hangup, and never label a mere greeting or timeout as completed. After an unconfirmed close, restart must retain the private line guard and block another dial.
