# NYE Party Game

A phone-and-TV party game. Guests scan a QR code on the TV, get a task, and
their progress (and photos) show up live on the big screen.

## Run it (on your laptop)

    pip install -r requirements.txt
    python app.py

It prints three links:

- **TV**: open `/tv` full-screen in a browser on the TV (or a laptop plugged into it).
- **Phones**: guests scan the QR code on the TV, which goes to `/play`.
- **Host**: open `/host?key=host` on your own phone. Change the key by starting the
  server with `HOST_KEY=something python app.py`.

Your laptop and the guests' phones must be on the same WiFi. If phones can't connect,
the router may have "client isolation" turned on. Turn it off, use a hotspot, or
deploy it online (see below).

## Edit your tasks: `cards.json`

Everything guests see comes from `cards.json`. You can edit it while the game is
running; new cards are picked up automatically. Each card has an `id`, `category`,
`type`, `prompt` and optional `hint` and `points`.

| type | what the phone shows |
|---|---|
| `confirm` | Done / Skip buttons |
| `text` / `number` | a text or number box |
| `photo` | camera (`"camera": "user"` for selfie, `"library"` for camera roll) |
| `draw` | a finger-drawing pad; `{thing}` in the prompt is replaced with a random item from `draw_things` |
| `secret_qr` | tells them to find and scan a hidden QR (set a `code`) |
| `timer` | Start button, countdown, Done unlocks after `minutes` |
| `gforce` | 10-second accelerometer test |
| `light` | live camera with a brightness meter, then snap a photo |

After editing, run `python tools/check_cards.py` to catch mistakes (duplicate ids, a missing
secret code, a typo in a card type). The server runs the same check on every save and, if
there is a mistake, keeps using your last good version and prints what is wrong in the terminal.

**Time of night:** add `"weights": {"early": 1, "mid": 1, "late": 1}` to a card to make it
more or less likely. "Early" is more than 2 hours before midnight, "mid" is 2 hours to
45 minutes before, "late" is the last 45 minutes. A weight of 0 means never.

**Settings** at the top of `cards.json`: party name, the `albert_handle` shown in the
"Send Albert $1" card, `approve_photos` (true = photos stay off the TV until you
approve them in the host page), and how many photos bounce on the TV at once.

## Host page

- **Poll**: ask everyone at once (presets from `poll_presets`, or type your own).
  Results show on the TV as bars, a number summary, or word chips.
- **Icon match**: gives pairs of players the same emoji; they find each other.
- **Flash QR**: puts a timed QR code on the TV; anyone who scans it in time gets bonus points.
- **Finale**: midnight screen with final scores. Turn it off to resume.
- **Photos**: tap one to hide or show it on the TV.
- **Print the secret QR codes**: prints the QR tags for the scavenger-hunt cards.
- **Reset**: wipes players, scores, and photos. Use it after rehearsal!

## Scoring

Completing a card earns its `points`. Skipping earns 0. Poll answers earn 1, icon match 2,
flash QR 3 (adjustable), midnight guess within 10 minutes +2.

## Sensors need https

The G-force and live-camera "brightest light" cards only work on `https://` pages
(a browser rule for phone sensors). On plain `http://` they fall back: brightest
light becomes a normal photo, and G force can be skipped. To get https, deploy it
(Render, Railway, or Fly.io all give you https) or use a tunnel like Cloudflare Tunnel
or ngrok pointing at your laptop. With a tunnel, start the server with
`BASE_URL=https://your-tunnel-address python app.py` so the QR code uses it.

## Working on the code

See [DEVELOPMENT.md](DEVELOPMENT.md). The short version:

    make setup    # one time
    make check    # lint, card check, tests
    make dev      # rehearse with a fake clock (your real game data is untouched)
    make sim      # fake guests, so the TV has something to show

## Files

- `app.py`: the server (all game rules). Start reading at the map at the top
- `tools/`: `check_cards.py` (validates cards.json), `simulate_guests.py` (fake guests)
- `tests/`: the automated tests; `Makefile`: shortcuts; `.github/workflows/ci.yml`: runs the checks on GitHub
- `cards.json`: your tasks and settings
- `pages/play.html`: phone page; `pages/tv.html`: TV page; `pages/host.html`: host page
- `state.json` and `uploads/`: created while running; delete both for a clean start
