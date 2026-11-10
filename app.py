"""
NYE party game server.

Run it:   python app.py
TV:       http://<your-ip>:5000/tv
Phones:   scan the QR code on the TV (goes to /play)
Host:     http://<your-ip>:5000/host?key=host     (change the key with HOST_KEY)

Everything that guests see is driven by cards.json. State is saved to
state.json after every change, so a restart does not lose the game.

Map of this file (top to bottom):
  Cards        load_cards(): cards.json, re-read when edited, validated
  State        one dict (`state`) guarded by `lock`, saved with save()
  Time         hours_to_midnight() / current_phase() drive card weights
  Dealing      pick_card() chooses the next card, finish() records the result
  Pages + QR   the HTML pages and generated QR images
  Player API   what phones call (/api/me, /api/card/*, /api/poll/answer, ...)
  TV API       /api/tv: everything the big screen shows, in one response
  Host API     /api/host/*: polls, icon match, flash QR, finale, photo hiding
               (the midnight finale also switches itself on: check_midnight())

Adding a card type (full checklist in DEVELOPMENT.md):
  1. tools/check_cards.py  add the name to CARD_TYPES (and any new fields)
  2. pages/play.html       add a handler function and list it in drawCard()
  3. app.py                api_respond() / api_photo() decide how it is scored
  4. cards.json            use it, then run: python tools/check_cards.py
  5. tests/test_game.py    add a test for it
"""

import io
import json
import math
import os
import random
import socket
import threading
import time
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

import qrcode
from flask import Flask, abort, jsonify, request, send_file, send_from_directory
from PIL import Image, ImageOps
from werkzeug.middleware.proxy_fix import ProxyFix

from tools.check_cards import validate as validate_cards

BASE = os.path.dirname(os.path.abspath(__file__))
PAGES = os.path.join(BASE, "pages")
# Everything the game writes while running (state.json, uploads/) goes in
# DATA_DIR. Override with GAME_DATA_DIR; the tests point it at a temp folder
# so they can never touch a real game.
DATA_DIR = os.environ.get("GAME_DATA_DIR", BASE)
UPLOADS = os.path.join(DATA_DIR, "uploads")
STATE_FILE = os.path.join(DATA_DIR, "state.json")
CARDS_FILE = os.environ.get("CARDS_FILE", os.path.join(BASE, "cards.json"))
os.makedirs(UPLOADS, exist_ok=True)

HOST_KEY = os.environ.get("HOST_KEY", "host")
PORT = int(os.environ.get("PORT", "5000"))
TZ = ZoneInfo(os.environ.get("TZ_NAME", "America/Denver"))
# For testing you can fake the countdown, e.g. MIDNIGHT_ISO=2026-10-16T00:00:00
MIDNIGHT_ISO = os.environ.get("MIDNIGHT_ISO")
# How long a closed poll's results stay on the TV before it goes back to normal.
POLL_RESULTS_SECONDS = 20

ICONS = [
    "🦄",
    "🍕",
    "🐙",
    "🚀",
    "🎸",
    "🌵",
    "🍩",
    "🦖",
    "🎈",
    "🐢",
    "🍔",
    "🦊",
    "🎯",
    "🐼",
    "🍉",
    "👾",
    "🎩",
    "🐝",
    "🍿",
    "🦋",
]

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
lock = threading.RLock()


# --------------------------------------------------------------------------
# Cards (re-read from disk whenever cards.json changes, so you can edit live)
# --------------------------------------------------------------------------
_cards_cache = {"mtime": 0, "bad_mtime": None, "data": None, "errors": []}


def load_cards():
    """Return the parsed cards.json, re-reading it whenever the file changes.

    If a saved edit has mistakes (see tools/check_cards.py) we keep serving
    the last good version and print what is wrong once, instead of crashing
    the game in the middle of the party. With no earlier good version (the
    very first load) there is nothing to fall back on, so we raise.
    """
    mtime = os.path.getmtime(CARDS_FILE)
    if mtime not in (_cards_cache["mtime"], _cards_cache["bad_mtime"]):
        try:
            with open(CARDS_FILE, encoding="utf-8") as f:
                data = json.load(f)
            errors, warnings = validate_cards(data)
        except (OSError, ValueError) as exc:  # includes JSON syntax errors
            data, errors, warnings = None, [f"could not read cards.json: {exc}"], []
        if errors:
            _cards_cache.update(bad_mtime=mtime, errors=errors)
            keeping = "keeping the last good version" if _cards_cache["data"] else "no earlier version"
            print(f"\ncards.json has errors ({keeping}):")
            for e in errors:
                print(f"  - {e}")
        else:
            _cards_cache.update(mtime=mtime, bad_mtime=None, data=data, errors=[])
            for w in warnings:
                print(f"cards.json warning: {w}")
    if _cards_cache["data"] is None:
        raise RuntimeError("cards.json has problems:\n  - " + "\n  - ".join(_cards_cache["errors"]))
    return _cards_cache["data"]


