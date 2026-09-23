# Third-party components

The root MIT license is the license selected by this repository's owner. It
does not replace the licenses or service terms of external components.

- Runtime dependencies include aiortc, aioice, PyAV, sounddevice and websockets.
  They are downloaded by the installer, not vendored as binary packages here.
  Consult each installed distribution's license metadata before redistribution.
- `doubao_protocols.py` originates from the vendor's bidirectional WebSocket
  speech-synthesis example supplied with the Doubao documentation. The surrounding
  adapter implements the selected provider protocol. Attribution and API reference:
  [Volcengine bidirectional TTS documentation](https://docs.volcengine.com/docs/6561/2532486?lang=zh).
  The supplied example had no standalone license header; no claim is made that
  this project's MIT license grants additional rights to that vendor example.
- BlackHole, whisper.cpp, its model files, Apple system components, the Codex
  application and optional speech services are separate prerequisites, not
  included downloads. Their own licenses, accounts and service terms apply.

No provider keys, account sessions, private voice recordings or model weights
are part of this repository. OpenAI, Codex, Apple, iPhone and Doubao names identify
interoperability targets; they do not imply affiliation or endorsement.
