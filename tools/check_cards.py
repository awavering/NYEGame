"""Check cards.json for mistakes before your guests find them.

    python tools/check_cards.py              # checks ./cards.json
    python tools/check_cards.py other.json

Errors (exit code 1) are things that would break the game. Warnings are
things that are probably not what you meant.

The server runs this same check every time cards.json changes. If an edit
has errors, the server keeps using the last good version and prints what is
wrong, so a typo during the party can't take the game down.
"""

import json
import re
import sys
from pathlib import Path

# Keep in sync with the card handlers in pages/play.html (drawCard) and with
# api_respond / api_photo in app.py. DEVELOPMENT.md has the checklist for
# adding a new card type.
CARD_TYPES = {
    "confirm",
    "text",
    "number",
    "photo",
    "draw",
    "secret_qr",
    "timer",
    "gforce",
    "light",
}
PHASES = {"early", "mid", "late"}
CAMERAS = {"user", "environment", "library"}
POLL_KINDS = {"choice", "number", "text"}
GRADES = {"minutes_to_midnight", "scoreboard_number"}
KNOWN_PLACEHOLDERS = {"thing", "albert_handle"}
PLACEHOLDER_RE = re.compile(r"\{(\w+)\}")
CODE_RE = re.compile(r"^[A-Za-z0-9_-]+$")  # codes live in a URL path: /s/<code>
LONG_PROMPT = 140  # characters; longer prompts get cramped on a phone


IMAGE_RE = re.compile(r"^[A-Za-z0-9_-]+\.(png|gif|webp|jpg|jpeg)$")  # a plain file name, no folders


def _check_image(where, name, assets_dir, errors):
    if not isinstance(name, str) or not IMAGE_RE.match(name):
        errors.append(
            f'{where}: image must be a file name like "dog.png" (letters, digits, - and _) kept in pages/assets/.'
        )
    elif assets_dir is not None and not (Path(assets_dir) / name).is_file():
        errors.append(f'{where}: image "{name}" was not found in pages/assets/.')


def _is_number(value):
    return isinstance(value, int | float) and not isinstance(value, bool)


def _check_card(i, card, draw_things, settings, assets_dir, errors, warnings):
    cid = card.get("id") if isinstance(card, dict) else None
    where = f'card #{i + 1} ("{cid}")' if cid else f"card #{i + 1}"
    if not isinstance(card, dict):
        errors.append(f"{where}: must be an object.")
        return None

    for field in ("id", "category", "prompt"):
        if not isinstance(card.get(field), str) or not card[field].strip():
            errors.append(f'{where}: "{field}" is required (non-empty text).')
    if "hint" in card and not isinstance(card["hint"], str):
        errors.append(f'{where}: "hint" must be text.')
    # "once": never dealt again after it is done. "starter": can be someone's first card.
    # "mascot_caption": the answer becomes the TV mascot's speech bubble (text cards only).
    for flag in ("once", "starter", "mascot_caption"):
        if flag in card and not isinstance(card[flag], bool):
            errors.append(f'{where}: "{flag}" must be true or false.')
    if card.get("mascot_caption") and card.get("type") != "text":
        errors.append(
            f'{where}: "mascot_caption" only works on text cards (the answer is what the mascot says).'
        )
    if card.get("mascot_caption") and not settings.get("mascot"):
        warnings.append(
            f'{where}: has "mascot_caption" but settings has no "mascot", so nothing will show on the TV.'
        )

    if "image" in card:
        _check_image(where, card["image"], assets_dir, errors)

    ctype = card.get("type")
    if ctype not in CARD_TYPES:
        errors.append(f'{where}: unknown type "{ctype}". Use one of: {", ".join(sorted(CARD_TYPES))}.')

    if "points" in card and (not _is_number(card["points"]) or card["points"] < 0):
        errors.append(f'{where}: "points" must be a number, 0 or more.')

    weights = card.get("weights")
    if weights is not None:
        if not isinstance(weights, dict):
            errors.append(f'{where}: "weights" must look like {{"early": 1, "mid": 1, "late": 1}}.')
        else:
            for phase, w in weights.items():
                if phase not in PHASES:
                    errors.append(f'{where}: weights has unknown phase "{phase}" (use early, mid, late).')
                elif not _is_number(w) or w < 0:
                    errors.append(f'{where}: weight for "{phase}" must be a number, 0 or more.')

    if ctype == "secret_qr":
        code = card.get("code")
        if not isinstance(code, str) or not CODE_RE.match(code):
            errors.append(f'{where}: secret_qr needs a "code" using only letters, digits, - and _.')
    if ctype == "timer" and (not _is_number(card.get("minutes")) or card["minutes"] <= 0):
        errors.append(f'{where}: timer needs "minutes" greater than 0.')
    if ctype in ("photo", "light") and "camera" in card and card["camera"] not in CAMERAS:
        errors.append(f"{where}: camera must be one of {', '.join(sorted(CAMERAS))}.")
    if "grade" in card and (ctype != "number" or card["grade"] not in GRADES):
        errors.append(f'{where}: "grade" only works on number cards, one of {", ".join(sorted(GRADES))}.')

    prompt = card.get("prompt") if isinstance(card.get("prompt"), str) else ""
    for name in PLACEHOLDER_RE.findall(prompt):
        if name == "thing" and (ctype != "draw" or not draw_things):
            errors.append(f"{where}: {{thing}} only works on draw cards, and needs draw_things.")
        elif name == "albert_handle" and not settings.get("albert_handle"):
            warnings.append(f'{where}: uses {{albert_handle}} but settings has no "albert_handle".')
        elif name not in KNOWN_PLACEHOLDERS:
            warnings.append(f"{where}: {{{name}}} is not a known placeholder and will show as typed.")
    if len(prompt) > LONG_PROMPT:
        warnings.append(f"{where}: prompt is {len(prompt)} characters; consider shortening for phones.")
    return cid


