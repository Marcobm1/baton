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

## 4. Interactive task map inside each project — shipped (2026-11-27)
- Shipped as the **Map** tab on `/p/{id}?tab=map` (`baton/task_map.py`,
  `baton/static/task-map.{css,js}`, [docs/project-detail.md](project-detail.md)).

## 5. External Gemini (`agy`) CLI token accounting & runtime silent-failure counter
- Raised during the [2026-11-28 Town Hall audit](townhall-audit-2026-11-28.md) (`00df1695b2bb`):
  - If the Antigravity CLI (`agy`) exposes local session usage logs in the future, extend `baton/usage.py` to count external `agy` runs alongside `~/.baton/gemini-usage.jsonl` while strictly preserving the numbers-only rule (never reading conversation text).
  - Optional live runtime counter on `/machines` tracking caught background exceptions after the static AST gate (`[GN-41]`) is in place.