def settings():
    return load_cards().get("settings", {})


def card_by_id(card_id):
    for c in load_cards()["cards"]:
        if c["id"] == card_id:
            return c
    return None


# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------
def fresh_state():
    return {
        "players": {},  # pid -> {name, points, seen, current, joined}
        "history": [],  # every completed / declined / bonus event
        "assigned": {},  # card_id -> times handed out
        "photos": [],  # uploaded pictures
        "poll": None,
        "icon_sync": None,
        "flash": None,
        "finale": False,  # False, "midnight" (Happy New Year) or "thanks" (host ended early)
        "midnight_done": False,  # the automatic midnight finale has happened
    }


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                s = json.load(f)
            base = fresh_state()
            base.update(s)
            return base
        except Exception:
            pass
    return fresh_state()


def save():
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f)
    os.replace(tmp, STATE_FILE)


state = load_state()


# --------------------------------------------------------------------------
# Time of night
# --------------------------------------------------------------------------
def hours_to_midnight():
    """Hours until the next midnight (negative after it, on Jan 1).

    Set MIDNIGHT_ISO=2026-12-31T22:00:00 (in TZ_NAME) to fake the clock."""
    now = datetime.now(TZ)
    if MIDNIGHT_ISO:
        target = datetime.fromisoformat(MIDNIGHT_ISO).replace(tzinfo=TZ)
    elif now.month == 1 and now.day == 1:
        target = datetime(now.year, 1, 1, tzinfo=TZ)
    else:
        target = datetime(now.year + 1, 1, 1, tzinfo=TZ)
    return (target - now).total_seconds() / 3600


def check_midnight():
    """At midnight, switch on the finale by itself (once). Call while holding lock.

    It only happens once, so the host can turn the finale off afterwards and
    keep playing. If the host already ended the game early, that stays."""
    if not state["midnight_done"] and hours_to_midnight() <= 0:
        state["midnight_done"] = True
        if not state["finale"]:
            state["finale"] = "midnight"
        save()


def finale_kind():
    # Older saved games stored True, which meant the midnight finale.
    return "thanks" if state["finale"] == "thanks" else "midnight"


def current_phase():
    h = hours_to_midnight()
    if h >= 2:
        return "early"
    if h >= 0.75:
        return "mid"
    return "late"


# --------------------------------------------------------------------------
# Handing out cards
# --------------------------------------------------------------------------
def pick_card(pid, first=False):
    """Choose the player's next card and store it as p["current"].

    A player sees every card once before any repeats. Chance = the card's
    weight for the current phase of the night (cards.json "weights"), divided
    by (1 + 0.5 x times it has already been handed out), which spreads the
    deck evenly across guests.

    first=True (just joined): only "starter" cards, so nobody's first task is
    "put your phone in a box for 15 minutes"."""
    p = state["players"][pid]
    pool = load_cards()["cards"]
    unseen = [c for c in pool if c["id"] not in p["seen"]]
    if not unseen:  # they have seen everything: start over...
        # ...except "once" cards they already did (like "Send Albert $1"),
        # which stay remembered so they never come back.
        p["seen"] = {
            cid: result
            for cid, result in p["seen"].items()
            if result == "done" and (card_by_id(cid) or {}).get("once")
        }
        unseen = [c for c in pool if c["id"] not in p["seen"]] or pool
    phase = current_phase()
    if first:
        starters = [c for c in unseen if c.get("starter") and c.get("weights", {}).get(phase, 1) > 0]
        if starters:  # no starter cards in cards.json? then any card will do
            unseen = starters
    weights = []
    for c in unseen:
        w = c.get("weights", {}).get(phase, 1)
        # Cards that have already been handed out a lot get a little less likely,
        # which spreads the deck evenly across guests.
        w = w / (1 + 0.5 * state["assigned"].get(c["id"], 0))
        weights.append(max(w, 0))
    if sum(weights) == 0:
        weights = [1] * len(unseen)
    card = random.choices(unseen, weights)[0]

    extra = {}
    if card["type"] == "draw":
        extra["thing"] = random.choice(load_cards().get("draw_things", ["something silly"]))
    p["current"] = {"card_id": card["id"], "assigned_at": time.time(), "started_at": None, "extra": extra}
    state["assigned"][card["id"]] = state["assigned"].get(card["id"], 0) + 1
    return p["current"]


