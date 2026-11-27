# Design system (2026-11-24)

Designed for a control-panel look (light grey page, white rounded cards,
pill tabs, small monospace labels, thin bars, status dots) with a light and a
dark theme that are easy to switch. The reference was a personal dashboard; we
took the style, not its name or logo.

## Layout

- **Phone first.** Baton is used mostly from the phone over Tailscale, so
  below 900 px there is a slim top bar (brand + theme button) and a tab bar at
  the bottom (Projects, Tasks, Quotas, Ranking), clear of the iPhone home
  indicator (`env(safe-area-inset-bottom)`).
- **Desktop:** a fixed sidebar with the same four sections, the theme switch
  and the version at the bottom. The active section has a soft background and
  a 3 px accent bar on the left.

## Theme

- Three choices: **Auto** (follow the system), **Light**, **Dark**. The
  sidebar has a three-button switch; the phone top bar has one button that
  cycles through them.
- The choice is stored in `localStorage` (`baton-theme`) and set as
  `<html data-theme="light|dark">`. Auto removes the attribute.
- A tiny inline script in `<head>`, before the stylesheet, applies the saved
  choice before the first paint, so a dark page never flashes white. The
  buttons themselves are in `static/theme.js` (deferred). Without JavaScript
  the switch is hidden and Auto works through `prefers-color-scheme`.
- Alternatives not taken: a cookie read by the server (one more request
  round-trip and server state for a purely visual choice); a theme toggle
  without Auto (the system setting is the best default on a phone).

## Tokens

All colours are CSS variables in one place at the top of `static/style.css`:
the light block, then the same names for dark, twice (inside
`@media (prefers-color-scheme: dark)` for Auto, and under
`:root[data-theme="dark"]`). The rest of the CSS only uses `var(--…)`.
`tests/test_design.py` checks that no colour is written outside those blocks,
that both dark blocks match, and the contrast below.

- **Dark is neutral near-black (Claude Code desktop look)** (`#0c0c0d` page,
  `#111113` sidebar, `#161618` cards, `#1e1e21` insets, `#2a2a2e` borders —
  replaced the earlier blue-grey palette in UI-1 on 2026-11-26): avoids pure
  `#000000` so surfaces and hairline borders stay clearly separated while
  dropping the blue tint.
- **Contrast:** every text colour (`ink`, `muted`, `faint`, `accent`, `ok`,
  `warn`, `bad`) is at least 4.5:1 (WCAG AA) on cards, insets and the page, in
  both themes. The first light palette failed for labels, green and amber; it
  was darkened until the test passed.
- **Meaning:** green = fine, amber = attention (quota burning fast, queued,
  running), red = problem (exhausted, failed, high priority). States also
  carry a text label, never colour alone.
- **Providers:** Claude orange, OpenAI/Codex blue, OpenCode yellow, used for
  quota bars and token sparklines. A window burning fast or exhausted switches
  to amber or red instead.

## Components

- Cards: 14 px radius, 1 px border, very soft shadow (border only in dark).
- Labels and table heads: monospace, uppercase, letter-spaced, small.
- Numbers: tabular figures so columns don't jump.
- Pills: monospace uppercase with a status dot.
- Bars: 6 px (4 px on project cards), rounded, with the elapsed-time tick on
  quota windows.
- Fonts: system sans and system monospace only. No web fonts, icon fonts or
  CDNs: icons are inline SVG drawn for Baton.
- `prefers-reduced-motion` turns transitions off; focus is a visible 2 px
  outline in both themes.
- Buttons: a bare `<button>` gets the primary look with a *plain* colour, so
  any class on it (chips, icon buttons, links styled as buttons) can set its
  own. Only `.btn` forces its colour with `!important`, because it is also
  used on `<a>`. Never put `!important` on a bare element selector: until
  2026-11-26 `button { color: … !important }` made the `/chat` suggestion
  chips, the attach button and Sign out invisible (dark text on dark).
- `[hidden] { display: none !important; }` is global: the JS hides things
  with `el.hidden = true`, and a class that sets `display` (like `.btn`) must
  not undo that. `tests/test_design.py` checks both rules.

## `/chat` full-height layout (UI-2, 2026-11-26)

- **Scoped to `/chat` only:** `base.html` sets `<body class="page-{{ nav }}">`,
  so `chat.css` removes `main`'s `max-width` and outer padding only on
  `body.page-chat` without touching Projects, Tasks, Quotas, Models, Machines,
  or Ranking.
