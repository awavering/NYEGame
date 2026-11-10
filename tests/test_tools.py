"""The helper scripts in tools/."""

import sys

from tools import simulate_guests


def test_simulator_fails_loudly_when_it_cannot_reach_the_server(monkeypatch, capsys):
    # Port 9 has nothing listening, like pointing the bots at the wrong port.
    args = ["simulate_guests", "--url", "http://127.0.0.1:9", "--players", "2", "--minutes", "0"]
    monkeypatch.setattr(sys, "argv", args)
    monkeypatch.setattr(simulate_guests, "failures", [])
    assert simulate_guests.main() == 1
    assert "could not join" in capsys.readouterr().out
