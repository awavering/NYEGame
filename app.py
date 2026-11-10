"""
NYE party game server.

Run it:   python app.py
TV:       http://<your-ip>:5000/tv
Phones:   scan the QR code on the TV (goes to /play)
Host:     http://<your-ip>:5000/host?key=host     (change the key with HOST_KEY)

Everything that guests see is driven by cards.json. State is saved to
state.json after every change, so a restart does not lose the game.
"""
import io
import json
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

BASE = os.path.dirname(os.path.abspath(__file__))
PAGES = os.path.join(BASE, "pages")
UPLOADS = os.path.join(BASE, "uploads")
STATE_FILE = os.path.join(BASE, "state.json")
CARDS_FILE = os.path.join(BASE, "cards.json")
os.makedirs(UPLOADS, exist_ok=True)

HOST_KEY = os.environ.get("HOST_KEY", "host")
PORT = int(os.environ.get("PORT", "5000"))
TZ = ZoneInfo(os.environ.get("TZ_NAME", "America/Denver"))
# For testing you can fake the countdown, e.g. MIDNIGHT_ISO=2026-10-16T00:00:00
MIDNIGHT_ISO = os.environ.get("MIDNIGHT_ISO")

ICONS = ["🦄", "🍕", "🐙", "🚀", "🎸", "🌵", "🍩", "🦖", "🎈", "🐢",
         "🍔", "🦊", "🎯", "🐼", "🍉", "👾", "🎩", "🐝", "🍿", "🦋"]

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
lock = threading.RLock()


# --------------------------------------------------------------------------
# Cards (re-read from disk whenever cards.json changes, so you can edit live)
# --------------------------------------------------------------------------
_cards_cache = {"mtime": 0, "data": None}


def load_cards():
    mtime = os.path.getmtime(CARDS_FILE)
    if mtime != _cards_cache["mtime"]:
        with open(CARDS_FILE, encoding="utf-8") as f:
            _cards_cache["data"] = json.load(f)
        _cards_cache["mtime"] = mtime
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
        "players": {},      # pid -> {name, points, seen, current, joined}
        "history": [],      # every completed / declined / bonus event
        "assigned": {},     # card_id -> times handed out
        "photos": [],       # uploaded pictures
        "poll": None,
        "icon_sync": None,
        "flash": None,
        "finale": False,
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
    now = datetime.now(TZ)
    if MIDNIGHT_ISO:
        target = datetime.fromisoformat(MIDNIGHT_ISO).replace(tzinfo=TZ)
    elif now.month == 1 and now.day == 1:
        target = datetime(now.year, 1, 1, tzinfo=TZ)
    else:
        target = datetime(now.year + 1, 1, 1, tzinfo=TZ)
    return (target - now).total_seconds() / 3600


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
def pick_card(pid):
    p = state["players"][pid]
    pool = load_cards()["cards"]
    unseen = [c for c in pool if c["id"] not in p["seen"]]
    if not unseen:           # they have done everything: start over
        p["seen"] = {}
        unseen = pool
    phase = current_phase()
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
    p["current"] = {"card_id": card["id"], "assigned_at": time.time(),
                    "started_at": None, "extra": extra}
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
    state["history"].append({
        "t": time.time(), "pid": pid, "name": p["name"], "card_id": card["id"],
        "category": card["category"], "prompt": render_prompt(card, cur),
        "result": result, "answer": answer, "photo": photo_id, "points": points,
    })
    p["current"] = None
    if not state["finale"]:
        pick_card(pid)
    save()
    return {"ok": True, "points_earned": points, "note": note}


def add_event(pid, text, points):
    p = state["players"][pid]
    p["points"] += points
    state["history"].append({
        "t": time.time(), "pid": pid, "name": p["name"], "card_id": None,
        "category": "Bonus", "prompt": text, "result": "bonus",
        "answer": None, "photo": None, "points": points,
    })


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


def qr_response(url):
    img = qrcode.make(url, box_size=12, border=2)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    buf.seek(0)
    resp = send_file(buf, mimetype="image/png")
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.get("/qr/join.png")
def qr_join():
    return qr_response(base_url() + "/play")


@app.get("/qr/secret/<code>.png")
def qr_secret(code):
    return qr_response(f"{base_url()}/s/{code}")