def render_prompt(card, cur):
    text = card["prompt"]
    text = text.replace("{albert_handle}", settings().get("albert_handle", ""))
    text = text.replace("{thing}", cur.get("extra", {}).get("thing", "something"))
    return text


def public_card(p):
    cur = p.get("current")
    if not cur:
        return None
    card = card_by_id(cur["card_id"])
    if not card:
        return None
    out = {
        "id": card["id"],
        "type": card["type"],
        "category": card["category"],
        "prompt": render_prompt(card, cur),
        "hint": card.get("hint", ""),
        "points": card.get("points", 1),
        "placeholder": card.get("placeholder", ""),
        "camera": card.get("camera", "environment"),
        "minutes": card.get("minutes"),
        "started_at": cur.get("started_at"),
    }
    if card["type"] == "timer" and cur.get("started_at"):
        out["ready_at"] = cur["started_at"] + card["minutes"] * 60
    if card["type"] == "secret_qr":
        out["location"] = card.get("location", "")
    return out


def ensure_current(p, pid):
    """Make sure the player has a valid card (cards.json may have changed)."""
    cur = p.get("current")
    if state["finale"]:
        return
    if not cur or not card_by_id(cur["card_id"]):
        pick_card(pid)


def finish(pid, card, result, answer=None, photo_id=None, bonus=0, note=None):
    """Record the outcome of a card and give the player their next one."""
    p = state["players"][pid]
    cur = p.get("current") or {}
    points = (card.get("points", 1) + bonus) if result == "done" else 0
    p["points"] += points
    p["seen"][card["id"]] = result
    if result == "done":
        # Doing a card earns back the right to skip straight away.
        p.pop("skip_ready_at", None)
    state["history"].append(
        {
            "t": time.time(),
            "pid": pid,
            "name": p["name"],
            "card_id": card["id"],
            "category": card["category"],
            "prompt": render_prompt(card, cur),
            "result": result,
            "answer": answer,
            "photo": photo_id,
            "points": points,
        }
    )
    p["current"] = None
    if not state["finale"]:
        pick_card(pid)
    save()
    return {"ok": True, "points_earned": points, "note": note}


def add_event(pid, text, points):
    """Award points that are not tied to a card (polls, icon match, flash QR)."""
    p = state["players"][pid]
    p["points"] += points
    state["history"].append(
        {
            "t": time.time(),
            "pid": pid,
            "name": p["name"],
            "card_id": None,
            "category": "Bonus",
            "prompt": text,
            "result": "bonus",
            "answer": None,
            "photo": None,
            "points": points,
        }
    )


def skip_wait(p):
    """Whole seconds before this player may skip again (0 = they can now).

    Skipping is free once, but a second skip in a row has to wait
    (settings.skip_wait_seconds). Without this, the best strategy was to
    skip every card that takes effort and farm the quick ones."""
    return max(0, math.ceil(p.get("skip_ready_at", 0) - time.time()))


def get_player(pid):
    p = state["players"].get(pid)
    if not p:
        abort(404, description="unknown_player")
    return p


# --------------------------------------------------------------------------
# Pages
# --------------------------------------------------------------------------
@app.get("/")
def index():
    return send_from_directory(PAGES, "play.html")


@app.get("/play")
def play_page():
    return send_from_directory(PAGES, "play.html")


@app.get("/tv")
def tv_page():
    return send_from_directory(PAGES, "tv.html")


@app.get("/host")
def host_page():
    check_host()
    return send_from_directory(PAGES, "host.html")


@app.get("/host/print")
def host_print():
    check_host()
    return send_from_directory(PAGES, "print.html")


@app.get("/s/<code>")
def secret_page(code):
    return send_from_directory(PAGES, "claim.html")


@app.get("/f/<token>")
def flash_page(token):
    return send_from_directory(PAGES, "claim.html")


