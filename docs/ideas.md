# Future ideas (backlog)

Ideas captured during discussions with Marco, ordered roughly by current interest.
None of these are scheduled in `STATUS.md` yet — promote one to `STATUS.md` → `Next`
when ready to design and build it.

## 1. Approve agent permissions directly from Telegram
- Today Telegram sends a delayed alert (`Notifier.notify_permission`) when an
  agent chat approval has been waiting >30 s, asking you to open `/chat`.
- Next step: add Telegram inline keyboard buttons (**Allow once** / **Deny**)
  handled via outbound long polling (no inbound webhook port required).
- Security invariants to preserve:
  - Strictly verify `chat_id == BATON_TELEGRAM_CHAT_ID` and ignore all other
    senders/updates.
  - Bind each callback payload to a short-lived, single-use random token scoped
    to one pending `permission_id` (no arbitrary command text or replay).
  - Never allow "always allow" from Telegram; allow-once or deny only.

## 2. Voice in `/chat` (dictation & optional read-aloud)
- Speech-to-text (STT) mic button in the `/chat` composer, either via the
  browser Web Speech API as a zero-dependency first step or a local Whisper
  server on the PC so audio never leaves the LAN.
- Optional text-to-speech (TTS) playback button on assistant messages.

## 3. Document uploads & persistent memory in `/chat`
- Allow attaching plain-text, Markdown, code files, and PDFs (extracted locally)
  in `/chat` alongside existing raster image attachments, with strict size and
  context-budget guards.
- Optional per-project or global lightweight notes/memory injected into the
  system prompt (explicit, user-editable, never hidden).
