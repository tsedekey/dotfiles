# Driving your agents from a phone

Three small tools, standard library only (Python 3, bash). No personal values live in this folder: your identity and addresses
go in a private config file that is not in git.

| Tool | What it does |
|---|---|
| `wm-phone` | Rearranges tmux for a small screen (one agent at a time). `wm-phone --home` (or `prefix H`) puts the desk layout back. |
| `ph-annotate` | Starts Plannotator for one artifact or one diff and publishes it on your tailnet only while the session lasts. |
| `phone-hub` | A private web page for the phone: agent list, each agent's screen, replies, a keypad for dialogs, diff review, artifacts. |

## What phone-hub can do, and what stops it doing harm

Agents often run with permissions bypassed, so text that reaches one is effectively command execution. Everything is built around that.

- **Reach:** only your own tailnet. `tailscale serve` publishes it; it listens on 127.0.0.1 only. Every request must pass, in order:
  the Host header, the Tailscale identity header, an optional source-address list, a paired-device cookie, and a PIN session.
- **No built-in identity.** `login` and `host` have no defaults; without them the hub refuses to start and answers 503.
- **No JavaScript** and a strict Content-Security-Policy. Pane text is untrusted and always escaped.
- **Typing into an agent** (Reply, keypad, review feedback) needs a fresh PIN (5 minutes), a CSRF token, and rate limits. Replies and
  review feedback show exactly what will be typed first (a reply preview is good for 10 minutes and works once). Enter, Esc and number keys
  go through a confirm page. A keypad tap is refused if the agent's screen changed since your page loaded.
- **Never typed:** text that starts with `!`, `/` or `#`, or that contains control characters. Reply is refused unless the agent is at an
  empty prompt (not working, no draft, no dialog); the keypad is refused unless a dialog is on screen.
- **Kill switch:** `phone-hub off` makes everything except `/healthz` answer 503 (`phone-hub on` undoes it). The audit log
  (`~/.local/state/phone-hub/audit.log`) records actions and lengths, never message text, PINs or tokens.

## Setup (once)

1. Tailscale: enable HTTPS certificates for the tailnet, and write access rules that let only your phone reach this machine on
   443 and on the annotate ports (8444-8450).
2. `./install.sh` links the commands into `~/.local/bin`.
3. Create `~/.config/phone-hub/config.json` (mode 600):
   `{"login": "you@example.com", "host": "this-machine.your-tailnet.ts.net", "allowed_sources": ["<your phone's tailnet IP>"]}`
   (`login` is your Tailscale login, `host` is the machine's tailnet name without a port).
4. `phone-hub set-pin` in your own terminal (prompts, echoes nothing).
5. `phone-hub ensure`, then `tailscale serve --bg --https=443 http://127.0.0.1:7480`.
6. `phone-hub pair` prints a one-time code (valid 10 minutes). Open `https://<host>/` on the phone, enter the code, then the PIN. Add the page
   to the Home Screen if you like.

`tmux/.tmux.conf` runs `phone-hub ensure` whenever the config loads, so the hub starts with tmux. `ensure` starts a small supervisor that
starts the server again if it ever dies.

## Everyday commands

`phone-hub status` / `log -n 50` / `devices` / `revoke <id>` / `off` / `on` / `restart` / `selftest`

## Known limits

- A link tapped in a notification opens your phone's default browser, never the Home Screen app, and the browser has no device cookie, so
  the push notifications carry no link.
- The agents are only reachable while the Mac is awake and on the tailnet.
- Dialog detection reads the screen (there is no API). A new agent version that words its dialogs differently shows as "unknown", and the
  keypad then refuses; add a fixture to `tests/fixtures/` and teach `DIALOG_RES` in `phone-hub`.

## Tests

```
PYTHONDONTWRITEBYTECODE=1 python3 tmux/phone/tests/phone-hub-test.py    # the hub, on a random loopback port, with fake agents and a scratch tmux
PYTHONDONTWRITEBYTECODE=1 python3 tmux/phone/tests/render-test.py       # pane rendering
PYTHONDONTWRITEBYTECODE=1 python3 tmux/phone/tests/screen-test.py       # dialog/idle/working detection against real captured screens
bash tmux/phone/tests/ph-annotate-test.sh                                # needs Tailscale; briefly publishes and removes tailnet ports 8444-8450
```

`tests/fixtures/` holds captured screens of Claude, Copilot and OpenCode (throwaway test agents only).