@app.get("/photos/<path:name>")
def photo_file(name):
    return send_from_directory(UPLOADS, name)


@app.get("/api/config")
def api_config():
    return jsonify({"party_name": settings().get("party_name", "Party Game")})


def check_host():
    key = request.headers.get("X-Host-Key") or request.args.get("key")
    if key != HOST_KEY:
        abort(403)


# --------------------------------------------------------------------------
# QR codes
# --------------------------------------------------------------------------
def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def base_url():
    env = os.environ.get("BASE_URL")
    if env:
        return env.rstrip("/")
    host = request.host
    if host.startswith("localhost") or host.startswith("127.0.0.1"):
        return f"http://{lan_ip()}:{PORT}"
    return request.host_url.rstrip("/")


def qr_response(url, cacheable=False):
    """PNG of a QR code for `url`.

    Secret and flash codes never change for a given path, so browsers may
    cache them. (The TV redraws the flash overlay every tick; without caching
    it re-downloads the image each time and flickers.) The join code depends
    on this machine's current address, so it is never cached."""
    img = qrcode.make(url, box_size=12, border=2)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    buf.seek(0)
    resp = send_file(buf, mimetype="image/png")
    resp.headers["Cache-Control"] = "public, max-age=3600" if cacheable else "no-store"
    return resp


@app.get("/qr/join.png")
def qr_join():
    return qr_response(base_url() + "/play")


@app.get("/qr/secret/<code>.png")
def qr_secret(code):
    return qr_response(f"{base_url()}/s/{code}", cacheable=True)


@app.get("/qr/flash/<token>.png")
def qr_flash(token):
    return qr_response(f"{base_url()}/f/{token}", cacheable=True)


# --------------------------------------------------------------------------
# Player API
# --------------------------------------------------------------------------
@app.post("/api/join")
def api_join():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()[:20]
    if not name:
        return jsonify({"error": "Please enter a name"}), 400
    with lock:
        pid = uuid.uuid4().hex[:10]
        state["players"][pid] = {
            "name": name,
            "points": 0,
            "seen": {},
            "current": None,
            "joined": time.time(),
        }
        pick_card(pid, first=True)
        save()
    return jsonify({"pid": pid})


def poll_view_for(pid):
    poll = state["poll"]
    if not poll or not poll["open"]:
        return None
    return {
        "id": poll["id"],
        "question": poll["question"],
        "kind": poll["kind"],
        "options": poll.get("options", []),
        "answered": pid in poll["answers"],
    }


def icon_view_for(pid):
    sync = state["icon_sync"]
    if not sync or pid not in sync["assign"]:
        return None
    return {
        "id": sync["id"],
        "emoji": sync["assign"][pid],
        "found": pid in sync["found"],
        "group": sync["group_size"][pid],
    }


@app.get("/api/me")
def api_me():
    pid = request.args.get("pid", "")
    with lock:
        p = state["players"].get(pid)
        if not p:
            return jsonify({"error": "unknown_player"}), 404
        check_midnight()
        ensure_current(p, pid)
        # Place on the leaderboard (ties share a place), for the finale screen.
        rank = 1 + sum(1 for other in state["players"].values() if other["points"] > p["points"])
        return jsonify(
            {
                "name": p["name"],
                "points": p["points"],
                "card": None if state["finale"] else public_card(p),
                "poll": poll_view_for(pid),
                "icon": icon_view_for(pid),
                "finale": bool(state["finale"]),
                "finale_kind": finale_kind(),
                "rank": rank,
                "skip_wait": skip_wait(p),
                "now": time.time(),
            }
        )


def current_card_or_400(pid, card_id):
    p = get_player(pid)
    cur = p.get("current")
    if not cur or cur["card_id"] != card_id:
        abort(400, description="That card has changed. Pull to refresh.")
    return p, cur, card_by_id(card_id)


