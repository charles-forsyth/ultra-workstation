"""House sensors on Life (SPEC 8.11, v1.14): a read-only look at a home dashboard.

Some homes run a small sensor dashboard on the LAN. When a workspace's config has
`[house] url = "http://<host>:<port>"`, Life asks it for a few JSON documents and shows
a short line on the matching area tiles:

- `/api/coop/thermal`  roost and outdoor temperature, waterer ice risk   -> animals
- `/api/freeze`        lowest temperature in 24/48/72 h  -> garden (home on a hard freeze)
- `/api/coop`          whether the night sentry is armed                 -> animals
- `/api/status`        outdoor weather, and any sensor that went stale    -> home

`[house] areas = { home = "<area key>", ... }` maps these groups onto the workspace's own
Life areas (life.toml) when their keys differ; `weather_key` picks the place in the
status document's weather block (default: the first one with a temperature).

Only GETs, only those four paths, a 4-second timeout, cached for two minutes, and a
failure is shown as "sensors unreachable", never as a reading. The URL must be on a
private network (10/8, 172.16/12, 192.168/16, 100.64/10 CGNAT for a tailnet, or a
`.local` name); anything else is refused, so a config typo cannot send the browser's
session to the internet. Values are reduced to numbers and short words before they
reach the page.
"""

from __future__ import annotations

import ipaddress
import json
import re
import threading
import time
import urllib.request
from typing import Any
from urllib.parse import urlparse

PATHS = ("/api/coop/thermal", "/api/freeze", "/api/coop", "/api/status")
TTL = 120
TIMEOUT = 4.0
WORD_RE = re.compile(r"^[A-Z_]{2,40}$")


def private_url(url: str) -> bool:
    try:
        u = urlparse(url)
    except ValueError:
        return False
    if u.scheme not in ("http", "https") or not u.hostname or u.username or u.password:
        return False
    if u.path not in ("", "/") or u.query or u.fragment:
        return False
    host = u.hostname
    if host.endswith(".local"):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    cgnat = ipaddress.ip_network("100.64.0.0/10")
    return (ip.is_private and not ip.is_loopback) or ip in cgnat


def _num(v: Any) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return round(float(v), 1)


def _word(v: Any) -> str:
    """An upper-case status word like CHILL_ADVISORY, or ''."""
    s = str(v or "")
    return s if WORD_RE.match(s) else ""


def _human(word: str) -> str:
    return word.replace("_", " ").lower()


