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

## Planned (decided 2026-11-26)

The reference look is the Claude Code desktop app. UI-1 (near-black dark tokens),
UI-2 (full-height `/chat` layout), and UI-3 (composer control centre) are
complete; remaining stages UI-4 and UI-5 in `STATUS.md` restyle messages/tools
and merge the local/agent chat branches. The contrast and "both dark blocks
match" tests stay as they are.
