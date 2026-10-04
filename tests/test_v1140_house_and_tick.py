"""v1.14: house sensors on Life tiles, and ticking a task done from Life (SPEC 8.11)."""

from __future__ import annotations

import datetime as dt
import re
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from ultra.house import PATHS, House, HouseApi, _lines, private_url
from ultra.life import Life, LifeApi
from ultra.server import ApiError
from ultra.vault import Vault, VaultError, client_from_config

ROOT = Path(__file__).resolve().parents[1]
CAL = "\U0001f4c5"

DOCS: dict[str, Any] = {
    "/api/coop/thermal": {"roost_interior_temp_f": 42.0, "waterer_ice_risk": "LOW"},
    "/api/coop": {"is_past_sunset": True, "master_armed": True},
    "/api/freeze": {"min_temp_24h": 48.2, "min_temp_48h": 46.2, "min_temp_72h": 33.2},
    "/api/status": {
        "weather": {"airport": {"wind": 3}, "home": {"temp_f": 49.2, "stale": False}},
        "sensors": {"hardware_ambient": {"stale": True, "age_min": 1458.8}},
    },
}


# ---------------------------------------------------------------- house sensors


@pytest.mark.parametrize(
    ("url", "ok"),
    [
        ("http://192.168.1.50:8080", True),
        ("http://10.0.0.5", True),
        ("http://100.100.1.2:8080", True),  # tailnet (CGNAT range)
        ("http://core.local:8080/", True),
        ("http://8.8.8.8", False),
        ("https://example.com", False),
        ("http://127.0.0.1:7440", False),  # never Ultra itself
        ("http://192.168.1.50:8080/api/x", False),
        ("http://user:pw@192.168.1.50", False),
        ("ftp://192.168.1.50", False),
        ("not a url", False),
    ],
)
def test_only_private_network_urls(url, ok):
    assert private_url(url) is ok


def test_lines_from_the_documents():
    a = _lines(DOCS)
    assert [x["text"] for x in a["animals"]] == [
        "coop 42\u00b0F",
        "waterer ice risk low",
        "night sentry armed",
    ]
    assert [x["text"] for x in a["home"]] == ["outside 49\u00b0F", "basement sensor stale 24 h"]
    assert a["garden"] == [{"text": "near freezing in 3 days, low 33\u00b0F", "level": "warn"}]


def test_weather_key_picks_the_place():
    docs = {"/api/status": {"weather": {"a": {"temp_f": 60.0}, "b": {"temp_f": 40.0}}}}
    assert _lines(docs)["home"][0]["text"] == "outside 60\u00b0F"
    assert _lines(docs, "b")["home"][0]["text"] == "outside 40\u00b0F"
    stale = {"/api/status": {"weather": {"b": {"temp_f": 40.0, "stale": True}}}}
    assert _lines(stale, "b") == {}


def test_freeze_cold_coop_and_unarmed_sentry_are_flagged():
    docs = {
        "/api/freeze": {"min_temp_24h": 30.0, "min_temp_48h": 28.0, "min_temp_72h": 27.0},
        "/api/coop/thermal": {"roost_interior_temp_f": 33.0, "waterer_ice_risk": "HIGH"},
        "/api/coop": {"is_past_sunset": True, "master_armed": False},
    }
    a = _lines(docs)
    hard = {"text": "freeze within 24 h, low 30\u00b0F", "level": "bad"}
    assert a["home"] == [hard] and a["garden"] == [hard]
    lv = {x["text"]: x["level"] for x in a["animals"]}
    assert lv == {
        "coop 33\u00b0F": "warn",
        "waterer ice risk high": "bad",
        "night sentry NOT armed": "bad",
    }


def test_odd_values_never_reach_the_page():
    docs = {
        "/api/coop/thermal": {"roost_interior_temp_f": "<script>", "waterer_ice_risk": "<b>x</b>"},
        "/api/freeze": {"min_temp_72h": True},
        "/api/status": {"weather": "nope", "sensors": []},
        "/api/coop": {"is_past_sunset": "yes"},
    }
    assert _lines(docs) == {}


def test_readings_cache_partial_failure_and_area_map():
    calls: list[str] = []

    def fetch(p: str) -> Any:
        calls.append(p)
        if p == "/api/status":
            raise OSError("down")
        return DOCS[p]

    h = House("http://192.168.1.50:8080", fetch=fetch, areas={"animals": "flock"})
    r = h.readings()
    assert r["ok"] is True and "flock" in r["areas"] and "animals" not in r["areas"]
    assert "home" not in r["areas"]  # its only document (status) failed: no line, not a guess
    h.readings()
    assert calls == list(PATHS)  # second read came from the cache


def test_all_down_says_unreachable_and_public_url_is_refused():
    def down(p: str) -> Any:
        raise OSError("no route")

    r = House("http://192.168.1.50:8080", fetch=down).readings()
    assert r["ok"] is False and r["error"] == "sensors unreachable" and r["areas"] == {}
    pub = House("https://example.com", fetch=lambda p: DOCS[p])
    assert pub.enabled is False and pub.readings()["ok"] is False
    with pytest.raises(ValueError):
        House("http://192.168.1.50")._fetch("/api/cameras/arm")  # only the four read paths


