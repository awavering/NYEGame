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
    for path in (
        "/host",
        "/host/print",
        "/prompts",
        "/archive",
        "/api/host/data",
        "/api/host/cards",
        "/api/host/archive",
    ):
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
    ann.give("t_scoreboard")
    assert ann.respond("t_scoreboard", answer="lots").status_code == 400


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
    assert ann.upload("selfie").get_json()["points_earned"] == 3
    files = list(Path(game.UPLOADS).glob("*.jpg"))
    assert len(files) == 1
    assert max(Image.open(files[0]).size) <= 900
    assert len(client.get("/api/tv").get_json()["photos"]) == 1


def test_junk_upload_is_rejected(make_player):
    ann = make_player()
    ann.give("lego")
    assert ann.upload("lego", data=b"not an image").status_code == 400
    assert ann.me()["card"]["id"] == "lego"  # still theirs to complete


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
    assert ann.respond("no_phone").get_json()["points_earned"] == 5


def test_secret_qr(client, make_player):
    ann = make_player()
    ann.give("qr_fridge")
    assert client.post("/api/secret", json={"pid": ann.pid, "code": "wrong"}).status_code == 400
    other = client.post("/api/secret", json={"pid": ann.pid, "code": "kiwi17"}).get_json()
    assert other["note"].startswith("Nice find")  # a real code, but not their task
    assert ann.points == 0
    found = client.post("/api/secret", json={"pid": ann.pid, "code": "PLUM42"}).get_json()
    assert found["points_earned"] == 4  # codes are not case sensitive


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


def test_every_poll_answer_is_logged(client, make_player, host):
    ann, bob = make_player("Ann"), make_player("Bob")
    client.post("/api/host/poll", json={"question": "Q1", "kind": "text"}, headers=host)
    ann.poll_answer("one")
    ann.poll_answer("two")  # changing an answer keeps both in the log
    client.post("/api/host/poll", json={"question": "Q2", "kind": "text"}, headers=host)
    bob.poll_answer("three")
    log = game.state["poll_answers"]
    assert [(a["question"], a["name"], a["value"]) for a in log] == [
        ("Q1", "Ann", "one"),
        ("Q1", "Ann", "two"),
        ("Q2", "Bob", "three"),
    ]
    assert log[0]["poll_id"] == log[1]["poll_id"] != log[2]["poll_id"]


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
        game.state["players"][ann.pid].pop("skip_ready_at", None)  # no skip wait in this test
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


def test_reset_archives_the_game_first(client, make_player, host):
    ann = make_player()
    ann.give("selfie")
    ann.upload("selfie")
    photo = game.state["photos"][0]["file"]
    name = client.post("/api/host/reset", headers=host).get_json()["archive"]
    folder = Path(game.ARCHIVE) / name
    assert name == game.today().isoformat()
    saved = json.loads((folder / "state.json").read_text())
    assert saved["players"][ann.pid]["name"] == "Ann"
    assert saved["history"][0]["photo"] == saved["photos"][0]["id"]
    assert (folder / "uploads" / photo).exists()  # moved, not deleted
    assert (folder / "cards.json").exists()


def test_reset_of_an_empty_game_archives_nothing(client, host):
    assert client.post("/api/host/reset", headers=host).get_json()["archive"] is None
    assert not Path(game.ARCHIVE).exists()


def test_two_games_on_one_day_get_separate_archive_folders(client, make_player, host):
    make_player()
    first = client.post("/api/host/reset", headers=host).get_json()["archive"]
    make_player()
    second = client.post("/api/host/reset", headers=host).get_json()["archive"]
    assert second == first + "-2"
    assert (Path(game.ARCHIVE) / first / "state.json").exists()


# ---------------------------------------------------------------- skipping, starter and once cards


def test_skipping_twice_in_a_row_has_to_wait(make_player):
    ann = make_player()
    ann.give("lego")
    assert ann.respond("lego", "decline").status_code == 200  # the first skip is free
    me = ann.me()
    assert me["skip_wait"] > 0
    second = ann.respond(me["card"]["id"], "decline")
    assert second.status_code == 400 and "skip again" in second.get_json()["error"]
    game.state["players"][ann.pid]["skip_ready_at"] = 0  # pretend the wait is over
    assert ann.respond(me["card"]["id"], "decline").status_code == 200


def test_doing_a_card_ends_the_skip_wait(make_player):
    ann = make_player()
    ann.give("lego")
    ann.respond("lego", "decline")
    ann.give("get_drink")
    ann.respond("get_drink")
    assert ann.me()["skip_wait"] == 0


