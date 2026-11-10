# NYE party game: notes for Claude

A Flask server (`app.py`) plus three plain HTML pages (`pages/`): phones scan
a QR code and get tasks, the TV shows a live scoreboard and bouncing photos,
the host page runs polls and special moments. No build step, no JS framework.
The owner is not a JavaScript developer, so keep code plain and commented, and
explain the *why*.

Start with `DEVELOPMENT.md` (architecture, recipes) and the map at the top of `app.py`.

## Commands

    make check   # lint + cards.json check + tests. Run before finishing any change
    make test    # pytest only (about a second)
    make fmt     # auto-fix style (ruff)
    make dev     # run with a fake clock and a separate data folder
    make sim     # fake guests against a running server

## Rules

- All game state is the `state` dict in `app.py`. Change it only while holding `lock`, then call `save()`.
- Guests' scores and photos are the point of the party. Never delete `state.json` or `uploads/` unless asked. Tests use a temp folder (`GAME_DATA_DIR`).
- `cards.json` is hot-reloaded and validated by `tools/check_cards.py`. A new card type or field needs updating in three places: that checker, `pages/play.html` (`drawCard`), and `app.py`. Add a test.
- Pictures live in `pages/assets/` at native pixel size (see `tools/make_sprite.py`); `image` and `settings.mascot` in `cards.json` name them and the checker verifies the file exists.
- Pages talk to the server only through `/api/*`. `tests/test_pages.py` fails if a page calls an endpoint that does not exist.
- Use `hours_to_midnight()` / `current_phase()`, never `datetime.now()`, so `MIDNIGHT_ISO` and the tests can fake the clock.
- Run exactly one server process (state is in memory).
- No third-party scripts or fonts in the pages; party WiFi may not reach them.
- Do not commit `.env`, `state.json`, `uploads/`, or `.dev-data/`.