@app.post("/api/card/respond")
def api_respond():
    data = request.get_json(silent=True) or {}
    pid, card_id = data.get("pid"), data.get("card_id")
    action = data.get("action")
    answer = data.get("answer")
    with lock:
        p, cur, card = current_card_or_400(pid, card_id)
        if action == "decline":
            wait = skip_wait(p)
            if wait:
                return jsonify({"error": f"Give this one a try! You can skip again in {wait}s."}), 400
            p["skip_ready_at"] = time.time() + settings().get("skip_wait_seconds", 20)
            return jsonify(finish(pid, card, "declined"))

        t = card["type"]
        bonus, note = 0, None
        if t in ("photo", "draw", "light", "secret_qr"):
            return jsonify({"error": "Use the upload or scan for this one."}), 400

        if t == "timer":
            if not cur.get("started_at"):
                return jsonify({"error": "Press Start first."}), 400
            if time.time() < cur["started_at"] + card["minutes"] * 60:
                return jsonify({"error": "Not yet! The timer is still running."}), 400

        if t in ("text", "number", "gforce"):
            answer = str(answer or "").strip()[:60]
            if not answer:
                return jsonify({"error": "Please enter an answer."}), 400

        if t in ("number", "gforce"):
            try:
                value = float(answer)
            except ValueError:
                return jsonify({"error": "That needs to be a number."}), 400
            if card.get("grade") == "minutes_to_midnight":
                actual = hours_to_midnight() * 60
                diff = abs(value - actual)
                note = f"Actual: {round(actual)} min. You were off by {round(diff)}."
                if diff <= 10:
                    bonus, note = 2, note + " Bonus +2!"
            elif card.get("grade") == "scoreboard_number":
                actual = sum(1 for h in state["history"] if h["result"] == "done")
                diff = abs(value - actual)
                note = f"The scoreboard says {actual}."
                if diff <= 2:
                    bonus, note = 1, note + " Close enough, +1!"
            elif t == "gforce":
                note = f"{value:.2f} g recorded!"
        return jsonify(finish(pid, card, "done", answer=answer, bonus=bonus, note=note))


@app.post("/api/card/start")
def api_start():
    data = request.get_json(silent=True) or {}
    with lock:
        p, cur, card = current_card_or_400(data.get("pid"), data.get("card_id"))
        if card["type"] == "timer" and not cur.get("started_at"):
            cur["started_at"] = time.time()
            save()
        return jsonify({"ok": True})


@app.post("/api/card/photo")
def api_photo():
    pid = request.form.get("pid")
    card_id = request.form.get("card_id")
    answer = (request.form.get("answer") or "")[:40] or None
    upload = request.files.get("file")
    if not upload:
        return jsonify({"error": "No photo received."}), 400
    with lock:
        p, cur, card = current_card_or_400(pid, card_id)
        if card["type"] not in ("photo", "draw", "light"):
            return jsonify({"error": "This card does not take a photo."}), 400
        try:
            img = Image.open(upload.stream)
            img = ImageOps.exif_transpose(img).convert("RGB")
            img.thumbnail((900, 900))
        except Exception:
            return jsonify({"error": "Could not read that image. Try another."}), 400
        photo_id = uuid.uuid4().hex[:10]
        img.save(os.path.join(UPLOADS, photo_id + ".jpg"), "JPEG", quality=82)
        state["photos"].append(
            {
                "id": photo_id,
                "pid": pid,
                "name": p["name"],
                "card_id": card_id,
                "prompt": render_prompt(card, cur),
                "file": photo_id + ".jpg",
                "t": time.time(),
                "status": "hidden" if settings().get("approve_photos") else "visible",
            }
        )
        return jsonify(finish(pid, card, "done", answer=answer, photo_id=photo_id))


@app.post("/api/secret")
def api_secret():
    data = request.get_json(silent=True) or {}
    pid, code = data.get("pid"), (data.get("code") or "").strip().lower()
    with lock:
        p = state["players"].get(pid)
        if not p:
            return jsonify({"error": "Join the game first, then scan again.", "need_join": True}), 404
        cur = p.get("current")
        card = card_by_id(cur["card_id"]) if cur else None
        if card and card["type"] == "secret_qr" and card["code"].lower() == code:
            res = finish(pid, card, "done", note="Found it!")
            return jsonify(res)
        for c in load_cards()["cards"]:
            if c["type"] == "secret_qr" and c["code"].lower() == code:
                if p["seen"].get(c["id"]) == "done":
                    return jsonify({"ok": True, "note": "You already found this one."})
                return jsonify({"ok": True, "note": "Nice find! That's not your current task, though."})
        return jsonify({"error": "Hmm, that code isn't right."}), 400


