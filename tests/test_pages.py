"""The HTML pages: their scripts parse, and every server call they make exists."""

import re
import shutil
import subprocess

import pytest
from helpers import ROOT, game

PAGES = sorted((ROOT / "pages").glob("*.html"))


def scripts_of(path):
    return re.findall(r"<script>(.*?)</script>", path.read_text(encoding="utf-8"), re.S)


@pytest.mark.skipif(not shutil.which("node"), reason="node is not installed")
@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.name)
def test_page_scripts_have_no_syntax_errors(page, tmp_path):
    js = tmp_path / "page.js"
    js.write_text("\n".join(scripts_of(page)), encoding="utf-8")
    result = subprocess.run(["node", "--check", str(js)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_every_api_path_used_by_a_page_exists_on_the_server():
    rules = [
        re.compile("^" + re.sub(r"<[^>]+>", "[^/]+", rule.rule) + "$")
        for rule in game.app.url_map.iter_rules()
    ]
    problems = []
    for page in PAGES:
        text = page.read_text(encoding="utf-8")
        for path in set(re.findall(r"""['"`](/api/[A-Za-z0-9_/-]*)""", text)):
            if not any(rule.match(path.rstrip("/")) for rule in rules):
                problems.append(f"{page.name} calls {path}")
    assert problems == []


def test_every_page_the_server_serves_exists():
    for name in ("play", "tv", "host", "print", "claim", "prompts"):
        assert (ROOT / "pages" / f"{name}.html").exists()