- **Desktop (`>= 960 px`):** the history sidebar (`.chat-sidebar`) sits flush
  to Baton's navigation rail at full viewport height (`100dvh`, `background: var(--sidebar)`,
  `1 px` hairline right border, no floating shadow or rounded outer frame).
  The conversation pane is a 3-zone flex column:
  1. **Slim topbar (`.chat-topbar`):** conversation title, subtle monospace
     folder and git branch chips (read directly from `.git/HEAD` without
     spawning `git`), New chat, and a `⋯` overflow menu (`Convert to task`,
     `Rename`, `Delete`).
  2. **Scrollable viewport (`.chat-scroll`):** internal scroll container with a
     centred `760 px` conversation column (`.chat-column`). Sticky auto-scroll
     and the `↓` jump button follow `.chat-scroll`.
  3. **Anchored composer dock (`.chat-dock`):** pinned to the bottom of the
     pane, centred to the same `760 px` measure.
- **Phone (`< 900 px`):** keeps `.mobile-top`, the bottom `.tab-bar`, and the
  slide-over history drawer (`.chat-sidebar` + backdrop) while keeping the
  composer anchored cleanly above `.tab-bar`.

## `/chat` composer control centre (UI-3, 2026-11-26)

- **Single bottom-row control bar:** agent, model (human-readable names from
  CLI catalogs or `readable_model_name()`), effort, folder, permission/tool
  mode, and local style/session resume live as compact chips in the bottom row
  of the `.composer` card with upward popovers (`.cc-popover`, clamped to the
  composer width on desktop and full-width on phones).
- **Locked, not hidden:** once an agent conversation starts (or while a local
  reply is streaming), the chips stay visible in the composer in a muted
  `.is-locked` state with a lock glyph so the active configuration always
  remains legible without pushing a separate banner into the message column.
- **Keyboard shortcuts:** `Enter` sends, `Shift+Enter` adds a newline,
  `Shift+Tab` cycles the permission/tool mode (or opens the mode popover if
  the alternate mode is disabled, explaining why), and `Esc` closes any open
  popover/menu/drawer or stops an active generation.

## `/chat` messages, tools, diffs & approvals (UI-4, 2026-11-26)

- **Cardless assistant prose:** `.msg-assistant` renders directly on the canvas
  (no card border, fill, or shadow) with comfortable line height (`1.62`) and
  subtle hover action buttons; `.msg-user` uses an understated inset pill.
- **Compact one-line tool rows with status dots:** `.ag-tool` and grouped
  `.ag-tool-group` render as sleek expandable rows led by an 8 px `.ag-dot`
  (pulsing amber while running, green on completion, red on failure; respects
  `prefers-reduced-motion`) and a subtle status text label.
- **Inline diffs with line numbers:** `BatonChat.diffBlock()` renders old/new
  tabular line-number gutters, `+added` / `-removed` header counts, and
  collapses unchanged stretches (>3 context lines from edits) cleanly.
- **Inline approvals with `1` / `2` shortcuts:** Allow/Deny buttons display
  `1` / `2` key caps; pressing `1` or `2` settles the active pending approval
  when focus is outside any text input or textarea.
- **Live `"Thinking… Ns"` timer & status line:** reasoning blocks stay
  collapsed by default with a live elapsed timer (`Thinking… Ns` → `Thought for Ns`),
  and `.composer-status` pairs keyboard/live status on the left with the context
  usage meter (or model/turn status) on the right.

## Unified `/chat` component (UI-5, 2026-11-27)

- **Single template (`templates/chat.html`):** the earlier split between an
  Agent branch and a Local branch was merged into one `<section class="chat" data-chat-root>`,
  one topbar, one scrollable log, one composer, one status bar, and one
  `#chat-strings` i18n payload built in Python (`_chat_strings()` in
  `baton/routes/chat.py`). Only the backend-specific hidden native controls,
  banners, and status-right widget vary between local models and ACP agents.
- **Single message & tool renderer (`static/chat-common.js`):** assistant
  bubbles (`createAssistantBubble`), copy buttons (`attachCopyButton`),
  collapsible read-only tool groups, one-line tool cards, line-numbered diffs,
  inline `1`/`2` approvals (`createToolsView`), and composer helpers
  (`initComposerControls`, `selectedLabel`, `shortFolder`, `bindAutosize`) live
  once in `window.BatonChat`.
- **Thin transport adapters:** `static/chat.js` (local `llama-server` & local
  tool harness) and `static/agents-chat.js` (ACP agents over SSE) only wire
  their session lifecycle, persistence, and transport endpoints into the shared
  component. `tests/test_design.py` guards both the single-template invariant
  and the absence of duplicated renderer code.

## `/chat` polish after review (UI-6, 2026-11-27)

- **Keyboard:** Shift+Tab cycles the permission mode only from the message box;
  everywhere else it moves focus backwards, as it should for keyboard users.
- **Restraint:** one accent action per view. The send button is a round arrow
  (text kept as its accessible name), "New chat" is a neutral button in the
  history column and an icon in the header only where the history is a drawer,
  and Deny is neutral (Allow is the primary action).
