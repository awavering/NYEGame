# Development guide

How to change this game without breaking it. For running it at the party, see
[README.md](README.md).

## Quick start

    make setup     # one time: creates .venv and installs everything
    make check     # lint + card check + tests (the same things CI runs)
    make dev       # rehearse: fake clock, separate data folder
    make sim       # in a second terminal: fake guests play the game

Run `make` on its own to see every shortcut. Optional: `make hooks` runs
`make check` before each commit.

## How the pieces fit

    phones  pages/play.html ──┐                       ┌── pages/tv.html   (the big screen)
       asks /api/me every 2s  │                       │   asks /api/tv every 1.5s
       posts answers          ▼                       ▼
                         ┌─────────────── app.py (Flask) ───────────────┐
    host   pages/host.html ─▶  one dict, `state`, guarded by `lock`      │
       /api/host/*        │   saved to state.json after every change     │
                          │   cards.json re-read whenever it is edited   │
                          └───────────────────────────────────────────────┘
                                     uploads/ (resized photos)

There is no build step and no JavaScript framework. Every page is one HTML
file with its script inline. Phones and the TV *poll*; nothing is pushed.
That is plenty for 20 to 40 guests and keeps the code easy to follow.

### The state dict

| key | what it holds |
|---|---|
| `players` | `pid -> {name, points, seen, current, joined}`. `seen` maps card id to `done` or `declined`; `current` is the card they hold now |
| `history` | every completed, declined, or bonus event, in order (the TV stats and feed come from this) |
| `assigned` | card id -> how many times it has been dealt (used to even out the deck) |
| `photos` | uploaded pictures and whether they are `visible` or `hidden` |
| `poll`, `icon_sync`, `flash`, `finale` | the host moments |

### The API

| who | calls |
|---|---|
| phones | `GET /api/me`, `POST /api/join`, `/api/card/respond`, `/api/card/start`, `/api/card/photo`, `/api/secret`, `/api/flash/claim`, `/api/poll/answer`, `/api/icon/found` |
| TV | `GET /api/tv`, `GET /api/config` |
| host | `GET /api/host/data`, `POST /api/host/poll`, `/poll/<close\|clear>`, `/icons/<start\|clear>`, `/flash`, `/finale`, `/photo`, `/reset` |

Host calls need the key (`?key=` or an `X-Host-Key` header).

## Recipes

### Change or add a card
Edit `cards.json`, then `make cards`. You can do this while the game is
running. If the file has a mistake, the server keeps the last good version and
prints what is wrong in the terminal, so a typo can't take the game down.

### Add a new card type
1. `tools/check_cards.py`: add the name to `CARD_TYPES` (plus any new fields to `_check_card`).
2. `pages/play.html`: write a handler (see `cardConfirm`, `cardText`, ...) and add it to the map in `drawCard()`.
3. `app.py`: if it is answered with a plain button or text, `api_respond()` may already work. If it needs special scoring or a different submission, handle it there (photos go through `api_photo()`).
4. Use it in `cards.json`, run `make cards`.
5. Add a test to `tests/test_game.py`. Copy a similar one (`test_timer_card`, `test_secret_qr`).

### Add a host moment (like a poll or the flash QR)
Add state in `fresh_state()`, a `/api/host/...` endpoint, a button in
`pages/host.html`, what the TV shows in `showOverlay()` (`pages/tv.html`) plus
the data it needs in `api_tv()`, and what phones see through `/api/me`.
`test_pages.py` fails if a page calls an endpoint that does not exist.

### Fake the clock
`make dev HOURS=0.2` starts the game with midnight 12 minutes away, so you can
see the late-night card weights and the finale. `HOURS=0.03` (about 2 minutes)
is handy for watching the countdown roll into the finale. The automatic finale
only happens once per game; `make reset` or a new `.dev-data/` brings it back. It uses `.dev-data/`, never
your real `state.json`. Do not call `datetime.now()` in new code. Use
`hours_to_midnight()` or `current_phase()` so the fake clock and tests work.

### Rehearse
Terminal 1: `make dev`. Terminal 2: `make sim` (it uses `PORT` from `.env` too;
on a Mac, port 5000 is often taken by AirPlay, so `PORT=5001` is a good choice). Open the TV link in a browser
and the host link on your phone, and try polls, icon match, flash QR, and the
finale. Do one real round on actual phones too, because the camera and motion
sensors can't be simulated.

## Before the party

- [ ] Put the real payment handle in `settings.albert_handle` (`make cards` warns until you do).
- [ ] Set `HOST_KEY` to something other than `host`.
- [ ] `make cards` and `make check` pass.
- [ ] Rehearse on real phones, on the real WiFi, with the real TV.
- [ ] G-force and "brightest light" need `https://`. Test them through your tunnel or host.
- [ ] Print the secret QR tags (host page) and stick them up. Scan each once.
- [ ] `make reset` (or the host page's Reset) so rehearsal photos and scores are gone.

## Testing notes

- `make test` takes about a second. Tests run the real Flask app against a temp folder with a fake clock.
- `tests/helpers.py` has a `Player` class that behaves like a phone (`give()` forces a card, `respond()`, `upload()`).
- `test_pages.py` runs `node --check` on each page's script (skipped if Node is missing) and checks every `/api/...` path the pages use exists on the server.
- There are no browser tests. The pages are simple enough that the contract test plus a rehearsal covers it.

## Code conventions

- Change `state` only while holding `lock`, then call `save()`.
- Comments explain *why*, not what. Keep the map at the top of `app.py` current.
- `ruff format` owns code style (`make fmt`).
- Pages must keep working with no network access to third-party sites (no CDN scripts): the party WiFi might not reach them.

## Deploying

Run **one** server process. The game state lives in that process's memory (and
`state.json`), so several worker processes would each see a different game and
guests would appear to vanish. With a production server use one worker and
several threads, for example `gunicorn -w 1 --threads 8 app:app`. If the host
wipes its disk on restart, point `GAME_DATA_DIR` at a persistent disk. Set
`BASE_URL` to the public https address so the QR codes use it.

## Known limits and ideas

- **Polling, not push.** A 1 to 2 second lag. If it ever bothers you, Server-Sent Events are the smallest upgrade.
- **Identity is a browser setting.** A guest's id is stored in their phone's `localStorage`. Clearing browser data makes them a new player. There is no rename or leave.
- **Honor system.** Nothing checks that someone really sent the $1 or used the no-phone box.
- **Photos are only moderated by hiding.** `approve_photos` makes new photos wait for the host.
- **One party per data folder.** Use `make reset` or a new `GAME_DATA_DIR` between events.
- **The TV spotlights new photos** at most once every 15 seconds (`SPOT_GAP_MS` in `tv.html`), so a busy party doesn't hide the scoreboard.
- Ideas not built: a results export, per-card stats on the host page, a host "undo points" button, sound on the TV at milestones, rate-limiting uploads.