@app.post("/api/flash/claim")
def api_flash_claim():
    data = request.get_json(silent=True) or {}
    pid, token = data.get("pid"), data.get("token")
    with lock:
        p = state["players"].get(pid)
        if not p:
            return jsonify({"error": "Join the game first, then scan again.", "need_join": True}), 404
        fl = state["flash"]
        if not fl or fl["token"] != token:
            return jsonify({"error": "That QR code has expired."}), 400
        if time.time() > fl["until"]:
            return jsonify({"error": "Too slow! That QR code has expired."}), 400
        if pid in fl["claimed"]:
            return jsonify({"ok": True, "note": "You already grabbed this one."})
        fl["claimed"].append(pid)
        add_event(pid, "Grabbed the flash QR", fl["bonus"])
        save()
        return jsonify({"ok": True, "note": f"+{fl['bonus']} points!"})


@app.post("/api/poll/answer")
def api_poll_answer():
    data = request.get_json(silent=True) or {}
    pid, value = data.get("pid"), str(data.get("value", "")).strip()[:40]
    with lock:
        get_player(pid)  # 404s for an unknown player
        poll = state["poll"]
        if not poll or not poll["open"]:
            return jsonify({"error": "This poll is closed."}), 400
        if not value:
            return jsonify({"error": "Please answer."}), 400
        if poll["kind"] == "choice" and value not in poll["options"]:
            return jsonify({"error": "Pick one of the options."}), 400
        if poll["kind"] == "number":
            try:
                float(value)
            except ValueError:
                return jsonify({"error": "That needs to be a number."}), 400
        first = pid not in poll["answers"]
        poll["answers"][pid] = value
        if first:
            add_event(pid, "Answered a poll", 1)
        save()
        return jsonify({"ok": True})


@app.post("/api/icon/found")
def api_icon_found():
    data = request.get_json(silent=True) or {}
    pid = data.get("pid")
    with lock:
        get_player(pid)
        sync = state["icon_sync"]
        if sync and pid in sync["assign"] and pid not in sync["found"]:
            sync["found"].append(pid)
            add_event(pid, "Found their icon twin", 2)
            save()
        return jsonify({"ok": True})


# --------------------------------------------------------------------------
# TV API
# --------------------------------------------------------------------------
def poll_summary(poll):
    answers = list(poll["answers"].values())
    out = {
        "id": poll["id"],
        "question": poll["question"],
        "kind": poll["kind"],
        "open": poll["open"],
        "count": len(answers),
        "options": poll.get("options", []),
    }
    if poll["kind"] == "choice":
        out["counts"] = {o: answers.count(o) for o in poll["options"]}
    elif poll["kind"] == "number":
        nums = sorted(float(a) for a in answers)
        out["numbers"] = nums[:60]
        if nums:
            out["avg"] = round(sum(nums) / len(nums), 1)
            out["min"], out["max"] = nums[0], nums[-1]
    else:
        counts = {}
        for a in answers:
            counts[a.lower()] = counts.get(a.lower(), 0) + 1
        out["words"] = sorted(counts.items(), key=lambda kv: -kv[1])[:40]
    return out


def category_stats():
    cats = {}
    for h in state["history"]:
        if h["result"] not in ("done", "declined"):
            continue
        c = cats.setdefault(h["category"], {"done": 0, "declined": 0})
        c[h["result"]] += 1
    most = least = None
    if cats:
        most = max(cats.items(), key=lambda kv: (kv[1]["done"], -kv[1]["declined"]))
        rated = {k: v for k, v in cats.items() if v["done"] + v["declined"] >= 2}
        if rated:
            least = max(rated.items(), key=lambda kv: kv[1]["declined"] / (kv[1]["done"] + kv[1]["declined"]))
    return (
        {"name": most[0], "done": most[1]["done"]} if most else None,
        {"name": least[0], "declined": least[1]["declined"], "total": least[1]["done"] + least[1]["declined"]}
        if least
        else None,
    )