def validate(data, assets_dir=None):
    """Return (errors, warnings), two lists of readable strings."""
    errors, warnings = [], []
    if not isinstance(data, dict):
        return ['cards.json must be a JSON object with a "cards" list.'], []

    settings = data.get("settings", {})
    if not isinstance(settings, dict):
        errors.append('"settings" must be an object.')
        settings = {}
    if settings.get("mascot") is not None:
        _check_image("settings.mascot", settings["mascot"], assets_dir, errors)
    draw_things = data.get("draw_things", [])
    if not isinstance(draw_things, list) or not all(isinstance(t, str) for t in draw_things):
        errors.append('"draw_things" must be a list of text.')
        draw_things = []

    cards = data.get("cards")
    if not isinstance(cards, list) or not cards:
        errors.append('"cards" must be a non-empty list.')
        cards = []

    seen_ids, seen_codes = {}, {}
    for i, card in enumerate(cards):
        cid = _check_card(i, card, draw_things, settings, assets_dir, errors, warnings)
        if cid is None:
            continue
        if cid in seen_ids:
            errors.append(f'card #{i + 1}: id "{cid}" is already used by card #{seen_ids[cid]}.')
        seen_ids.setdefault(cid, i + 1)
        code = card.get("code")
        if card.get("type") == "secret_qr" and isinstance(code, str):
            if code.lower() in seen_codes:
                errors.append(
                    f'card #{i + 1}: secret code "{code}" duplicates card #{seen_codes[code.lower()]}.'
                )
            seen_codes.setdefault(code.lower(), i + 1)

    for key in ("skip_wait_seconds", "tv_photo_limit"):
        if key in settings and (not _is_number(settings[key]) or settings[key] < 0):
            errors.append(f'settings: "{key}" must be a number, 0 or more.')
    if cards and not any(isinstance(c, dict) and c.get("starter") for c in cards):
        warnings.append('no card has "starter": true, so a new guest could get any card first.')

    handle = str(settings.get("albert_handle", ""))
    if "edit me" in handle.lower():
        warnings.append('settings.albert_handle still says "edit me". Put the real payment handle there.')

    presets = data.get("poll_presets", [])
    if not isinstance(presets, list):
        errors.append('"poll_presets" must be a list.')
        presets = []
    for i, preset in enumerate(presets):
        where = f"poll preset #{i + 1}"
        if not isinstance(preset, dict) or not str(preset.get("question", "")).strip():
            errors.append(f'{where}: needs a "question".')
            continue
        if preset.get("kind") not in POLL_KINDS:
            errors.append(f"{where}: kind must be one of {', '.join(sorted(POLL_KINDS))}.")
        elif preset["kind"] == "choice" and len(preset.get("options", [])) < 2:
            errors.append(f"{where}: a choice poll needs at least two options.")

    return errors, warnings


def main(argv):
    path = Path(argv[1]) if len(argv) > 1 else Path(__file__).resolve().parent.parent / "cards.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"ERROR: could not read {path}: {exc}")
        return 1
    errors, warnings = validate(data, assets_dir=path.resolve().parent / "pages" / "assets")
    for w in warnings:
        print(f"warning: {w}")
    for e in errors:
        print(f"ERROR: {e}")
    n = len(data.get("cards", [])) if isinstance(data, dict) else 0
    print(f"{path.name}: {n} cards, {len(errors)} errors, {len(warnings)} warnings")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
