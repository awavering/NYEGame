"""The game rules, exercised through the real HTTP API."""

import json
import random
from collections import Counter
from pathlib import Path

import pytest
from helpers import game
from PIL import Image

# ---------------------------------------------------------------- pages / joining


@pytest.mark.parametrize(
    "path", ["/", "/play", "/tv", "/s/plum42", "/f/abc", "/qr/join.png", "/qr/secret/plum42.png"]
)
def test_public_pages_load(client, path):
    assert client.get(path).status_code == 200


def test_host_pages_need_the_key(client):
    for path in ("/host", "/host/print", "/api/host/data"):
        assert client.get(path).status_code == 403
        assert client.get(f"{path}?key={game.HOST_KEY}").status_code == 200


def test_join_gives_a_card_and_rejects_blank_names(client, make_player):
    assert client.post("/api/join", json={"name": "  "}).status_code == 400
    ann = make_player("Ann")
    assert ann.me()["card"]["prompt"]
    assert ann.me()["name"] == "Ann"


def test_unknown_player_gets_404(client):
    assert client.get("/api/me?pid=nope").status_code == 404


# ---------------------------------------------------------------- answering cards


def test_confirm_card_awards_its_points(make_player):
    ann = make_player()
    ann.give("get_drink")
    assert ann.respond("get_drink").get_json()["points_earned"] == 1
    assert ann.points == 1


def test_text_card_needs_an_answer(make_player):
    ann = make_player()
    ann.give("intro")
    assert ann.respond("intro", answer="  ").status_code == 400
    assert ann.respond("intro", answer="Maria").get_json()["ok"]


def test_number_card_rejects_words(make_player):
    ann = make_player()
    ann.give("t_watchers")
    assert ann.respond("t_watchers", answer="lots").status_code == 400


def test_decline_scores_nothing_and_deals_a_new_card(make_player):
    ann = make_player()
    ann.give("cheers")
    assert ann.respond("cheers", "decline").get_json()["points_earned"] == 0
    assert ann.me()["card"]["id"] != "cheers"
    assert game.state["history"][-1]["result"] == "declined"


def test_answering_a_stale_card_is_rejected(make_player):
    ann = make_player()
    ann.give("cheers")
    ann.respond("cheers", "decline")
    assert ann.respond("cheers").status_code == 400


def test_photo_cards_cannot_be_answered_without_a_photo(make_player):
    ann = make_player()
    ann.give("selfie")
    assert ann.respond("selfie").status_code == 400


def test_midnight_guess_is_graded(make_player):
    ann = make_player()
    ann.give("t_midnight")  # fake clock says 300 minutes to go
    close = ann.respond("t_midnight", answer="295").get_json()
    assert close["points_earned"] == 3  # 1 + bonus 2
    ann.give("t_midnight")
    far = ann.respond("t_midnight", answer="100").get_json()
    assert far["points_earned"] == 1
    assert "off by 200" in far["note"]


def test_scoreboard_guess_is_graded(make_player):
    ann = make_player()
    ann.give("t_scoreboard")  # nothing completed yet, so the board says 0
    result = ann.respond("t_scoreboard", answer="1").get_json()
    assert result["points_earned"] == 2
    assert "scoreboard says 0" in result["note"]


def test_gforce_reading_is_recorded_and_shown(client, make_player):
    ann = make_player()
    ann.give("gforce")
    ann.respond("gforce", answer="3.42")
    assert client.get("/api/tv").get_json()["best_g"] == {"name": "Ann", "g": 3.42}


# ---------------------------------------------------------------- photos


def test_photo_upload_completes_the_card_and_is_resized(client, make_player):
    ann = make_player()
    ann.give("selfie")
    assert ann.upload("selfie").get_json()["points_earned"] == 2
    files = list(Path(game.UPLOADS).glob("*.jpg"))
    assert len(files) == 1
    assert max(Image.open(files[0]).size) <= 900
    assert len(client.get("/api/tv").get_json()["photos"]) == 1


def test_junk_upload_is_rejected(make_player):
    ann = make_player()
    ann.give("crispy")
    assert ann.upload("crispy", data=b"not an image").status_code == 400
    assert ann.me()["card"]["id"] == "crispy"  # still theirs to complete