@app.get("/api/tv")
def api_tv():
    with lock:
        check_midnight()
        hist = state["history"]
        done = sum(1 for h in hist if h["result"] == "done")
        declined = sum(1 for h in hist if h["result"] == "declined")
        assigned = sum(state["assigned"].values())
        resolved = done + declined
        most, least = category_stats()

        limit = settings().get("tv_photo_limit", 8)
        visible = [ph for ph in state["photos"] if ph["status"] == "visible"]
        photos = [
            {"id": ph["id"], "url": "/photos/" + ph["file"], "name": ph["name"], "prompt": ph["prompt"]}
            for ph in visible[-limit:]
        ]

        players = sorted(state["players"].values(), key=lambda x: -x["points"])
        leaderboard = [{"name": p["name"], "points": p["points"]} for p in players[:8]]

        best_g = None
        for h in hist:
            if h["card_id"] == "gforce" and h["result"] == "done":
                try:
                    g = float(h["answer"])
                except (TypeError, ValueError):
                    continue
                if not best_g or g > best_g["g"]:
                    best_g = {"name": h["name"], "g": g}

        # A closed poll shows its final results for a while, then leaves the
        # TV by itself (the host can still clear it sooner).
        poll = state["poll"]
        if poll and not poll["open"] and time.time() - poll.get("closed_at", 0) > POLL_RESULTS_SECONDS:
            poll = None
        poll = poll_summary(poll) if poll else None

        icon = None
        sync = state["icon_sync"]
        if sync:
            icon = {"found": len(sync["found"]), "total": len(sync["assign"])}

        flash = None
        fl = state["flash"]
        if fl and time.time() < fl["until"]:
            flash = {
                "token": fl["token"],
                "remaining": round(fl["until"] - time.time()),
                "total": fl.get("seconds", 60),
                "bonus": fl["bonus"],
                "claimed": len(fl["claimed"]),
            }

        feed = []
        for h in hist[-10:][::-1]:
            feed.append(
                {"name": h["name"], "result": h["result"], "prompt": h["prompt"], "points": h["points"]}
            )

        return jsonify(
            {
                "players": len(state["players"]),
                "assigned": assigned,
                "completed": done,
                "declined": declined,
                "completed_pct": round(100 * done / resolved) if resolved else 0,
                "declined_pct": round(100 * declined / resolved) if resolved else 0,
                "most_popular": most,
                "least_popular": least,
                "leaderboard": leaderboard,
                "photos": photos,
                "best_g": best_g,
                "poll": poll,
                "icon": icon,
                "flash": flash,
                "feed": feed,
                "finale": bool(state["finale"]),
                "finale_kind": finale_kind(),
                # Every visible photo, for the finale slideshow (only sent then).
                "finale_photos": [{"url": "/photos/" + ph["file"], "name": ph["name"]} for ph in visible]
                if state["finale"]
                else [],
                "photo_total": len(visible),
                "minutes_to_midnight": round(hours_to_midnight() * 60),
                # For the big countdown in the last minute.
                "seconds_to_midnight": round(hours_to_midnight() * 3600, 1),
                "join_url": base_url() + "/play",
            }
        )


# --------------------------------------------------------------------------
# Host API
# --------------------------------------------------------------------------
@app.get("/api/host/data")
def host_data():
    check_host()
    with lock:
        players = sorted(state["players"].items(), key=lambda kv: -kv[1]["points"])
        return jsonify(
            {
                "players": [{"pid": pid, "name": p["name"], "points": p["points"]} for pid, p in players],
                "photos": [
                    {
                        "id": ph["id"],
                        "url": "/photos/" + ph["file"],
                        "name": ph["name"],
                        "prompt": ph["prompt"],
                        "status": ph["status"],
                    }
                    for ph in state["photos"][::-1]
                ],
                "answers": [
                    {"name": h["name"], "prompt": h["prompt"], "answer": h["answer"], "result": h["result"]}
                    for h in state["history"][::-1]
                    if h["result"] == "done" and h["answer"] is not None
                ][:60],
                "poll": poll_summary(state["poll"]) if state["poll"] else None,
                "icon": (
                    {"found": len(state["icon_sync"]["found"]), "total": len(state["icon_sync"]["assign"])}
                    if state["icon_sync"]
                    else None
                ),
                "finale": bool(state["finale"]),
                "finale_kind": finale_kind(),
                "presets": load_cards().get("poll_presets", []),
                "secrets": [
                    {"location": c.get("location", ""), "code": c["code"], "prompt": c["prompt"]}
                    for c in load_cards()["cards"]
                    if c["type"] == "secret_qr"
                ],
                "phase": current_phase(),
                "minutes_to_midnight": round(hours_to_midnight() * 60),
            }
        )


@app.post("/api/host/poll")
def host_poll():
    check_host()
    data = request.get_json(silent=True) or {}
    question = (data.get("question") or "").strip()
    kind = data.get("kind", "text")
    options = [o.strip() for o in data.get("options", []) if o.strip()]
    if not question or kind not in ("choice", "number", "text"):
        return jsonify({"error": "Need a question and a kind."}), 400
    if kind == "choice" and len(options) < 2:
        return jsonify({"error": "Choice polls need at least two options."}), 400
    with lock:
        state["poll"] = {
            "id": uuid.uuid4().hex[:6],
            "question": question,
            "kind": kind,
            "options": options,
            "answers": {},
            "open": True,
        }
        save()
    return jsonify({"ok": True})


