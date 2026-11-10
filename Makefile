# Shortcuts for working on the game. Run `make` to see the list.
# Settings in .env (copy .env.example) are loaded automatically.
-include .env
export

# Use the project's virtualenv if `make setup` created one.
PY := $(if $(wildcard .venv/bin/python),.venv/bin/python,python3)
HOURS ?= 3
PLAYERS ?= 8

.DEFAULT_GOAL := help
.PHONY: help setup run dev sim test lint fmt cards check reset hooks

help:  ## show this list
	@awk 'BEGIN {FS = ":.*## "} /^[a-z]+:.*## / {printf "  make %-8s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

setup:  ## one time: create .venv and install everything
	python3 -m venv .venv
	.venv/bin/pip install -q -r requirements-dev.txt
	@echo "Done. Try: make check"

run:  ## start the real game (TV, phones and host links are printed)
	$(PY) app.py

dev:  ## rehearsal: fake clock (HOURS=3 means midnight in 3 hours), separate data folder
	@MIDNIGHT_ISO=$$($(PY) -c 'import os; from datetime import datetime, timedelta; from zoneinfo import ZoneInfo; tz = ZoneInfo(os.environ.get("TZ_NAME", "America/Denver")); print((datetime.now(tz) + timedelta(hours=$(HOURS))).strftime("%Y-%m-%dT%H:%M:%S"))') && \
	echo "Fake clock: midnight at $$MIDNIGHT_ISO. Data goes in .dev-data/ (your real game is untouched)." && \
	MIDNIGHT_ISO=$$MIDNIGHT_ISO GAME_DATA_DIR=.dev-data $(PY) app.py

sim:  ## fake guests for rehearsing (needs `make dev` or `make run` going; PLAYERS=8)
	$(PY) tools/simulate_guests.py --url http://localhost:$(or $(PORT),5000) --players $(PLAYERS) --minutes 3

test:  ## run the tests
	$(PY) -m pytest

lint:  ## check code style (what CI checks)
	$(PY) -m ruff check .
	$(PY) -m ruff format --check .

fmt:  ## fix code style automatically
	$(PY) -m ruff check --fix .
	$(PY) -m ruff format .

cards:  ## check cards.json for mistakes
	$(PY) tools/check_cards.py

check: lint cards test  ## everything CI runs: lint, cards, tests

reset:  ## wipe the real game's saved state and photos (new game)
	rm -f state.json state.json.tmp uploads/*.jpg
	@echo "Cleared state.json and uploads/"

hooks:  ## run `make check` automatically before every commit
	git config core.hooksPath .githooks
	@echo "Pre-commit check enabled. Skip once with: git commit --no-verify"