def test_draw_prompt_names_the_thing_to_draw(make_player):
    ann = make_player()
    ann.give("draw")
    assert "a test thing" in ann.me()["card"]["prompt"]


def test_host_can_hide_a_photo_from_the_tv(client, make_player, host):
    ann = make_player()
    ann.give("selfie")
    ann.upload("selfie")
    photo = client.get("/api/host/data", headers=host).get_json()["photos"][0]
    client.post("/api/host/photo", json={"id": photo["id"], "status": "hidden"}, headers=host)
    assert client.get("/api/tv").get_json()["photos"] == []


def test_approve_photos_setting_hides_new_photos_until_approved(client, make_player, host, monkeypatch):
    monkeypatch.setattr(game, "settings", lambda: {"approve_photos": True})
    ann = make_player()
    ann.give("selfie")
    ann.upload("selfie")
    assert client.get("/api/tv").get_json()["photos"] == []
    photo = client.get("/api/host/data", headers=host).get_json()["photos"][0]
    client.post("/api/host/photo", json={"id": photo["id"], "status": "visible"}, headers=host)
    assert len(client.get("/api/tv").get_json()["photos"]) == 1


def test_a_card_can_carry_a_picture(client, make_player):
    ann = make_player()
    ann.give("crispy")
    assert ann.me()["card"]["image"] == "/assets/dog.png"
    ann.give("selfie")
    assert "image" not in ann.me()["card"]
    served = client.get("/assets/dog.png")
    assert served.status_code == 200 and served.mimetype == "image/png"


def test_assets_cannot_reach_outside_their_folder(client):
    assert client.get("/assets/nothing.png").status_code == 404
    assert client.get("/assets/..%2Fapp.py").status_code == 404
    assert client.get("/assets/..%2F..%2Fcards.json").status_code == 404


def test_the_tv_is_told_which_mascot_to_show(client):
    assert client.get("/api/config").get_json()["mascot"] == "/assets/dog.png"


def test_no_mascot_when_not_configured(client, monkeypatch):
    monkeypatch.setattr(game, "settings", lambda: {"party_name": "X"})
    assert client.get("/api/config").get_json()["mascot"] is None


# ---------------------------------------------------------------- timer and secret QR


def test_timer_card(client, make_player):
    ann = make_player()
    ann.give("no_phone")
    assert ann.respond("no_phone").status_code == 400  # not started
    client.post("/api/card/start", json={"pid": ann.pid, "card_id": "no_phone"})
    assert ann.respond("no_phone").status_code == 400  # still running
    card = ann.me()["card"]
    assert card["ready_at"] - card["started_at"] == 15 * 60
    game.state["players"][ann.pid]["current"]["started_at"] -= 15 * 60 + 1  # fast-forward
    assert ann.respond("no_phone").get_json()["points_earned"] == 3


def test_secret_qr(client, make_player):
    ann = make_player()
    ann.give("qr_fridge")
    assert client.post("/api/secret", json={"pid": ann.pid, "code": "wrong"}).status_code == 400
    other = client.post("/api/secret", json={"pid": ann.pid, "code": "kiwi17"}).get_json()
    assert other["note"].startswith("Nice find")  # a real code, but not their task
    assert ann.points == 0
    found = client.post("/api/secret", json={"pid": ann.pid, "code": "PLUM42"}).get_json()
    assert found["points_earned"] == 2  # codes are not case sensitive


def test_secret_qr_before_joining_asks_to_join(client):
    resp = client.post("/api/secret", json={"pid": "nobody", "code": "plum42"})
    assert resp.status_code == 404 and resp.get_json()["need_join"]


# ---------------------------------------------------------------- polls


def test_number_poll(client, make_player, host):
    players = [make_player(n) for n in "ABCD"]
    client.post("/api/host/poll", json={"question": "How many?", "kind": "number"}, headers=host)
    assert players[0].me()["poll"]["question"] == "How many?"
    assert players[0].poll_answer("lots").status_code == 400
    for i, p in enumerate(players):
        p.poll_answer(str(10 + i))
    poll = client.get("/api/tv").get_json()["poll"]
    assert poll["count"] == 4 and poll["avg"] == 11.5
    assert players[0].points == 1  # one point for answering


