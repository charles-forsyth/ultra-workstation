"""v1.5/1.6: the calm layout and the action table (SPEC 7.13).

Hiding must never lose an action: every toolbar button the views render has a row in
`static/actions.js` (so it lands on the toolbar or in a menu, and in the palette), every
item key maps to an action, and the calm toolbar stays inside its button budget.
Browser-level counts (visible buttons per view) were measured with Playwright on demo
data and are recorded in SPEC 7.13; these tests pin the source-level guarantees.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "src/ultra/static"


def src(name: str) -> str:
    return (STATIC / name).read_text()


def table(name: str) -> list[dict[str, str]]:
    body = src("actions.js")
    i = body.index(f"export const {name} = [")
    block = body[i : body.index("];", i)]
    rows = []
    for m in re.finditer(
        r"\{ id: \"([^\"]+)\", label: \"([^\"]+)\", place: \"([^\"]+)\"(.*?)\}", block
    ):
        key = re.search(r'key: "([^"]+)"', m.group(4))
        rows.append(
            {
                "id": m.group(1),
                "label": m.group(2),
                "place": m.group(3),
                "key": key.group(1) if key else "",
            }
        )
    return rows


def thread_toolbar() -> str:
    app = src("app.js")
    i = app.index('<div class="thread-acts">')
    return app[i : app.index("</div>", i)]


def task_toolbar() -> str:
    app = src("app.js")
    i = app.index('<div class="thread-acts">', app.index("async function openTask"))
    return app[i : app.index("</div>\n    <section", i)]


PLACES = {"primary", "reply", "ai", "more"}


@pytest.mark.parametrize("name", ["ACTIONS", "TASK_ACTIONS", "ROW_ACTIONS"])
def test_every_action_has_a_known_place_and_unique_id(name):
    rows = table(name)
    assert rows, f"{name} is empty"
    ids = [r["id"] for r in rows]
    assert len(ids) == len(set(ids)), f"duplicate ids in {name}"
    assert {r["place"] for r in rows} <= PLACES


def test_every_thread_button_has_a_row_in_the_table():
    bar = thread_toolbar()
    found = set(re.findall(r'data-a="([a-z_]+)"', bar))
    assert found, "no thread buttons found"
    ids = {r["id"] for r in table("ACTIONS")}
    assert found <= ids, f"thread buttons with no action-table row: {sorted(found - ids)}"
    assert "askButton(" in bar and "ask" in ids  # Ask Hermes button
    assert "t.permalink" in bar and "permalink" in ids  # Open in Slack link


def test_every_task_button_has_a_row_in_the_table():
    bar = task_toolbar()
    found = set(re.findall(r'data-t="([a-z_]+)"', bar))
    assert found, "no task buttons found"
    ids = {r["id"] for r in table("TASK_ACTIONS")}
    assert found <= ids, f"task buttons with no action-table row: {sorted(found - ids)}"


def test_task_block_time_and_blocked_status_are_different_actions():
    # They shared data-t="block": in a menu, "Block time" would have set the task BLOCKED.
    bar = task_toolbar()
    assert bar.count('data-t="block"') == 1 and 'data-t="blocktime"' in bar
    app = src("app.js")
    assert 'if (a === "blocktime")' in app and 'if (a === "block") { window' not in app


def test_every_row_button_has_a_row_in_the_table():
    app = src("app.js")
    i = app.index("function rowActs(it)")
    body = app[i : app.index("\n}\n", i)]
    found = set(re.findall(r'data-ra="([a-z_]+)"', body)) - {"more"}
    ids = {r["id"] for r in table("ROW_ACTIONS")}
    assert found <= ids, f"row buttons with no action-table row: {sorted(found - ids)}"


def test_calm_thread_toolbar_budget():
    rows = table("ACTIONS")
    primary = [r for r in rows if r["place"] == "primary"]
    # Reply split (button + caret) + primary + AI + "..." must stay at 6 or fewer
    assert 2 + len(primary) + 2 <= 6, f"too many primary actions: {[r['id'] for r in primary]}"
    assert (
        next(r["id"] for r in rows if r["place"] == "reply") == "reply_all"
    )  # Q5: Reply all leads


def test_item_keys_map_to_actions():
    """Keys that act on an open item click a button the table places somewhere."""
    app = src("app.js")
    body = app[app.index("if (it && S.key === it.key) {") :]
    body = body[: body.index('if (e.key === "c")')]
    clicked = dict(
        re.findall(r'e\.key === "(.)"\) \{ e\.preventDefault\(\); click\("([a-z_]+)"\)', body)
    )
    assert clicked, "no item key handlers found"
    keyed = {r["key"]: r["id"] for r in table("ACTIONS") if r["key"]}
    for key, action in clicked.items():
        assert keyed.get(key) == action, (
            f"key {key} clicks {action} but the table says {keyed.get(key)}"
        )
    assert keyed.get("h") == "ask"


def test_palette_lists_item_actions_from_the_table():
    app = src("app.js")
    i = app.index("function commands()")
    body = app[i : app.index("\nfunction palette()", i)]
    assert "TASK_ACTIONS : ACTIONS" in body and "for (const a of [...table].reverse())" in body
    assert "Use the classic layout" in body  # the way back is always in the palette


def test_menus_are_built_from_the_real_buttons():
    acts = src("actions.js")
    # disabled / relabelled state comes from the element, and clicking runs its handler
    assert "disabled: el.disabled" in acts and "run: () => el.click()" in acts
    assert "el.textContent.trim() || row.label" in acts
    # labels go through textContent, never innerHTML (menus carry subjects and names)
    assert "innerHTML" not in acts


def test_layout_switch_reaches_the_page(tmp_path):
    from ultra.config import Config
    from ultra.server import Api

    def session(raw):
        api = Api.__new__(Api)
        api.cfg = Config(raw)
        api.token, api.demo, api.live = "t", True, None
        return api.session({}, None, None)

    assert session({})["layout"] == "calm"
    assert session({"ui": {"layout": "classic"}})["layout"] == "classic"
    assert session({"ui": {"layout": "weird"}})["layout"] == "calm"
    html = src("index.html")
    assert '<body class="calm">' in html
    app = src("app.js")
    assert 'localStorage.getItem("ultra.layout") || s.layout || "calm"' in app


def test_classic_layout_keeps_every_button():
    """Calm hides classic-only controls with CSS; nothing is removed from the page."""
    html = src("index.html")
    for bid in [
        "btn-board",
        "btn-graph",
        "btn-day",
        "btn-today",
        "btn-ledger",
        "btn-refresh",
        "btn-tidy",
        "btn-help",
    ]:
        assert f'id="{bid}"' in html, bid
    css = src("app.css")
    assert "body.calm .classic-only { display: none !important; }" in css
    assert "body:not(.calm) .calm-only" in css


def test_every_place_and_menu_item_reaches_a_view():
    app = src("app.js")
    for fn in [
        "openToday()",
        "openLedgerTab()",
        "openBoard()",
        "openTidy()",
        "openDay()",
        "openGraph()",
    ]:
        assert fn in app, fn
    for f in ["slack", "tasks", "tickets", "low"]:
        assert f'["{f}",' in app  # every filter dropped from the bar is in the menu


def test_hover_controls_show_on_touch():
    css = src("app.css")
    assert "@media (hover: none) { body.calm .item .rq { opacity: 1; } }" in css
    assert "@media (hover: none) { body.calm .att .btn { display: inline-block; } }" in css


def test_example_config_documents_ui_layout():
    import tomllib

    text = (ROOT / "src/ultra/config.example.toml").read_text()
    assert tomllib.loads(text)["ui"]["layout"] == "calm"


def test_action_labels_are_ascii():
    for name in ["ACTIONS", "TASK_ACTIONS", "ROW_ACTIONS"]:
        for r in table(name):
            assert r["label"].isascii(), r
    json.dumps(table("ACTIONS"))


# ---------------------------------------------------------------- v1.7 (U2)
def test_tidy_suggestion_only_in_calm_and_can_be_hidden_for_the_day():
    app = src("app.js")
    i = app.index("async function tidyHint()")
    body = app[i : app.index("\n}\n", i)]
    assert "if (!isCalm() || S.search) return;" in body
    assert 'localStorage.getItem("ultra.tidyHintOff") === localDay(0)' in body
    assert "innerHTML" not in body  # the count is the only data, set as text
    assert "openTidy()" in body  # the suggestion opens the normal preview, never runs


def test_ready_archive_offers_a_log_card_not_a_log():
    app = src("app.js")
    i = app.index("async function archive(it")
    body = app[i : app.index("\n}\n", i)]
    assert '(it.badges || []).includes("READY") && isCalm()' in body
    assert 'stage("log", it.key)' in body  # the staged card, reviewed as always


def test_today_plan_strip_and_ledger_summary_are_calm_only():
    today = src("today.js")
    assert 'id="today-plan" class="today-plan calm-only"' in today
    assert 'api("/api/day/plan")' in today and 'data-goto="day"' in today
    lt = src("ltab.js")
    assert 'class="lt-summary calm-only"' in lt and 'class="lt-tiles classic-only"' in lt
    assert 'show("org")' in lt and 'show("reports")' in lt  # moved to the ... menu, still reachable
    css = src("app.css")
    assert "body.calm #lt-seg .lt-rare { display: none; }" in css
    assert "@media (hover: none) { body.calm .bd-acts { opacity: 1; } }" in css


def test_board_takes_the_full_width_in_calm():
    app = src("app.js")
    assert 'document.body.classList.toggle("boardview", boardOpen());' in app
    css = src("app.css")
    assert "body.calm.boardview .panel.left { display: none; }" in css
    board = src("board.js")
    assert 'data-goto="inbox"' in board  # List gets back to the stream


def test_both_layouts_have_a_visible_way_to_the_other():
    """v1.9.1: classic had no visible way back to calm (only the palette)."""
    html = src("index.html")
    assert 'class="btn ghost classic-only" id="btn-layout"' in html
    app = src("app.js")
    assert '$("#btn-layout").onclick = () => setLayout("calm");' in app
    assert 'run: () => setLayout("classic")' in app  # dot menu, calm side
    assert 'e.key === "L") setLayout(isCalm() ? "classic" : "calm")' in app
