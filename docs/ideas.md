# Future ideas (backlog)

Ideas captured during discussions with Marco, ordered roughly by current interest.
None of these are scheduled in `STATUS.md` yet — promote one to `STATUS.md` → `Next`
when ready to design and build it.

## 1. Extend the Claude Code desktop look across the whole app + mobile PWA
- Bring the visual polish of `/chat` (UI-1…UI-6: crisp spacing, quieter chrome,
  compact controls, unified headers) to the remaining pages (`/`, `/p/{id}`,
  `/tasks`, `/quotas`, `/models`, `/machines`, `/ranking`).
- Add a Web App Manifest (`manifest.webmanifest`), Apple touch icon (using the
  chosen Huo icon), and minimal PWA metadata so Huo installs cleanly on the
  phone home screen over Tailscale without browser chrome.

## 2. Approve agent permissions directly from Telegram
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

## 3. Voice in `/chat` (dictation & optional read-aloud)
- Speech-to-text (STT) mic button in the `/chat` composer, either via the
  browser Web Speech API as a zero-dependency first step or a local Whisper
  server on the PC so audio never leaves the LAN.
- Optional text-to-speech (TTS) playback button on assistant messages.

## 4. Document uploads & persistent memory in `/chat`
- Allow attaching plain-text, Markdown, code files, and PDFs (extracted locally)
  in `/chat` alongside existing raster image attachments, with strict size and
  context-budget guards.
- Optional per-project or global lightweight notes/memory injected into the
  system prompt (explicit, user-editable, never hidden).