def test_answering_a_poll_twice_only_scores_once(client, make_player, host):
    ann = make_player()
    client.post("/api/host/poll", json={"question": "Q", "kind": "text"}, headers=host)
    ann.poll_answer("one")
    ann.poll_answer("two")
    assert ann.points == 1
    assert client.get("/api/tv").get_json()["poll"]["count"] == 1


def test_text_poll_groups_answers_ignoring_case(client, make_player, host):
    a, b = make_player("A"), make_player("B")
    client.post("/api/host/poll", json={"question": "Drink?", "kind": "text"}, headers=host)
    a.poll_answer("Beer")
    b.poll_answer("beer")
    assert client.get("/api/tv").get_json()["poll"]["words"][0] == ["beer", 2]


def test_choice_poll_validates_options(client, make_player, host):
    ann = make_player()
    too_few = client.post(
        "/api/host/poll", json={"question": "Q", "kind": "choice", "options": ["a"]}, headers=host
    )
    assert too_few.status_code == 400
    client.post(
        "/api/host/poll", json={"question": "Q", "kind": "choice", "options": ["a", "b"]}, headers=host
    )
    assert ann.poll_answer("c").status_code == 400
    assert ann.poll_answer("a").status_code == 200
    assert client.get("/api/tv").get_json()["poll"]["counts"] == {"a": 1, "b": 0}


def test_closing_and_clearing_a_poll(client, make_player, host):
    ann = make_player()
    client.post("/api/host/poll", json={"question": "Q", "kind": "text"}, headers=host)
    client.post("/api/host/poll/close", headers=host)
    assert ann.me()["poll"] is None
    assert client.get("/api/tv").get_json()["poll"]["open"] is False
    assert ann.poll_answer("late").status_code == 400
    client.post("/api/host/poll/clear", headers=host)
    assert client.get("/api/tv").get_json()["poll"] is None


def test_unknown_poll_action_is_rejected(client, host):
    assert client.post("/api/host/poll/explode", headers=host).status_code == 400


# ---------------------------------------------------------------- icon match and flash QR


def test_icon_match_pairs_players(client, make_player, host):
    players = [make_player(n) for n in "ABCD"]
    client.post("/api/host/icons/start", headers=host)
    icons = [p.me()["icon"] for p in players]
    assert all(i["emoji"] for i in icons)
    assert [i["group"] for i in icons] == [2, 2, 2, 2]
    assert len({i["emoji"] for i in icons}) == 2  # two pairs, two emoji
    client.post("/api/icon/found", json={"pid": players[0].pid})
    client.post("/api/icon/found", json={"pid": players[0].pid})  # twice does not double count
    assert client.get("/api/tv").get_json()["icon"] == {"found": 1, "total": 4}
    assert players[0].points == 2


def test_icon_match_with_an_odd_player_makes_a_group_of_three(client, make_player, host):
    players = [make_player(n) for n in "ABC"]
    client.post("/api/host/icons/start", headers=host)
    assert {p.me()["icon"]["group"] for p in players} == {3}


def test_icon_match_needs_two_players(client, make_player, host):
    make_player()
    assert client.post("/api/host/icons/start", headers=host).status_code == 400
    assert client.post("/api/host/icons/nonsense", headers=host).status_code == 400


def test_flash_qr(client, make_player, host):
    ann = make_player()
    assert client.post("/api/host/flash", json={"seconds": "soon"}, headers=host).status_code == 400
    client.post("/api/host/flash", json={"seconds": 120, "bonus": 4}, headers=host)
    flash = client.get("/api/tv").get_json()["flash"]
    assert flash["total"] == 120  # the TV countdown bar scales to this
    assert client.get(f"/qr/flash/{flash['token']}.png").status_code == 200
    assert client.post("/api/flash/claim", json={"pid": ann.pid, "token": "bad"}).status_code == 400
    claim = {"pid": ann.pid, "token": flash["token"]}
    client.post("/api/flash/claim", json=claim)
    client.post("/api/flash/claim", json=claim)  # only once per player
    assert ann.points == 4


def test_expired_flash_qr_is_refused(client, make_player, host):
    ann = make_player()
    client.post("/api/host/flash", json={"seconds": 10}, headers=host)
    game.state["flash"]["until"] -= 60
    resp = client.post("/api/flash/claim", json={"pid": ann.pid, "token": game.state["flash"]["token"]})
    assert resp.status_code == 400
    assert client.get("/api/tv").get_json()["flash"] is None