def test_skip_wait_can_be_turned_off(make_player, monkeypatch):
    monkeypatch.setattr(game, "settings", lambda: {"skip_wait_seconds": 0})
    ann = make_player()
    for _ in range(3):
        assert ann.respond(ann.me()["card"]["id"], "decline").status_code == 200


def test_new_guests_start_on_a_starter_card(make_player):
    starters = {c["id"] for c in game.load_cards()["cards"] if c.get("starter")}
    random.seed(3)
    for i in range(20):
        assert make_player(f"Guest{i}").me()["card"]["id"] in starters


def skip_through(player, times):
    """Skip `times` cards in a row (ignoring the skip wait); return the ids dealt."""
    dealt = []
    for _ in range(times):
        game.state["players"][player.pid].pop("skip_ready_at", None)
        card_id = player.me()["card"]["id"]
        dealt.append(card_id)
        player.respond(card_id, "decline")
    return dealt


def test_once_card_never_comes_back_after_it_is_done(make_player):
    ann = make_player()
    ann.give("venmo_albert")
    ann.respond("venmo_albert")
    deck = len(game.load_cards()["cards"])
    assert "venmo_albert" not in skip_through(ann, 3 * deck)  # three trips through the deck


def test_skipped_once_card_comes_back_with_the_deck(make_player):
    ann = make_player()
    ann.give("venmo_albert")
    ann.respond("venmo_albert", "decline")
    deck = len(game.load_cards()["cards"])
    assert "venmo_albert" in skip_through(ann, 2 * deck)


# ---------------------------------------------------------------- midnight, ending early, polls on the TV


def test_finale_starts_by_itself_at_midnight_only_once(client, make_player, host, monkeypatch):
    ann = make_player()
    tv = client.get("/api/tv").get_json()
    assert not tv["finale"] and tv["seconds_to_midnight"] == 5 * 3600
    monkeypatch.setattr(game, "hours_to_midnight", lambda: -0.01)  # just after midnight
    tv = client.get("/api/tv").get_json()
    assert tv["finale"] and tv["finale_kind"] == "midnight"
    # The host can turn it off to keep partying, and it stays off.
    client.post("/api/host/finale", json={"on": False}, headers=host)
    assert not client.get("/api/tv").get_json()["finale"]
    assert ann.me()["card"] is not None


def test_host_can_end_the_game_early_with_a_thank_you(client, make_player, host, monkeypatch):
    ann = make_player()
    client.post("/api/host/finale", json={"on": True, "kind": "thanks"}, headers=host)
    me = ann.me()
    assert me["finale"] and me["finale_kind"] == "thanks" and me["card"] is None
    assert client.get("/api/tv").get_json()["finale_kind"] == "thanks"
    # Midnight arriving later doesn't swap the thank-you for Happy New Year.
    monkeypatch.setattr(game, "hours_to_midnight", lambda: -0.01)
    assert client.get("/api/tv").get_json()["finale_kind"] == "thanks"
    assert client.post("/api/host/finale", json={"on": True, "kind": "rave"}, headers=host).status_code == 400


def test_finale_gets_every_photo_and_each_players_place(client, make_player, host):
    ann, ben, cal = make_player("Ann"), make_player("Ben"), make_player("Cal")
    ann.give("selfie")
    ann.upload("selfie")
    assert client.get("/api/tv").get_json()["finale_photos"] == []  # only sent during the finale
    client.post("/api/host/finale", json={"on": True}, headers=host)
    assert len(client.get("/api/tv").get_json()["finale_photos"]) == 1
    assert ann.me()["rank"] == 1
    assert ben.me()["rank"] == cal.me()["rank"] == 2  # tied scores share a place


def test_closed_poll_leaves_the_tv_after_a_while(client, host):
    client.post("/api/host/poll", json={"question": "Best snack?", "kind": "text"}, headers=host)
    client.post("/api/host/poll/close", headers=host)
    assert client.get("/api/tv").get_json()["poll"]["open"] is False  # final results still showing
    game.state["poll"]["closed_at"] -= game.POLL_RESULTS_SECONDS + 1
    assert client.get("/api/tv").get_json()["poll"] is None
    assert client.get("/api/host/data", headers=host).get_json()["poll"]  # the host still sees it


def launch_poll(client, host, question):
    client.post("/api/host/poll", json={"question": question, "kind": "text"}, headers=host)
    return game.state["poll"]["id"]


