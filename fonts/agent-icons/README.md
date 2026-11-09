# AgentIcons

A tiny icon font so the workmux sidebar can show the real agent logos. No Nerd Font has a
Claude or OpenCode mark, and a terminal cell can only hold a character, so the logos are glyphs
in a private-use font that Ghostty maps to by codepoint (`font-codepoint-map` in `ghostty/config`).

| Codepoint | Logo | Source file |
|---|---|---|
| U+E400 | Claude (starburst) | `src/claude.svg` |
| U+E401 | Claude Code (pixel mascot) | `src/claudecode.svg` |
| U+E402 | OpenCode | `src/opencode.svg` |
| U+E403 | GitHub Copilot | `src/githubcopilot.svg` |

Used from `workmux/config.yaml` (`sidebar.agent_icons`): write the glyph plus ONE trailing space
(`" "`); the logos are two cells wide and the space is the second cell.

- **Rebuild:** `./build.py` (needs `uv`; fontTools is fetched on the fly). `--box 1000` makes the logos
  smaller, `--dump DIR` exports each glyph back out of the built font as SVG to check it.
- **Install:** `../../install.sh` copies `AgentIcons.otf` to `~/Library/Fonts`. After a rebuild, copy it
  again and reload Ghostty (cmd+shift+,); quit and reopen Ghostty if a changed glyph does not appear.
- **Metrics** are copied from JetBrainsMono Nerd Font (the font in `ghostty/config`): 1000 units/em,
  ascent 1020, descent -300, cell advance 600. Change the terminal font and these four numbers in
  `build.py` should follow.

Logo shapes: [Simple Icons](https://simpleicons.org) 16.37.0, CC0 1.0. The Claude, Claude Code, OpenCode
and GitHub Copilot marks are trademarks of their owners; this is a personal-use terminal font.
