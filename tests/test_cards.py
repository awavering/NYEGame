"""cards.json: the shipped file is valid, the checker catches mistakes, and
the server survives a bad edit while the game is running."""

import copy
import json
import os
import time
from pathlib import Path

import pytest
from helpers import ROOT, game

from tools.check_cards import validate

SHIPPED = json.loads((ROOT / "cards.json").read_text(encoding="utf-8"))


def test_shipped_cards_are_valid():
    errors, warnings = validate(SHIPPED)
    assert errors == []


def test_there_are_enough_cards_for_a_party():
    assert len(SHIPPED["cards"]) >= 20


def edited(mutate):
    data = copy.deepcopy(SHIPPED)
    mutate(data)
    return validate(data)[0]


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (lambda d: d["cards"].append(dict(d["cards"][0])), "already used"),
        (lambda d: d["cards"][0].update(type="teleport"), "unknown type"),
        (lambda d: d["cards"][0].pop("prompt"), '"prompt" is required'),
        (lambda d: d["cards"][0].update(points=-1), '"points" must be a number'),
        (lambda d: d["cards"][0].update(weights={"noon": 1}), "unknown phase"),
        (lambda d: d["cards"][0].update(weights={"early": "lots"}), "must be a number"),
        (lambda d: d["cards"][0].update(type="timer", minutes=0), "minutes"),
        (lambda d: d["cards"][0].update(camera="rear"), "camera must be"),
        (lambda d: d["cards"][0].update(grade="vibes"), '"grade" only works'),
        (lambda d: d.update(cards=[]), "non-empty list"),
        (
            lambda d: d["poll_presets"].append({"question": "Q", "kind": "choice", "options": ["a"]}),
            "two options",
        ),
        (lambda d: d["poll_presets"].append({"question": "Q", "kind": "dance"}), "kind must be"),
    ],
)
def test_checker_catches_mistakes(mutate, expected):
    assert any(expected in e for e in edited(mutate))


def test_checker_catches_secret_code_problems():
    def no_code(d):
        next(c for c in d["cards"] if c["type"] == "secret_qr").pop("code")

    def duplicate(d):
        secrets = [c for c in d["cards"] if c["type"] == "secret_qr"]
        secrets[1]["code"] = secrets[0]["code"].upper()

    def bad_chars(d):
        next(c for c in d["cards"] if c["type"] == "secret_qr")["code"] = "has space/slash"

    assert any("needs a" in e for e in edited(no_code))
    assert any("duplicates" in e for e in edited(duplicate))
    assert any("needs a" in e for e in edited(bad_chars))


def test_thing_placeholder_needs_a_draw_card_and_draw_things():
    def wrong_type(d):
        d["cards"][0]["prompt"] = "Draw {thing}"

    def no_things(d):
        d["draw_things"] = []

    assert any("{thing}" in e for e in edited(wrong_type))
    assert any("{thing}" in e for e in edited(no_things))


def test_checker_warns_about_the_placeholder_payment_handle():
    data = copy.deepcopy(SHIPPED)
    data["settings"]["albert_handle"] = "@someone (edit me)"
    assert any("edit me" in w for w in validate(data)[1])


def test_garbage_input_does_not_crash_the_checker():
    for junk in (None, [], "text", {"cards": "nope"}, {"cards": [1, None, "x"]}):
        errors, _ = validate(junk)
        assert errors


# ------------------------------------------------ live reloading survives bad edits


@pytest.fixture
def live_cards(tmp_path, monkeypatch):
    """A private copy of cards.json that the server is pointed at."""
    path = tmp_path / "cards.json"
    path.write_text(json.dumps(SHIPPED))
    monkeypatch.setattr(game, "CARDS_FILE", str(path))
    monkeypatch.setattr(game, "_cards_cache", {"mtime": 0, "bad_mtime": None, "data": None, "errors": []})

    def save(text):
        path.write_text(text)
        later = time.time() + save.tick
        save.tick += 5  # filesystems can have coarse timestamps; force a visible change
        os.utime(path, (later, later))

    save.tick = 5
    return save


def test_a_broken_edit_keeps_the_last_good_cards(live_cards, capsys):
    good = game.load_cards()
    live_cards("{ this is not json")
    assert game.load_cards() is good
    assert "could not read cards.json" in capsys.readouterr().out

    broken = copy.deepcopy(SHIPPED)
    broken["cards"][0]["type"] = "teleport"
    live_cards(json.dumps(broken))
    assert game.load_cards() is good


def test_a_bad_edit_is_only_reported_once(live_cards, capsys):
    game.load_cards()
    live_cards("{ nope")
    game.load_cards()
    capsys.readouterr()
    game.load_cards()
    game.load_cards()
    assert capsys.readouterr().out == ""


def test_a_fixed_edit_is_picked_up(live_cards):
    game.load_cards()
    live_cards("{ nope")
    game.load_cards()
    updated = copy.deepcopy(SHIPPED)
    updated["cards"].append({"id": "extra", "category": "Test", "type": "confirm", "prompt": "New one"})
    live_cards(json.dumps(updated))
    assert any(c["id"] == "extra" for c in game.load_cards()["cards"])


def test_a_broken_file_at_startup_raises_a_readable_error(live_cards):
    live_cards("{ nope")
    with pytest.raises(RuntimeError, match="cards.json has problems"):
        game.load_cards()


def test_removing_a_card_while_a_guest_holds_it_deals_a_new_one(live_cards, make_player):
    ann = make_player()
    ann.give("cheers")
    smaller = copy.deepcopy(SHIPPED)
    smaller["cards"] = [c for c in smaller["cards"] if c["id"] != "cheers"]
    live_cards(json.dumps(smaller))
    assert ann.me()["card"]["id"] != "cheers"


def test_cards_file_path_exists():
    assert Path(game.CARDS_FILE).exists()