@app.get("/qr/flash/<token>.png")
def qr_flash(token):
    return qr_response(f"{base_url()}/f/{token}")


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
        state["players"][pid] = {"name": name, "points": 0, "seen": {},
                                 "current": None, "joined": time.time()}
        pick_card(pid)
        save()
    return jsonify({"pid": pid})


def poll_view_for(pid):
    poll = state["poll"]
    if not poll or not poll["open"]:
        return None
    return {"id": poll["id"], "question": poll["question"], "kind": poll["kind"],
            "options": poll.get("options", []), "answered": pid in poll["answers"]}


def icon_view_for(pid):
    sync = state["icon_sync"]
    if not sync or pid not in sync["assign"]:
        return None
    return {"id": sync["id"], "emoji": sync["assign"][pid],
            "found": pid in sync["found"], "group": sync["group_size"][pid]}


@app.get("/api/me")
def api_me():
    pid = request.args.get("pid", "")
    with lock:
        p = state["players"].get(pid)
        if not p:
            return jsonify({"error": "unknown_player"}), 404
        ensure_current(p, pid)
        return jsonify({
            "name": p["name"], "points": p["points"],
            "card": None if state["finale"] else public_card(p),
            "poll": poll_view_for(pid), "icon": icon_view_for(pid),
            "finale": state["finale"], "now": time.time(),
        })


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
        state["photos"].append({
            "id": photo_id, "pid": pid, "name": p["name"], "card_id": card_id,
            "prompt": render_prompt(card, cur), "file": photo_id + ".jpg",
            "t": time.time(),
            "status": "hidden" if settings().get("approve_photos") else "visible",
        })
        return jsonify(finish(pid, card, "done", answer=answer, photo_id=photo_id))


@app.post("/api/secret")
def api_secret():
    data = request.get_json(silent=True) or {}
    pid, code = data.get("pid"), (data.get("code") or "").strip().lower()
    with lock:
        p = state["players"].get(pid)
        if not p:
            return jsonify({"error": "Join the game first, then scan again.",
                            "need_join": True}), 404
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
            return jsonify({"error": "Join the game first, then scan again.",
                            "need_join": True}), 404
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
        p = get_player(pid)
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
    out = {"id": poll["id"], "question": poll["question"], "kind": poll["kind"],
           "open": poll["open"], "count": len(answers), "options": poll.get("options", [])}
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
            least = max(rated.items(),
                        key=lambda kv: kv[1]["declined"] / (kv[1]["done"] + kv[1]["declined"]))
    return (
        {"name": most[0], "done": most[1]["done"]} if most else None,
        {"name": least[0], "declined": least[1]["declined"],
         "total": least[1]["done"] + least[1]["declined"]} if least else None,
    )


@app.get("/api/tv")
def api_tv():
    with lock:
        hist = state["history"]
        done = sum(1 for h in hist if h["result"] == "done")
        declined = sum(1 for h in hist if h["result"] == "declined")
        assigned = sum(state["assigned"].values())
        resolved = done + declined
        most, least = category_stats()

        limit = settings().get("tv_photo_limit", 14)
        visible = [ph for ph in state["photos"] if ph["status"] == "visible"]
        photos = [{"id": ph["id"], "url": "/photos/" + ph["file"],
                   "name": ph["name"], "prompt": ph["prompt"]}
                  for ph in visible[-limit:]]

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

        poll = poll_summary(state["poll"]) if state["poll"] else None

        icon = None
        sync = state["icon_sync"]
        if sync:
            icon = {"found": len(sync["found"]), "total": len(sync["assign"])}

        flash = None
        fl = state["flash"]
        if fl and time.time() < fl["until"]:
            flash = {"token": fl["token"], "remaining": round(fl["until"] - time.time()),
                     "bonus": fl["bonus"], "claimed": len(fl["claimed"])}

        feed = []
        for h in hist[-6:][::-1]:
            feed.append({"name": h["name"], "result": h["result"],
                         "prompt": h["prompt"], "points": h["points"]})

        return jsonify({
            "players": len(state["players"]),
            "assigned": assigned, "completed": done, "declined": declined,
            "completed_pct": round(100 * done / resolved) if resolved else 0,
            "declined_pct": round(100 * declined / resolved) if resolved else 0,
            "most_popular": most, "least_popular": least,
            "leaderboard": leaderboard, "photos": photos, "best_g": best_g,
            "poll": poll, "icon": icon, "flash": flash, "feed": feed,
            "finale": state["finale"], "photo_total": len(visible),
            "minutes_to_midnight": round(hours_to_midnight() * 60),
            "join_url": base_url() + "/play",
        })