@app.post("/api/host/poll/<action>")
def host_poll_action(action):
    check_host()
    if action not in ("close", "clear"):
        return jsonify({"error": "Unknown action."}), 400
    with lock:
        if action == "close" and state["poll"]:
            state["poll"]["open"] = False
            state["poll"]["closed_at"] = time.time()
        elif action == "clear":
            state["poll"] = None
        save()
    return jsonify({"ok": True})


@app.post("/api/host/icons/<action>")
def host_icons(action):
    check_host()
    if action not in ("start", "clear"):
        return jsonify({"error": "Unknown action."}), 400
    with lock:
        if action == "clear":
            state["icon_sync"] = None
            save()
            return jsonify({"ok": True})
        pids = list(state["players"].keys())
        if len(pids) < 2:
            return jsonify({"error": "Need at least two players."}), 400
        random.shuffle(pids)
        groups = [pids[i : i + 2] for i in range(0, len(pids), 2)]
        if len(groups[-1]) == 1:  # odd one out joins the previous pair
            groups[-2].extend(groups.pop())
        icons = random.sample(ICONS, min(len(groups), len(ICONS)))
        assign, group_size = {}, {}
        for i, g in enumerate(groups):
            for pid in g:
                assign[pid] = icons[i % len(icons)]
                group_size[pid] = len(g)
        state["icon_sync"] = {
            "id": uuid.uuid4().hex[:6],
            "assign": assign,
            "group_size": group_size,
            "found": [],
        }
        save()
    return jsonify({"ok": True})


@app.post("/api/host/flash")
def host_flash():
    check_host()
    data = request.get_json(silent=True) or {}
    try:
        seconds = max(10, min(int(data.get("seconds", 60)), 600))
        bonus = max(1, min(int(data.get("bonus", 3)), 20))
    except (TypeError, ValueError):
        return jsonify({"error": "Seconds and points must be numbers."}), 400
    with lock:
        state["flash"] = {
            "token": uuid.uuid4().hex[:8],
            "until": time.time() + seconds,
            "seconds": seconds,
            "bonus": bonus,
            "claimed": [],
        }
        save()
    return jsonify({"ok": True})


@app.post("/api/host/finale")
def host_finale():
    check_host()
    data = request.get_json(silent=True) or {}
    # kind "midnight" = Happy New Year; "thanks" = ending early, same stats
    # but a "Thanks for playing!" celebration instead.
    kind = data.get("kind", "midnight")
    if kind not in ("midnight", "thanks"):
        return jsonify({"error": "Unknown finale kind."}), 400
    with lock:
        state["finale"] = kind if data.get("on") else False
        if not state["finale"]:
            for pid, p in state["players"].items():
                if not p.get("current"):
                    pick_card(pid)
        save()
    return jsonify({"ok": True})


@app.post("/api/host/photo")
def host_photo():
    check_host()
    data = request.get_json(silent=True) or {}
    with lock:
        for ph in state["photos"]:
            if ph["id"] == data.get("id") and data.get("status") in ("visible", "hidden"):
                ph["status"] = data["status"]
        save()
    return jsonify({"ok": True})


@app.post("/api/host/reset")
def host_reset():
    check_host()
    with lock:
        state.clear()
        state.update(fresh_state())
        for name in os.listdir(UPLOADS):
            if name.endswith(".jpg"):
                os.remove(os.path.join(UPLOADS, name))
        save()
    return jsonify({"ok": True})


@app.errorhandler(400)
@app.errorhandler(404)
def json_errors(e):
    if request.path.startswith("/api/"):
        return jsonify({"error": getattr(e, "description", "error")}), e.code
    return e


if __name__ == "__main__":
    load_cards()  # stop right away, with a readable message, if cards.json is broken
    ip = lan_ip()
    print(f"\n  TV:     http://{ip}:{PORT}/tv")
    print(f"  Phones: http://{ip}:{PORT}/play")
    print(f"  Host:   http://{ip}:{PORT}/host?key={HOST_KEY}\n")
    app.run(host="0.0.0.0", port=PORT, threaded=True)
