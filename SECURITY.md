# Safety and private data

This is an experimental local bridge with real calling and task-execution side
effects. Phone input retains the source task's normal approval boundaries.
The person answering the phone is **not authenticated** by this implementation.
Do not use a shared/forwarded receiving number for unattended sensitive work.

Never attach credentials, private configuration, complete transcripts, recordings,
account files or the `.codex-phone` state directory to a public issue. A phone
number is also private data. The provided diagnostic export is allowlisted and
must still be reviewed before sharing; exporting locally does not authorize upload.

For non-sensitive bugs, include the OS and Codex versions, the failed stage
(preparation, dialing, pickup, audio, command delivery or execution), and a short
redacted description. Automated tests or a queued job are not proof of a real call.

Do not post an exploitable vulnerability or exposed secret publicly. Use the
repository's private vulnerability-reporting channel if the owner has enabled it;
otherwise ask the maintainer for a private contact without posting sensitive details.
No response-time commitment or independent security audit is claimed.

Recordings and command history remain local until the owner removes them. There
is no automatic deletion or retention-compliance guarantee. Never clear delivery
journals or line guards to force another call or repeat an uncertain command.