# --------------------------------------------------------------------------
# Host API
# --------------------------------------------------------------------------
@app.get("/api/host/data")
def host_data():
    check_host()
    with lock:
        players = sorted(state["players"].items(), key=lambda kv: -kv[1]["points"])
        return jsonify({
            "players": [{"pid": pid, "name": p["name"], "points": p["points"]} for pid, p in players],
            "photos": [{"id": ph["id"], "url": "/photos/" + ph["file"], "name": ph["name"],
                        "prompt": ph["prompt"], "status": ph["status"]}
                       for ph in state["photos"][::-1]],
            "answers": [{"name": h["name"], "prompt": h["prompt"], "answer": h["answer"],
                         "result": h["result"]}
                        for h in state["history"][::-1]
                        if h["result"] == "done" and h["answer"] is not None][:60],
            "poll": poll_summary(state["poll"]) if state["poll"] else None,
            "icon": ({"found": len(state["icon_sync"]["found"]),
                      "total": len(state["icon_sync"]["assign"])}
                     if state["icon_sync"] else None),
            "finale": state["finale"],
            "presets": load_cards().get("poll_presets", []),
            "secrets": [{"location": c.get("location", ""), "code": c["code"], "prompt": c["prompt"]}
                        for c in load_cards()["cards"] if c["type"] == "secret_qr"],
            "phase": current_phase(),
            "minutes_to_midnight": round(hours_to_midnight() * 60),
        })


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
        state["poll"] = {"id": uuid.uuid4().hex[:6], "question": question, "kind": kind,
                         "options": options, "answers": {}, "open": True}
        save()
    return jsonify({"ok": True})


@app.post("/api/host/poll/<action>")
def host_poll_action(action):
    check_host()
    with lock:
        if action == "close" and state["poll"]:
            state["poll"]["open"] = False
        elif action == "clear":
            state["poll"] = None
        save()
    return jsonify({"ok": True})


@app.post("/api/host/icons/<action>")
def host_icons(action):
    check_host()
    with lock:
        if action == "clear":
            state["icon_sync"] = None
            save()
            return jsonify({"ok": True})
        pids = list(state["players"].keys())
        if len(pids) < 2:
            return jsonify({"error": "Need at least two players."}), 400
        random.shuffle(pids)
        groups = [pids[i:i + 2] for i in range(0, len(pids), 2)]
        if len(groups[-1]) == 1:        # odd one out joins the previous pair
            groups[-2].extend(groups.pop())
        icons = random.sample(ICONS, min(len(groups), len(ICONS)))
        assign, group_size = {}, {}
        for i, g in enumerate(groups):
            for pid in g:
                assign[pid] = icons[i % len(icons)]
                group_size[pid] = len(g)
        state["icon_sync"] = {"id": uuid.uuid4().hex[:6], "assign": assign,
                              "group_size": group_size, "found": []}
        save()
    return jsonify({"ok": True})


@app.post("/api/host/flash")
def host_flash():
    check_host()
    data = request.get_json(silent=True) or {}
    seconds = max(10, min(int(data.get("seconds", 60)), 600))
    bonus = max(1, min(int(data.get("bonus", 3)), 20))
    with lock:
        state["flash"] = {"token": uuid.uuid4().hex[:8], "until": time.time() + seconds,
                          "bonus": bonus, "claimed": []}
        save()
    return jsonify({"ok": True})


@app.post("/api/host/finale")
def host_finale():
    check_host()
    data = request.get_json(silent=True) or {}
    with lock:
        state["finale"] = bool(data.get("on"))
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
    ip = lan_ip()
    print("\n  TV:     http://%s:%d/tv" % (ip, PORT))
    print("  Phones: http://%s:%d/play" % (ip, PORT))
    print("  Host:   http://%s:%d/host?key=%s\n" % (ip, PORT, HOST_KEY))
    app.run(host="0.0.0.0", port=PORT, threaded=True)
