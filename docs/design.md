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

- **Dark is blue-grey, never pure black** (`#0f1318` page, `#171d26` cards):
  softer on the eyes and lets shadows and borders still read.
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
