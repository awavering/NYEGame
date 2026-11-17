# NYE Party Game

A phone-and-TV party game. Guests scan a QR code on the TV, get a task, and
their progress (and photos) show up live on the big screen.

## Run it (on your laptop)

    pip install -r requirements.txt
    python app.py

It prints three links:

- **TV**: open `/tv` full-screen in a browser on the TV (or a laptop plugged into it).
- **Phones**: guests scan the QR code on the TV, which goes to `/play`.
- **Host**: open the host link on your own phone. It ends in `?key=...`, your host key.
  Set the key by starting the server with `HOST_KEY=something python app.py` (or put it in
  `.env`, see `.env.example`). With no key set, the server makes up a random one each start.

Your laptop and the guests' phones must be on the same WiFi. If phones can't connect,
the router may have "client isolation" turned on. Turn it off, use a hotspot, or
deploy it online (see below).

## Edit your tasks: `cards.json`

Everything guests see comes from `cards.json`. You can edit it while the game is
running; new cards are picked up automatically. Each card has an `id`, `category`,
`type`, `prompt` and optional `hint` and `points`. Add `"image": "dog.png"` to show a
picture from `pages/assets/` above the prompt.

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

**First cards:** `"starter": true` marks a card a brand-new guest can get first. Keep
starters quick and friendly (a selfie, get a drink). If no card is a starter, any card can come first.

**One-time cards:** `"once": true` means the card never comes back after someone does it,
even when the deck starts over (used for "Send Albert $1"). If they skip it, it can come back.

**Mascot captions:** `"mascot_caption": true` on a text card makes the answer the mascot's
line. The mascot appears on the TV saying it in a pixel-art speech bubble for 3 minutes, or
until someone answers that card again. It stays hidden the rest of the time (used for
"Give Crispy something to say").

**Settings** at the top of `cards.json`: party name, the `albert_handle` shown in the
"Send Albert $1" card, `approve_photos` (true = photos stay off the TV until you
approve them in the host page), `tv_photo_limit` (how many photos bounce on the TV at
once), `skip_wait_seconds` (see Scoring), and `mascot` (a file in `pages/assets/`,
such as `"dog.png"`, shown on the TV when someone gives it a caption: see Mascot captions).

## Host page

- **Poll**: ask everyone at once (presets from `poll_presets`, or type your own).
  Results show on the TV as bars, a number summary, or word chips.
- **Icon match**: gives pairs of players the same emoji; they find each other.
- **Flash QR**: puts a timed QR code on the TV; anyone who scans it in time gets bonus points.
- **Midnight**: happens by itself. In the last minute the TV shows a giant countdown, then
  the finale: final scores (tied players share a medal), confetti, and a slideshow of the
  night's photos. Phones show each guest's score and place. "Finale off" resumes the game.
- **End the game early**: the same finale screen, but "Thanks for playing!" instead of
  Happy New Year. For parties that wrap up before midnight.
- **Polls** close when you press Close poll; the final results stay on the TV for 20 seconds.
- **Poll log**: every poll from tonight with its top answer. **Show on TV** brings a poll's
  final results back (no new answers) and keeps them up until you press Clear from TV.
- **Photos**: tap one to hide or show it on the TV.
- **All prompts** (link at the top, or `/prompts?key=...`): every card with its points, settings,
  secret codes, time-of-night weights, and how often it has been dealt, done and skipped tonight.
- **Past games** (link at the top, or `/archive?key=...`): every earlier game with its final
  scores, photos, polls and full log (see Past games).
- **Print the secret QR codes**: prints the QR tags for the scavenger-hunt cards.
- **Start a new game**: clears players, scores, and photos for a fresh start. Use it after
  rehearsal! Nothing is thrown away: the old game is saved first (see Past games).

## Scoring

Completing a card earns its `points`. Skipping earns 0. Poll answers earn 1, icon match 2,
flash QR 3 (adjustable), midnight guess within 10 minutes +2.

Points should match effort. Text answers and "Done" buttons can't be checked, so they're
worth 1. Photos, drawings and scavenger hunts are worth 3 to 5. Skipping is free once,
but a second skip in a row waits `skip_wait_seconds` (default 20). Doing a card ends the
wait. Without this, the best strategy is to skip everything hard and tap through the easy cards.

## Past games

Every time you start a new game, the one that just ended is saved in a dated folder,
for example `archive/2026-12-31/` (a second game the same day gets `2026-12-31-2`):

- `state.json`: the players and their scores, every card that was done or skipped (who,
  when, the prompt, the answer, the points), and every poll answer anyone gave.
- `uploads/`: all the photos and drawings.
- `cards.json`: the cards as they were that night.

Browse them on the **Past games** page (link at the top of the host page, or
`/archive?key=...`): pick a night to see its final scores, photos, polls and the full log
of who did what. These are plain files and are never loaded back into a live game. Delete
a folder if you don't want to keep that game (a rehearsal, say).

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
- `tools/`: `check_cards.py` (validates cards.json), `simulate_guests.py` (fake guests), `make_sprite.py` (pixel art to sprite)
- `pages/assets/`: pictures used by cards and the TV mascot
- `tests/`: the automated tests; `Makefile`: shortcuts; `.github/workflows/ci.yml`: runs the checks on GitHub
- `cards.json`: your tasks and settings
- `pages/play.html`: phone page; `pages/tv.html`: TV page; `pages/host.html`: host page;
  `pages/prompts.html` and `pages/archive.html`: the host's All prompts and Past games pages
- `state.json` and `uploads/`: the game in progress, created while running
- `archive/`: past games, one dated folder each (see Past games)
