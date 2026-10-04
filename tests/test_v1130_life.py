"""v1.13: Life, the Personal workspace's Ledger place, from the notes vault (SPEC 8.11).

Generic names only: the operator's own areas live in a private life.toml.
"""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from ultra.life import (
    DEFAULT_AREAS,
    Life,
    LifeApi,
    _log_section,
    _pretty,
    area_of_task,
    load_areas,
    plain,
)
from ultra.vault import VaultError

ROOT = Path(__file__).resolve().parents[1]
CAL = "\U0001f4c5"

AREAS_TOML = """
skip_tasks = ["01 - Hubs/Chore_Checklist"]

[[area]]
key = "home"
name = "Home"
icon = "H"
blurb = "The house."
notes = [["01 - Hubs/Home_MOC", "Home hub"], ["01 - Hubs/Missing", "Not there"]]
folders = ["Assets/House"]
words = ["coop", "furnace"]
log_topic = "Homestead"

[[area]]
key = "animals"
name = "Animals"
words = ["hens", "flock"]

[[area]]
key = "garden"
name = "Garden"
words = ["garlic", "seed"]

[[area]]
key = "vehicles"
name = "Vehicles"
folders = ["Assets/Fleet"]
words = ["camper"]

[[area]]
key = "spirit"
name = "Spirit"
folders = ["02 - Personal/Events/Gathering_2026.md"]
"""

TASKS = [
    ("01 - Hubs/Tasks and Todos.md", 8, f"Coop: test the heated waterer {CAL} 2026-10-01"),
    ("01 - Hubs/Tasks and Todos.md", 9, f"Garlic: buy seed garlic {CAL} 2026-10-05"),
    ("01 - Hubs/Tasks and Todos.md", 10, f"Camper: drop-off ( [[Dealer_Note]]) {CAL} 2026-10-03"),
    ("01 - Hubs/Tasks and Todos.md", 11, "Feed the hens extra corn"),
    ("Assets/Fleet/Camper.md", 40, "Grease the scissor jacks"),
    ("02 - Personal/Events/Gathering_2026.md", 5, f"Warm layers {CAL} 2026-10-08"),
    ("Daily Notes/2026-08-05_Daily_Schedule.md", 3, "Old schedule box"),
    ("01 - Hubs/Chore_Checklist.md", 3, "Daily chore"),
    ("_archive/x.md", 1, f"Archived thing {CAL} 2026-09-01"),
    ("01 - Hubs/Goals.md", 2, "Learn **Norwegian**"),
]

RECENT = {
    "Journal": ["Journal/Garden/2026-10-02_Bed_prep.md"],
    "Daily Notes/Check-ins": ["Daily Notes/Check-ins/2026-10-02_Checkin_Evening.md"],
    "Assets/Fleet": ["Assets/Fleet/Camper.md", "Assets/Fleet/2026-09-24_Dealer_Reply.md"],
}

DAILY = (
    "# Day\n\n## Weather\nclear\n\n## Log\n\n- 7:30 AM Fed the hens\n"
    "- 9:00 AM Dropped the camper\n\n## Tomorrow\n- x\n"
)


class FakeVault:
    """Answers the vault-mcp read tools Life uses; records every call."""

    enabled = True
    writes = True
    name = "Notes"

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.today_fn = lambda: dt.date(2026, 10, 3)

    def _hidden(self, path: str) -> bool:
        return path.startswith("_archive/")

    def _call(self, tool: str, args: dict[str, Any], ttl: float = 60) -> Any:
        self.calls.append((tool, args))
        if tool == "vault_tasks":
            return {"results": [{"path": p, "line": n, "text": t} for p, n, t in TASKS]}
        if tool == "vault_read":
            if args["path"] == "01 - Hubs/Home_MOC":
                return {"path": args["path"] + ".md", "contents": "# x"}
            raise VaultError(400, "note not found")
        if tool == "vault_recent":
            if args["folder"] not in RECENT:
                raise VaultError(400, "bad path")
            rows = RECENT[args["folder"]]
            return {"results": [{"path": p, "modified": "2026-10-03T10:00:00-04:00"} for p in rows]}
        if tool == "vault_day":
            return {
                "daily": {"path": "Daily Notes/2026-10-03_Daily_Schedule.md", "contents": DAILY},
                "checkins": ["Daily Notes/Check-ins/2026-10-03_Checkin_Morning.md"],
                "captains_log": "",
                "logs": [],
            }
        raise AssertionError(f"Life must not call {tool}")


def _areas(tmp_path: Path):
    p = tmp_path / "life.toml"
    p.write_text(AREAS_TOML)
    return load_areas(p)


@pytest.fixture
def life(tmp_path: Path) -> Life:
    (tmp_path / "life.toml").write_text(AREAS_TOML)
    cfg = SimpleNamespace(config_home=tmp_path)
    return Life.from_config(FakeVault(), cfg)  # type: ignore[arg-type]


def test_default_areas_are_generic():
    keys = [a.key for a in DEFAULT_AREAS]
    assert keys == ["home", "animals", "garden", "vehicles", "family", "spirit", "money", "fun"]
    assert all(a.name and a.blurb for a in DEFAULT_AREAS)