def test_qr_caching(client):
    assert client.get("/qr/join.png").headers["Cache-Control"] == "no-store"
    assert "max-age" in client.get("/qr/flash/abc.png").headers["Cache-Control"]
    assert "max-age" in client.get("/qr/secret/plum42.png").headers["Cache-Control"]


# ---------------------------------------------------------------- TV, finale, host data


def test_tv_counters_and_leaderboard(client, make_player):
    ann, ben = make_player("Ann"), make_player("Ben")
    ann.give("get_drink")
    ann.respond("get_drink")
    ben.give("cheers")
    ben.respond("cheers", "decline")
    tv = client.get("/api/tv").get_json()
    assert (tv["players"], tv["completed"], tv["declined"]) == (2, 1, 1)
    assert (tv["completed_pct"], tv["declined_pct"]) == (50, 50)
    assert tv["leaderboard"][0] == {"name": "Ann", "points": 1}
    assert tv["most_popular"]["name"] == "Drinks"


def test_finale_hides_cards_and_can_be_turned_off(client, make_player, host):
    ann = make_player()
    client.post("/api/host/finale", json={"on": True}, headers=host)
    assert ann.me()["finale"] and ann.me()["card"] is None
    assert client.get("/api/tv").get_json()["finale"]
    client.post("/api/host/finale", json={"on": False}, headers=host)
    assert ann.me()["card"] is not None


def test_host_data_lists_secrets_and_answers(client, make_player, host):
    ann = make_player()
    ann.give("intro")
    ann.respond("intro", answer="Maria")
    data = client.get("/api/host/data", headers=host).get_json()
    assert len(data["secrets"]) == 4
    assert data["answers"][0]["answer"] == "Maria"
    assert data["phase"] == "early"


# ---------------------------------------------------------------- dealing cards


def draws(pid, n):
    """Deal n cards to a player (forgetting what they have seen) and count them."""
    counts = Counter()
    game.state["assigned"] = {}
    for _ in range(n):
        game.state["players"][pid]["seen"] = {}
        counts[game.pick_card(pid)["card_id"]] += 1
    return counts


def test_whole_deck_gets_dealt(make_player):
    ann = make_player()
    random.seed(1)
    assert len(draws(ann.pid, 400)) >= 20


def test_player_sees_every_card_before_a_repeat(make_player):
    ann = make_player()
    deck = len(game.load_cards()["cards"])
    seen = []
    for _ in range(deck):
        card_id = ann.me()["card"]["id"]
        seen.append(card_id)
        ann.respond(card_id, "decline")
    assert len(set(seen)) == deck


def test_late_night_weights(make_player, monkeypatch):
    ann = make_player()
    random.seed(2)
    early = draws(ann.pid, 400)
    monkeypatch.setattr(game, "current_phase", lambda: "late")
    late = draws(ann.pid, 400)
    assert late["no_phone"] == 0  # weight 0 in the last 45 minutes
    assert late["t_midnight"] > 3 * early["t_midnight"]  # "guess the time" gets likelier


def test_phase_boundaries(monkeypatch):
    for hours, phase in [
        (5, "early"),
        (2, "early"),
        (1.9, "mid"),
        (0.75, "mid"),
        (0.5, "late"),
        (-1, "late"),
    ]:
        monkeypatch.setattr(game, "hours_to_midnight", lambda h=hours: h)
        assert game.current_phase() == phase


# ---------------------------------------------------------------- persistence


def test_state_is_saved_after_changes_and_reloads(make_player):
    ann = make_player("Ann")
    ann.give("get_drink")
    ann.respond("get_drink")
    on_disk = json.loads(Path(game.STATE_FILE).read_text())
    assert on_disk["players"][ann.pid]["points"] == 1
    assert game.load_state()["players"][ann.pid]["name"] == "Ann"


def test_reset_wipes_players_scores_and_photos(client, make_player, host):
    ann = make_player()
    ann.give("selfie")
    ann.upload("selfie")
    client.post("/api/host/reset", headers=host)
    assert game.state["players"] == {}
    assert list(Path(game.UPLOADS).glob("*.jpg")) == []
    assert client.get(f"/api/me?pid={ann.pid}").status_code == 404