def test_house_api_off_and_on():
    assert HouseApi(None).r_house({}, None, None) == {"enabled": False}  # type: ignore[arg-type]
    h = House("http://192.168.1.50:8080", fetch=lambda p: DOCS[p])
    assert HouseApi(h).r_house({}, None, None)["enabled"] is True  # type: ignore[arg-type]
    cfg = SimpleNamespace(get=lambda s, k, d=None: {"url": "http://192.168.1.50:8080"}.get(k, d))
    assert House.from_config(cfg) is not None


# ---------------------------------------------------------------- tick done


class TickVault:
    enabled = True
    writes = True
    name = "Notes"

    def __init__(self) -> None:
        self.today_fn = lambda: dt.date(2026, 10, 3)
        self.written: list[tuple[str, dict[str, Any]]] = []

    def _hidden(self, path: str) -> bool:
        return False

    def _call(self, tool: str, args: dict[str, Any], ttl: float = 60) -> Any:
        if tool == "vault_tasks":
            return {
                "results": [
                    {
                        "path": "01 - Hubs/Tasks and Todos.md",
                        "line": 8,
                        "text": f"Garlic: buy seed {CAL} 2026-10-01",
                    }
                ]
            }
        if tool == "vault_day":
            return {}
        if tool in ("vault_recent", "vault_read"):
            raise VaultError(400, "none")
        raise AssertionError(tool)

    def write(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        self.written.append((tool, args))
        return {"path": args["path"], "commit": "abc1234", "line": args["line"]}


def _life(v: Any) -> Life:
    return Life(v)


def test_tick_uses_a_single_use_token_and_sends_the_raw_line():
    v = TickVault()
    life = _life(v)
    t = life.overview()["due"][0]
    assert "raw" not in t and "path" in t and t["done_token"]
    api = LifeApi(life)
    out = api.r_done({}, {"token": t["done_token"]}, None)  # type: ignore[arg-type]
    assert out == {
        "ok": True,
        "path": "01 - Hubs/Tasks and Todos.md",
        "commit": "abc1234",
        "line": 8,
    }
    assert v.written == [
        (
            "vault_task_done",
            {
                "path": "01 - Hubs/Tasks and Todos.md",
                "line": 8,
                "text": f"Garlic: buy seed {CAL} 2026-10-01",
            },
        )
    ]
    with pytest.raises(ApiError, match="reload"):
        api.r_done({}, {"token": t["done_token"]}, None)  # type: ignore[arg-type]
    for bad in ("", "x", "../../etc", "a" * 50):
        with pytest.raises(ApiError):
            api.r_done({}, {"token": bad}, None)  # type: ignore[arg-type]
    assert len(v.written) == 1


def test_no_tokens_when_writes_are_off():
    v = TickVault()
    v.writes = False
    assert "done_token" not in _life(v).overview()["due"][0]


def test_ui_wiring():
    js = (ROOT / "src/ultra/static/life.js").read_text()
    assert 'api("/api/life/done", { method: "POST", body: { token: btn.dataset.done } })' in js
    assert "confirm(`Mark done?" in js and 'api("/api/life/house")' in js
    assert "esc(l.text)" in js


VAULT_MCP = shutil.which("vault-mcp") or str(Path.home() / ".local/bin/vault-mcp")


@pytest.mark.skipif(not Path(VAULT_MCP).exists(), reason="vault-mcp is not installed")
def test_tick_end_to_end_with_the_real_vault_mcp(tmp_path: Path):
    notes = tmp_path / "Notes"
    (notes / "01 - Hubs").mkdir(parents=True)
    tasks = notes / "01 - Hubs/Tasks and Todos.md"
    tasks.write_text(
        f"# Tasks\n\n## ACTIVE\n\n- [ ] Garlic: buy seed {CAL} 2026-10-01\n- [ ] Latch\n"
    )
    for a in (
        ["init", "-q"],
        ["add", "-A"],
        ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "i"],
    ):
        subprocess.run(["git", "-C", str(notes), *a], check=True, capture_output=True)
    from ultra.config import Config

    cfg = Config(
        {
            "vault": {
                "path": str(notes),
                "name": "Notes",
                "command": VAULT_MCP,
                "author": "Test Operator",
            }
        }
    )
    client = client_from_config(cfg)
    assert client is not None
    try:
        v = Vault(cfg, client, today_fn=dt.date.today)
        life = Life(v)
        due = life.overview()["due"]
        tok = next(t["done_token"] for t in due if t["text"].startswith("Garlic"))
        LifeApi(life).r_done({}, {"token": tok}, None)  # type: ignore[arg-type]
        body = tasks.read_text()
        assert re.search(
            r"- \[x\] Garlic: buy seed .* \u2705 \d{4}-\d{2}-\d{2}\n- \[ \] Latch\n$", body
        )
        log = subprocess.run(
            ["git", "-C", str(notes), "log", "-1", "--format=%an|%s"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        assert log.startswith("Test Operator (via Ultra)|Done: Garlic")
        # a stale token for a line that changed underneath is refused by the server
        tok2 = life._with_ticks(
            [{"path": "01 - Hubs/Tasks and Todos.md", "line": 6, "text": "Latch", "raw": "Latch"}]
        )[0]["done_token"]
        tasks.write_text(tasks.read_text().replace("- [ ] Latch\n", "- [ ] Something else\n"))
        with pytest.raises(ApiError):
            LifeApi(life).r_done({}, {"token": tok2}, None)  # type: ignore[arg-type]
        assert "- [ ] Something else" in tasks.read_text()
    finally:
        client.close()
