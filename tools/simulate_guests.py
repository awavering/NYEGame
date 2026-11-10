#!/usr/bin/env python3
"""Fake guests, so you can rehearse the TV screen without 15 friends.

    python tools/simulate_guests.py --players 10 --minutes 5

Start the server first (make run). The bots join, answer cards, upload
little generated pictures, answer polls and find their icon twin, so the
TV scoreboard, bouncing photos and host moments all have something to show.
Uses only the standard library plus Pillow (already a dependency).
"""

import argparse
import io
import json
import random
import threading
import time
import urllib.error
import urllib.request
import uuid

from PIL import Image, ImageDraw

NAMES = ["Ava", "Ben", "Cleo", "Dev", "Eli", "Fay", "Gus", "Hana", "Ivo", "Jun",
         "Kai", "Lena", "Moe", "Nia", "Otis", "Pia", "Quin", "Rex", "Sol", "Tess"]  # fmt: skip
WORDS = ["pizza", "blue", "Maria", "nachos", "green", "Sam", "cider", "red", "Jo"]


def call(base, path, body=None, raw=None, content_type=None):
    """One HTTP call; returns the parsed JSON (with '_status' added on errors)."""
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    headers = {"Content-Type": content_type or "application/json"} if data is not None else {}
    req = urllib.request.Request(base + path, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as err:
        return {**json.loads(err.read() or b"{}"), "_status": err.code}


def fake_photo(label):
    """A bright random-colour picture with some text, as JPEG bytes."""
    colour = tuple(random.randint(40, 230) for _ in range(3))
    img = Image.new("RGB", (640, 480), colour)
    ImageDraw.Draw(img).text((20, 20), label, fill="white")
    buf = io.BytesIO()
    img.save(buf, "JPEG")
    return buf.getvalue()


def upload(base, pid, card, label):
    boundary = "----sim" + uuid.uuid4().hex
    parts = []
    for key, value in {"pid": pid, "card_id": card["id"], "answer": "50% bright"}.items():
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode()
        )
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="p.jpg"\r\n'
        "Content-Type: image/jpeg\r\n\r\n".encode()
        + fake_photo(label)
        + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode())
    return call(
        base, "/api/card/photo", raw=b"".join(parts), content_type=f"multipart/form-data; boundary={boundary}"
    )


def play_card(base, pid, name, card, decline_rate):
    cid, ctype = card["id"], card["type"]
    if ctype in ("secret_qr", "timer") or random.random() < decline_rate:
        return call(base, "/api/card/respond", {"pid": pid, "card_id": cid, "action": "decline"})
    if ctype in ("photo", "draw", "light"):
        return upload(base, pid, card, f"{name}: {card['prompt'][:40]}")
    answer = None
    if ctype == "text":
        answer = random.choice(WORDS)
    elif ctype == "number":
        answer = str(random.randint(5, 300))
    elif ctype == "gforce":
        answer = f"{random.uniform(1.0, 6.0):.2f}"
    return call(base, "/api/card/respond", {"pid": pid, "card_id": cid, "action": "done", "answer": answer})


def poll_answer(poll):
    if poll["kind"] == "choice":
        return random.choice(poll["options"])
    if poll["kind"] == "number":
        return str(random.randint(5, 30))
    return random.choice(WORDS)


def bot(base, name, stop_at, pace, decline_rate):
    pid = call(base, "/api/join", {"name": name})["pid"]
    while time.time() < stop_at:
        time.sleep(random.uniform(0.5, 1.5) * pace)
        me = call(base, f"/api/me?pid={pid}")
        poll, icon = me.get("poll"), me.get("icon")
        if poll and not poll["answered"]:
            call(base, "/api/poll/answer", {"pid": pid, "value": poll_answer(poll)})
        if icon and not icon["found"]:
            call(base, "/api/icon/found", {"pid": pid})
        if me.get("card"):
            result = play_card(base, pid, name, me["card"], decline_rate)
            if "error" in result:
                print(f"  {name}: {result['error']}")


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--url", default="http://localhost:5000")
    parser.add_argument("--players", type=int, default=8)
    parser.add_argument("--minutes", type=float, default=3, help="how long to keep playing")
    parser.add_argument("--pace", type=float, default=3, help="average seconds between actions")
    parser.add_argument("--decline", type=float, default=0.2, help="chance (0-1) of skipping a card")
    args = parser.parse_args()

    stop_at = time.time() + args.minutes * 60
    names = (NAMES * 2)[: args.players]
    threads = [
        threading.Thread(target=bot, args=(args.url, n, stop_at, args.pace, args.decline), daemon=True)
        for n in names
    ]
    print(f"{len(threads)} fake guests playing for {args.minutes} minutes at {args.url} (Ctrl-C to stop)")
    for t in threads:
        t.start()
    try:
        for t in threads:
            t.join()
    except KeyboardInterrupt:
        print("stopped")


if __name__ == "__main__":
    main()