def test_life_toml_and_fallbacks(tmp_path: Path):
    a = _areas(tmp_path)
    assert [x.key for x in a] == ["home", "animals", "garden", "vehicles", "spirit"]
    assert a[0].icon == "H" and a[0].log_topic == "Homestead"
    assert load_areas(tmp_path / "missing.toml") == DEFAULT_AREAS
    for name, body in (
        ("bad", '[[area]]\nkey = "Bad Key"\n'),
        ("dup", '[[area]]\nkey = "aa"\n[[area]]\nkey = "aa"\n'),
        ("broken", "[[area\n"),
    ):
        p = tmp_path / f"{name}.toml"
        p.write_text(body)
        assert load_areas(p) == DEFAULT_AREAS, name


@pytest.mark.parametrize(
    ("path", "text", "want"),
    [
        ("01 - Hubs/Tasks and Todos.md", "Coop: test the waterer", "home"),
        ("01 - Hubs/Tasks and Todos.md", "Feed the hens extra corn", "animals"),
        ("01 - Hubs/Tasks and Todos.md", "Garlic: buy seed", "garden"),
        ("01 - Hubs/Tasks and Todos.md", "Camper: winterize", "vehicles"),
        ("Assets/Fleet/Camper.md", "Grease the jacks", "vehicles"),
        ("02 - Personal/Events/Gathering_2026.md", "Warm layers", "spirit"),
        ("01 - Hubs/Goals.md", "Learn Norwegian", "other"),
    ],
)
def test_tasks_are_filed_by_folder_then_prefix_then_words(tmp_path, path, text, want):
    assert area_of_task({"path": path, "text": text}, _areas(tmp_path)) == want


def test_plain_text_and_titles():
    assert plain("Camper: drop-off ( [[Dealer_Note]])") == "Camper: drop-off"
    assert plain("See [[Assets/Fleet/Camper|the camper]] notes") == "See the camper notes"
    assert plain("Read **The Effective Executive**") == "Read The Effective Executive"
    assert _pretty("Daily Notes/Check-ins/2026-10-02_Checkin_Evening.md") == "Evening check-in"
    assert _pretty("Daily Notes/Check-ins/2026-09-15_Checkin_Evening_2.md") == "Evening check-in"
    assert _pretty("Journal/Garden/2026-10-02_Bed_prep.md") == "Bed prep"
    assert _log_section("## A\nx\n## Log\n\n- one\n- two\n## B\n- no") == ["- one", "- two"]


def test_overview(life: Life):
    o = life.overview()
    by = {a["key"]: a for a in o["areas"]}
    assert set(by) == {"home", "animals", "garden", "vehicles", "spirit"}
    assert by["home"]["overdue"] == 1 and by["home"]["next"]["text"].startswith("Coop")
    assert by["home"]["icon"] == "H"
    assert by["garden"]["soon"] == 1 and by["vehicles"]["soon"] == 1
    assert by["vehicles"]["open"] == 2
    assert by["animals"]["open"] == 1 and by["spirit"]["soon"] == 1
    texts = [t["text"] for t in o["due"]]
    for gone in ("Old schedule box", "Daily chore", "Archived thing"):
        assert gone not in texts  # schedules, skip_tasks notes and hidden folders
    assert [t["days"] for t in o["due"]] == [-2, 0, 2, 5]  # sorted by due date
    assert o["day"]["log"] == ["- 7:30 AM Fed the hens", "- 9:00 AM Dropped the camper"]
    assert o["day"]["checkins"] == ["Daily Notes/Check-ins/2026-10-03_Checkin_Morning.md"]
    assert [r["title"] for r in o["recent"]] == ["Bed prep", "Evening check-in"]
    assert o["undated_other"] == 1


def test_area_pages(life: Life):
    h = life.area("home")
    assert [n["label"] for n in h["notes"]] == ["Home hub"]  # a missing note is skipped
    a = life.area("vehicles")
    assert [t["text"] for t in a["tasks"]] == ["Camper: drop-off", "Grease the scissor jacks"]
    assert a["recent"][0]["path"] == "Assets/Fleet/2026-09-24_Dealer_Reply.md"
    with pytest.raises(VaultError):
        life.area("nope")


def test_life_only_reads(life: Life):
    life.overview()
    for k in life.keys:
        life.area(k)
    tools = {t for t, _ in life.vault.calls}  # type: ignore[attr-defined]
    assert tools <= {"vault_tasks", "vault_read", "vault_recent", "vault_day"}


def test_api_routes_and_off(life: Life):
    from ultra.server import ApiError

    api = LifeApi(life)
    assert api.r_overview({}, None, None)["enabled"] is True  # type: ignore[arg-type]
    m = re.match(r"/api/life/area/([a-z]{2,20})", "/api/life/area/garden")
    assert api.r_area({}, None, m)["key"] == "garden"  # type: ignore[arg-type]
    with pytest.raises(ApiError):
        api.r_area({}, None, re.match(r"(.+)", "nosuch"))  # type: ignore[arg-type]
    life.vault.enabled = False
    assert api.r_overview({}, None, None) == {"enabled": False}  # type: ignore[arg-type]


def test_ui_wiring():
    app = (ROOT / "src/ultra/static/app.js").read_text()
    js = (ROOT / "src/ultra/static/life.js").read_text()
    assert "const useLife = () => !S.ledgerOn && S.vaultHere;" in app
    assert 'pb.textContent = "Life"' in app and "initLife(ledgerHooks)" in app
    for verb in ("vault_log", "vault_task", "write("):
        assert verb not in js  # writes: the review card, or the one tick-done route
    assert js.count('method: "POST"') == 1 and '"/api/life/done"' in js
    assert "esc(t.text)" in js and "esc(r.title)" in js and "esc(a.icon)" in js