- **Header:** the folder chip shows the last folder name (`scratch (pruebas)`),
  never a full path; the full value is in its tooltip. On phones it hides: the
  folder already shows in its composer chip.
- **Tools:** kind labels are translated (Leer, Editar, Ejecutar…), groups use
  singular/plural, edits open their diff straight away (once, so a folded diff
  stays folded), and arguments that repeat the title are not shown twice.
  Tool rows are saved with the assistant turn and restored on reload.
- **Quiet chrome:** no "Keys: 1 allow · 2 deny" prose (the keycaps say it, and
  they hide on touch screens); the keys hint under the composer shows only
  while typing; the status line no longer repeats the model; the empty state
  just says what to ask.
- **Phone:** header is ☰ · title · ＋ · ⋯; composer chips sit in one row that
  scrolls sideways instead of wrapping into three.

## Brand & icon: Huo (2026-11-27)

- **Visible brand vs. technical internals:** the web UI and mobile
  notifications default to **Huo** (`DEFAULT_TITLE = "Huo"` in
  `baton/config.py`) while keeping the Python package (`baton/`), CLI
  (`baton`), environment variables (`BATON_*`), data directory (`~/.baton`),
  and Windows scheduled task untouched for zero-breakage compatibility.
- **Dynamic `{app}` strings:** every visible brand mention in `baton/i18n.py`
  uses `{app}` (substituted via safe `str.replace` in `strings(lang, app)` so
  `%s` and `{port}` placeholders and literal braces never collide), and
  `Notifier` receives `app_name=settings.title` so Telegram and Webhook
  headings match `BATON_TITLE` automatically (keeping `"source": "baton"` in
  structured webhook JSON).
- **Single-source SVG icon (`static/huo-icon.svg`):** replaces the plain `◆`
  glyph in the desktop sidebar, mobile header, `/login`, and `<link rel="icon">`
  favicon, cache-busted by `css_v` (which now hashes `.svg` alongside `.css`
  and `.js`). Designed as mischievous Mr. Tail (spectral jade fox-flame with
  slanted golden eyes) in front of a golden Taoist talisman carrying the
  vermilion *Li* trigram (`☲`, Fire) seal — inspired by Huohuo from
  *Honkai: Star Rail* — inside a dark obsidian-jade squircle so it stays sharp
  and high-contrast on both light and near-black dark themes at 16 px, 30 px,
  and 34 px.

## Global Claude Code desktop look & mobile PWA (2026-11-27)

- **Warm off-white light palette & neutral primary actions:** light mode moved
  from cool blue-grey (`#eef1f5`) to warm Claude Code parchment (`#faf9f5` page,
  `#f3f1ea` rail/insets, `#ffffff` cards, `#e9e6dd` selected, `#e4e1d8`
  hairlines). Primary buttons and active filter chips use `--primary` /
  `--on-primary` (charcoal `#1f1e1c` on `#faf9f5` in light; crisp `#ececef` on
  `#0c0c0d` in dark, both >15:1 contrast), reserving `--accent` blue strictly
  for hyperlinks, focus rings, and progress indicators.
- **Collapsible global sidebar rail (`baton-sidebar`):** desktop (`>= 900 px`)
  adds a `[data-sidebar-toggle]` button in `.side-top` that flips
  `<html data-sidebar="collapsed|expanded">` (`220 px` ↔ `56 px` icon rail) and
  persists the choice in `localStorage` (`baton-sidebar`), applied before first
  paint in `<head>`. When no preference is stored yet, `/chat` starts collapsed
  to maximize conversation width while all other pages start expanded. The
  collapsed rail keeps native tooltips, alert badges on the Machines icon, a
  single `.side-theme-cycle` toggle, and an icon-only Sign out button.
- **Scrollable mobile tab bar:** with 8 sections in the bottom bar, fixed
  equal-width grid columns collided on 390 px screens; `.tab-bar` is now a
  horizontally scrollable flex strip (`scroll-snap-type: x proximity`) and
  `theme.js` centers the active tab into view on load.
- **Unified page chrome across all views:** consistent `.page-head` headers with
  a subtle bottom hairline, quiet `.back-link` navigation bars, neutral
  `.next-up` and segmented `.score-switch` controls, non-wrapping card footer
  actions, chevron accordions on `/machines`, and mono uppercase field labels on
  `/townhall`.
- **Installable mobile PWA:** dynamic `GET /manifest.webmanifest`
  (`application/manifest+json`, using `BATON_TITLE` for `name`/`short_name`,
  exempt from session auth so install prompts work when `BATON_ACCESS_KEY` is
  set), PNG icons rendered from `huo-icon.svg` (`apple-touch-icon.png` 180²,
  `huo-icon-192.png`, `huo-icon-512.png`), plus `theme-color` light/dark and
  standalone iOS/Android meta tags on both `base.html` and `login.html`.