def test_old_polls_go_into_the_log_with_their_answers(client, make_player, host):
    ann = make_player()
    launch_poll(client, host, "Best snack?")
    ann.poll_answer("nachos")
    launch_poll(client, host, "Favorite song?")  # replaces the first one
    log = client.get("/api/host/data", headers=host).get_json()["poll_log"]
    assert [p["question"] for p in log] == ["Favorite song?", "Best snack?"]  # newest first
    assert log[0]["on_tv"] and not log[1]["on_tv"]
    assert log[1]["words"] == [["nachos", 1]] and not log[1]["open"]


def test_host_can_show_a_hidden_poll_again(client, make_player, host):
    first = launch_poll(client, host, "Best snack?")
    client.post("/api/host/poll/clear", headers=host)
    assert client.get("/api/tv").get_json()["poll"] is None
    assert client.post("/api/host/poll/show", json={"id": first}, headers=host).status_code == 200
    tv_poll = client.get("/api/tv").get_json()["poll"]
    assert tv_poll["question"] == "Best snack?" and not tv_poll["open"]  # results only, no new answers
    # It stays up (no 20-second timeout) until the host hides it again.
    game.state["poll"]["closed_at"] -= game.POLL_RESULTS_SECONDS + 60
    assert client.get("/api/tv").get_json()["poll"] is not None
    assert make_player().poll_answer("cake").status_code == 400
    client.post("/api/host/poll/clear", headers=host)
    assert client.get("/api/tv").get_json()["poll"] is None
    assert len(client.get("/api/host/data", headers=host).get_json()["poll_log"]) == 1  # not duplicated


def test_showing_an_old_poll_swaps_out_the_current_one(client, host):
    first = launch_poll(client, host, "Best snack?")
    launch_poll(client, host, "Favorite song?")
    client.post("/api/host/poll/show", json={"id": first}, headers=host)
    log = client.get("/api/host/data", headers=host).get_json()["poll_log"]
    assert [(p["question"], p["on_tv"]) for p in log] == [("Best snack?", True), ("Favorite song?", False)]


def test_a_poll_that_timed_out_can_be_shown_again(client, host):
    first = launch_poll(client, host, "Best snack?")
    client.post("/api/host/poll/close", headers=host)
    game.state["poll"]["closed_at"] -= game.POLL_RESULTS_SECONDS + 1
    assert client.get("/api/tv").get_json()["poll"] is None
    client.post("/api/host/poll/show", json={"id": first}, headers=host)
    assert client.get("/api/tv").get_json()["poll"]["question"] == "Best snack?"


def test_showing_an_unknown_poll_is_an_error(client, host):
    assert client.post("/api/host/poll/show", json={"id": "nope"}, headers=host).status_code == 404


# ---------------------------------------------------------------- the mascot's caption card


def test_answering_the_crispy_card_puts_crispy_on_the_tv(client, make_player):
    assert client.get("/api/tv").get_json()["mascot_line"] is None  # hidden until someone gives her a line
    ann = make_player("Ann")
    ann.give("crispy")
    reply = ann.respond("crispy", answer="Where are the snacks?").get_json()
    assert reply["note"] == "Look at the TV!" and reply["points_earned"] == 1
    line = client.get("/api/tv").get_json()["mascot_line"]
    assert line["text"] == "Where are the snacks?" and line["name"] == "Ann"


def test_the_next_crispy_answer_replaces_the_line(client, make_player):
    ann, ben = make_player("Ann"), make_player("Ben")
    ann.give("crispy")
    ann.respond("crispy", answer="Woof")
    first = client.get("/api/tv").get_json()["mascot_line"]
    game.state["mascot_line"]["t"] -= 5  # a few seconds later
    ben.give("crispy")
    ben.respond("crispy", answer="Happy New Year!")
    line = client.get("/api/tv").get_json()["mascot_line"]
    assert line["text"] == "Happy New Year!" and line["name"] == "Ben" and line["id"] != first["id"]


def test_crispy_leaves_after_three_minutes(client, make_player):
    ann = make_player()
    ann.give("crispy")
    ann.respond("crispy", answer="Woof")
    game.state["mascot_line"]["t"] -= game.MASCOT_LINE_SECONDS - 1
    assert client.get("/api/tv").get_json()["mascot_line"] is not None
    game.state["mascot_line"]["t"] -= 2
    assert client.get("/api/tv").get_json()["mascot_line"] is None


def test_skipping_or_other_cards_dont_make_crispy_talk(client, make_player):
    ann = make_player()
    ann.give("crispy")
    ann.respond("crispy", "decline")
    ann.give("intro")
    ann.respond("intro", answer="Maria")
    assert client.get("/api/tv").get_json()["mascot_line"] is None