class House:
    def __init__(
        self,
        url: str,
        fetch: Any = None,
        areas: dict[str, str] | None = None,
        weather_key: str = "",
    ):
        self.url = url.rstrip("/")
        self.weather_key = weather_key
        # sensor group (home, animals, garden) -> the Life area key that shows it
        self.areas = areas or {}
        self.enabled = private_url(url)
        self.fetch = fetch or self._fetch
        self.lock = threading.Lock()
        self.cache: tuple[float, dict[str, Any]] | None = None

    @classmethod
    def from_config(cls, cfg: Any) -> House | None:
        url = str(cfg.get("house", "url", "") or "").strip()
        if not url or cfg.get("house", "enabled", True) is False:
            return None
        amap = cfg.get("house", "areas", {}) or {}
        areas = {str(k): str(v) for k, v in amap.items()} if isinstance(amap, dict) else {}
        wkey = str(cfg.get("house", "weather_key", "") or "")
        return cls(url, areas=areas, weather_key=wkey)

    def _fetch(self, path: str) -> Any:
        if path not in PATHS:
            raise ValueError("not a path Ultra reads")
        req = urllib.request.Request(  # noqa: S310 - http(s) on a private host, checked
            self.url + path, headers={"Accept": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:  # noqa: S310 - private URL, checked
            return json.loads(r.read(2_000_000))

    def readings(self) -> dict[str, Any]:
        """Per-area lines for the Life tiles: {"ok": bool, "areas": {key: [line, ...]}}."""
        if not self.enabled:
            return {"ok": False, "error": "[house] url is not on a private network", "areas": {}}
        now = time.time()
        with self.lock:
            if self.cache and now - self.cache[0] < TTL:
                return self.cache[1]
        got: dict[str, Any] = {}
        errors = 0
        for p in PATHS:
            try:
                got[p] = self.fetch(p)
            except Exception:  # noqa: BLE001 - one missing document must not hide the rest
                errors += 1
        lines = _lines(got, self.weather_key)
        mapped: dict[str, list[dict[str, str]]] = {}
        for group, rows in lines.items():
            mapped.setdefault(self.areas.get(group, group), []).extend(rows)
        out = {"ok": errors < len(PATHS), "areas": mapped, "at": int(now)}
        if errors == len(PATHS):
            out["error"] = "sensors unreachable"
        with self.lock:
            self.cache = (now, out)
        return out


def _lines(got: dict[str, Any], weather_key: str = "") -> dict[str, list[dict[str, str]]]:
    """Short, already-escaped-safe lines (numbers and fixed words only) per area.
    Each line: {"text", "level"} where level is ok | warn | bad."""
    areas: dict[str, list[dict[str, str]]] = {}

    def add(area: str, text: str, level: str = "ok") -> None:
        areas.setdefault(area, []).append({"text": text, "level": level})

    th = got.get("/api/coop/thermal")
    if isinstance(th, dict):
        roost = _num(th.get("roost_interior_temp_f"))
        ice = _word(th.get("waterer_ice_risk"))
        if roost is not None:
            add("animals", f"coop {roost:.0f}\u00b0F", "warn" if roost <= 34 else "ok")
        if ice:
            add(
                "animals",
                f"waterer ice risk {_human(ice)}",
                "ok" if ice == "LOW" else "warn" if ice in ("MODERATE", "MEDIUM") else "bad",
            )
    coop = got.get("/api/coop")
    if isinstance(coop, dict) and coop.get("is_past_sunset") is True:
        armed = coop.get("master_armed") is True
        add(
            "animals",
            "night sentry armed" if armed else "night sentry NOT armed",
            "ok" if armed else "bad",
        )
    fz = got.get("/api/freeze")
    if isinstance(fz, dict):
        lows = [(h, _num(fz.get(f"min_temp_{h}h"))) for h in (24, 48, 72)]
        first = next(((h, t) for h, t in lows if t is not None and t <= 32), None)
        low72 = lows[2][1]
        if first:
            line, lvl = f"freeze within {first[0]} h, low {first[1]:.0f}\u00b0F", "bad"
        elif low72 is not None and low72 <= 36:
            line, lvl = f"near freezing in 3 days, low {low72:.0f}\u00b0F", "warn"
        elif low72 is not None:
            line, lvl = f"3-day low {low72:.0f}\u00b0F", "ok"
        else:
            line, lvl = "", "ok"
        if line:  # frost matters to the garden; the house tile only hears a real freeze
            add("garden", line, lvl)
            if lvl == "bad":
                add("home", line, lvl)
    st = got.get("/api/status")
    if isinstance(st, dict):
        wx: dict[str, Any] = st["weather"] if isinstance(st.get("weather"), dict) else {}
        # the dashboard keys weather by place; [house] weather_key names it, else the
        # first place that has a temperature
        w = wx.get(weather_key) if weather_key else None
        if not isinstance(w, dict):
            w = next((v for v in wx.values() if isinstance(v, dict) and "temp_f" in v), {})
        t = _num(w.get("temp_f")) if isinstance(w, dict) else None
        if t is not None and not (isinstance(w, dict) and w.get("stale") is True):
            add("home", f"outside {t:.0f}\u00b0F", "ok")
        sens = st.get("sensors") if isinstance(st.get("sensors"), dict) else {}
        amb = sens.get("hardware_ambient") if isinstance(sens, dict) else None
        if isinstance(amb, dict) and amb.get("stale") is True:
            hrs = _num(amb.get("age_min"))
            add("home", f"basement sensor stale{f' {hrs / 60:.0f} h' if hrs else ''}", "warn")
    return areas


class HouseApi:
    """GET /api/life/house: the tile lines (or {"enabled": false})."""

    def __init__(self, house: House | None):
        self.house = house

    def register(self, api: Any) -> None:
        api.add("GET", r"/api/life/house", self.r_house)

    def r_house(self, q: dict, body: Any, m: re.Match[str]) -> dict:
        if self.house is None:
            return {"enabled": False}
        return {"enabled": True, **self.house.readings()}
