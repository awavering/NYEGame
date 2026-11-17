"""Shared fixtures.

Every test gets an empty game, a fake clock (5 hours before midnight, so it
is the "early" phase), and a throwaway data folder.
"""

import shutil
from pathlib import Path

import pytest
from helpers import Player, game


@pytest.fixture(autouse=True)
def clean_game(monkeypatch):
    with game.lock:
        game.state.clear()
        game.state.update(game.fresh_state())
    for photo in Path(game.UPLOADS).glob("*.jpg"):
        photo.unlink()
    shutil.rmtree(game.ARCHIVE, ignore_errors=True)
    monkeypatch.setattr(game, "hours_to_midnight", lambda: 5.0)


@pytest.fixture
def client():
    return game.app.test_client()


@pytest.fixture
def host():
    """Headers that identify the request as coming from the host page."""
    return {"X-Host-Key": game.HOST_KEY}


@pytest.fixture
def make_player(client):
    """make_player("Ben") joins a new guest."""
    return lambda name="Ann": Player(client, name)