def test_flash_qr_lasts_15_seconds_by_default(client, host):
    client.post("/api/host/flash", json={}, headers=host)
    flash = client.get("/api/tv").get_json()["flash"]
    assert flash["total"] == 15
    assert 14 < flash["ends_in"] <= 15  # exact time left, for the TV's smooth timer bar


# ---------------------------------------------------------------- prompts page


def test_prompts_page_lists_every_card_with_tonights_stats(client, make_player, host):
    ann, ben = make_player("Ann"), make_player("Ben")
    ann.give("get_drink")
    ann.respond("get_drink")
    ben.give("get_drink")
    ben.respond("get_drink", "decline")
    data = client.get("/api/host/cards", headers=host).get_json()
    assert [c["id"] for c in data["cards"]] == [c["id"] for c in game.load_cards()["cards"]]
    drink = next(c for c in data["cards"] if c["id"] == "get_drink")
    assert (drink["done"], drink["skipped"]) == (1, 1)
    assert drink["dealt"] == game.state["assigned"].get("get_drink", 0)  # real deals (give() skips the count)
    assert drink["weights"]["early"] == 2 and drink["starter"]
    qr = next(c for c in data["cards"] if c["type"] == "secret_qr")
    assert qr["code"] and qr["location"]  # host only, so the secret codes are included
    assert data["phase"] == "early" and data["draw_things"]


# ---------------------------------------------------------------- past games page


def played_and_archived(client, make_player, host):
    """A short game (a card, a photo, a poll), then reset. Returns the archive folder name."""
    ann, ben = make_player("Ann"), make_player("Ben")
    ann.give("get_drink")
    ann.respond("get_drink")
    ben.give("get_drink")
    ben.respond("get_drink", "decline")
    ann.give("selfie")
    ann.upload("selfie")
    client.post("/api/host/poll", json={"question": "Best snack?", "kind": "text"}, headers=host)
    ann.poll_answer("chips")
    ann.poll_answer("dip")
    ben.poll_answer("chips")
    return client.post("/api/host/reset", headers=host).get_json()["archive"]


def test_past_games_are_listed_newest_first(client, make_player, host):
    assert client.get("/api/host/archive", headers=host).get_json() == {"games": []}
    first = played_and_archived(client, make_player, host)
    make_player("Cat")
    second = client.post("/api/host/reset", headers=host).get_json()["archive"]
    games = client.get("/api/host/archive", headers=host).get_json()["games"]
    assert [g["name"] for g in games] == [second, first]
    old = games[1]
    assert (old["players"], old["winner"], old["photos"], old["polls"]) == (2, "Ann", 1, 1)
    assert old["done"] == 2  # Ann's drink and her selfie


def test_a_past_game_shows_scores_photos_polls_and_the_log(client, make_player, host):
    name = played_and_archived(client, make_player, host)
    game_ = client.get(f"/api/host/archive/{name}", headers=host).get_json()
    assert [p["name"] for p in game_["players"]] == ["Ann", "Ben"]
    assert (game_["players"][0]["done"], game_["players"][1]["skipped"]) == (2, 1)
    poll = game_["polls"][0]
    assert poll["question"] == "Best snack?"
    assert sorted((a["name"], a["value"]) for a in poll["answers"]) == [("Ann", "dip"), ("Ben", "chips")]
    selfie = next(h for h in game_["history"] if h["photo"])
    assert selfie["photo"] == game_["photos"][0]["url"]
    assert client.get(selfie["photo"], headers=host).status_code == 200  # the picture itself
    assert client.get(selfie["photo"]).status_code == 403  # host only
    assert client.get(f"/api/host/archive/{name}").status_code == 403


def test_a_past_game_that_does_not_exist_is_a_404(client, host):
    for name in ("2020-01-01", "nope", "..", "2026-12-31-x"):
        assert client.get(f"/api/host/archive/{name}", headers=host).status_code == 404
    assert client.get("/archive/photos/nope/x.jpg", headers=host).status_code == 404


def test_polls_that_fell_off_the_poll_log_are_still_in_the_archive(client, make_player, host, monkeypatch):
    monkeypatch.setattr(game, "POLL_LOG_SIZE", 1)
    ann = make_player("Ann")
    for question in ("Q1", "Q2", "Q3"):
        client.post("/api/host/poll", json={"question": question, "kind": "text"}, headers=host)
        ann.poll_answer(question.lower())
    name = client.post("/api/host/reset", headers=host).get_json()["archive"]
    polls = client.get(f"/api/host/archive/{name}", headers=host).get_json()["polls"]
    assert [(p["question"], p["answers"][0]["value"]) for p in polls] == [
        ("Q1", "q1"),
        ("Q2", "q2"),
        ("Q3", "q3"),
    ]
