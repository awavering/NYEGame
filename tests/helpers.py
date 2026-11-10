"""Small helpers shared by the tests."""

import io
import os
import sys
import tempfile
import time
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
# The app reads these when it is imported, so set them first. Tests then run
# against a throwaway data folder and never touch a real game's state.json.
os.environ.setdefault("GAME_DATA_DIR", tempfile.mkdtemp(prefix="nye-test-"))
os.environ.pop("MIDNIGHT_ISO", None)

import app as game  # noqa: E402


def jpeg_bytes(size=(1600, 1200), colour=(200, 50, 50)):
    buf = io.BytesIO()
    Image.new("RGB", size, colour).save(buf, "JPEG")
    return buf.getvalue()


class Player:
    """A joined guest, with shortcuts for the calls a phone makes."""

    def __init__(self, client, name="Ann"):
        self.client = client
        self.name = name
        self.pid = client.post("/api/join", json={"name": name}).get_json()["pid"]

    def me(self):
        return self.client.get(f"/api/me?pid={self.pid}").get_json()

    @property
    def points(self):
        return self.me()["points"]

    def give(self, card_id):
        """Force a particular card. (The real deal is random.)"""
        with game.lock:
            game.state["players"][self.pid]["current"] = {
                "card_id": card_id,
                "assigned_at": time.time(),
                "started_at": None,
                "extra": {"thing": "a test thing"},
            }

    def respond(self, card_id, action="done", answer=None):
        return self.client.post(
            "/api/card/respond",
            json={"pid": self.pid, "card_id": card_id, "action": action, "answer": answer},
        )

    def upload(self, card_id, data=None, answer=None):
        form = {"pid": self.pid, "card_id": card_id, "file": (io.BytesIO(data or jpeg_bytes()), "x.jpg")}
        if answer:
            form["answer"] = answer
        return self.client.post("/api/card/photo", data=form, content_type="multipart/form-data")

    def poll_answer(self, value):
        return self.client.post("/api/poll/answer", json={"pid": self.pid, "value": value})
