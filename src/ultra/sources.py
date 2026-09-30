"""Draft Studio sources (SPEC 9.6): policy pages and house facts.

Policy sources are the operator's private list (config `[draft] sources`); nothing in
this module names a real site. Kinds:

- ``page``: one fixed URL, fetched as text.
- ``site``: a public site; candidate pages come from its sitemap.xml, ranked by how many
  of the item's terms appear in the URL path, then the best few are fetched.
- ``servicenow_kb``: a ServiceNow portal's public knowledge base. Articles are read
  without login through the portal page API; finding them needs the web search tool
  (guest search through the portal returns nothing), so the caller passes candidate
  article ids.

Every fetch is a plain HTTPS GET with a 20 s timeout, cached 24 h in kv_cache with the
fetch time. A source that fails is reported as failed, never silently skipped.

House facts are the operator's settled answers, stored locally (SQLite), added only by
the operator. The relevant ones (topic words overlap the item) go into briefs and drafts.
"""

from __future__ import annotations

import html
import json
import re
import sqlite3
import time
import urllib.parse
import urllib.request
from typing import Any

from ultra.store import Store

UA = "Mozilla/5.0 (X11; Linux x86_64) ultra-workstation"
TTL_PAGE = 24 * 3600
TTL_SITEMAP = 7 * 24 * 3600
MAX_PAGE_CHARS = 200_000
STOP = set(
    """a an and are as at be but by can could did do does for from had has have how i if in
    into is it its just me my no not of on or our please re so than that the their them then
    there these they this to up us was we were what when where which who why will with would
    you your hi hello thanks thank regards best dear""".split()
)

_SITEMAP_LOC = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.I)
_SKIP_PATH = re.compile(
    r"/(image|images|file|files|document|documents|media)/|\.(png|jpe?g|gif|pdf|svg)$", re.I
)


class SourceError(Exception):
    pass


# Words that say nothing about what a request is about: generic workplace, courtesy and
# letter-structure words. Distinctive nouns (llm, gemini, grant, dua, storage) survive.
GENERIC = set(
    """research project projects access guidance institutional whether advise appreciate
    appropriate available currently considering depend involves using used use need needs
    question questions help support supports team work working university california
    department associate assistant professor dear best regards thank thanks very much
    also would could should like want wanted know let get got make made time today week
    email mail reply message sent send following particular setup set info information
    please regarding re fw fwd new one two three first next last computing assigned
    choice option options provide provided within across about into more most such""".split()
)


def terms(text: str, limit: int = 40) -> list[str]:
    """Content words, most frequent first (for ranking pages and picking passages)."""
    words = re.findall(r"[a-z][a-z0-9+-]{2,}", (text or "").lower())
    counts: dict[str, int] = {}
    for w in words:
        if w not in STOP:
            counts[w] = counts.get(w, 0) + 1
    return [w for w, _ in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))][:limit]


# Workplace-specific filler (the campus name, the department's own name) comes from the
# private config ([draft] generic_words), never from this public file.
EXTRA_GENERIC: set[str] = set()


def set_extra_generic(words: list[str]) -> None:
    EXTRA_GENERIC.clear()
    EXTRA_GENERIC.update(w.lower() for w in words if isinstance(w, str))


def key_terms(text: str, limit: int = 10, drop: set[str] | None = None) -> list[str]:
    """The words that say what a message is about: content words minus generic ones and
    minus ``drop`` (the participants' own names), most frequent first."""
    drop = drop or set()
    skip = GENERIC | EXTRA_GENERIC | drop
    out = [w for w in terms(text, 80) if w not in skip]
    return out[:limit]


def html_text(h: str) -> tuple[str, str]:
    """(title, readable text) from HTML: scripts, styles, nav, header, footer dropped."""
    m = re.search(r"(?is)<title[^>]*>(.*?)</title>", h)
    title = html.unescape(re.sub(r"\s+", " ", m.group(1))).strip() if m else ""
    body = re.sub(r"(?is)<(script|style|noscript|nav|header|footer|svg|form)[^>]*>.*?</\1>", " ", h)
    body = re.sub(r"(?i)<(br|/p|/div|/li|/h[1-6]|/tr)[^>]*>", "\n", body)
    body = re.sub(r"(?s)<[^>]+>", " ", body)
    body = html.unescape(body)
    body = re.sub(r"[ \t\r\f\v]+", " ", body)
    body = re.sub(r"\n\s*\n+", "\n", body).strip()
    return title, body[:MAX_PAGE_CHARS]


def passages(text: str, words: list[str], width: int = 420, limit: int = 4) -> list[str]:
    """The windows of text densest in the given words, in page order, non-overlapping."""
    if not text or not words:
        return []
    low = text.lower()
    hits: list[int] = []
    for w in words[:20]:
        start = 0
        while True:
            i = low.find(w, start)
            if i < 0 or len(hits) > 400:
                break
            hits.append(i)
            start = i + len(w)
    if not hits:
        return []
    hits.sort()
    scored: list[tuple[int, int]] = []
    for h in hits:
        a = max(0, h - width // 3)
        b = a + width
        score = sum(1 for w in words[:20] if w in low[a:b])
        scored.append((score, a))
    scored.sort(key=lambda x: (-x[0], x[1]))
    chosen: list[int] = []
    for _score, a in scored:
        if all(abs(a - c) >= width for c in chosen):
            chosen.append(a)
        if len(chosen) >= limit:
            break
    out = []
    for a in sorted(chosen):
        seg = text[a : a + width]
        # trim to whole words at both ends
        seg = re.sub(r"^\S*\s", "", seg) if a else seg
        seg = re.sub(r"\s\S*$", "", seg)
        out.append(" ".join(seg.split()))
    return out


class Sources:
    """Fetch and cache the operator's policy pages; pick the relevant passages."""

    def __init__(self, cfg_sources: list[dict[str, Any]], store: Store, fetch: Any = None):
        self.sources = [s for s in cfg_sources if isinstance(s, dict) and s.get("url")]
        self.store = store
        self._fetch = fetch or self._http_get

    # ---------------------------------------------------------------- fetching
    @staticmethod
    def _http_get(url: str) -> str:
        if not url.startswith("https://"):
            raise SourceError(f"only https sources are fetched: {url}")
        # https only (checked above), so no file: or custom schemes
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})  # noqa: S310
        with urllib.request.urlopen(req, timeout=20) as r:  # noqa: S310
            raw = r.read(3_000_000)
            charset = r.headers.get_content_charset() or "utf-8"
        return raw.decode(charset, errors="replace")

    def page(self, url: str, fresh: bool = False) -> dict[str, Any]:
        """{url, title, text, fetched_at} or {url, error}. Cached 24 h."""
        ck = "src:page:" + url
        if not fresh:
            hit = self.store.cache_get(ck, TTL_PAGE)
            if hit:
                return hit[0]
        try:
            h = self._fetch(url)
            title, text = html_text(h)
            out = {"url": url, "title": title, "text": text, "fetched_at": time.time()}
        except Exception as e:  # noqa: BLE001 - reported to the operator, not hidden
            return {"url": url, "error": str(e)[:200]}
        self.store.cache_put(ck, out)
        return out

    def sitemap(self, base: str) -> list[str]:
        ck = "src:sitemap:" + base
        hit = self.store.cache_get(ck, TTL_SITEMAP)
        if hit:
            return list(hit[0])
        root = base.rstrip("/") + "/sitemap.xml"
        urls: list[str] = []
        try:
            x = self._fetch(root)
            locs = _SITEMAP_LOC.findall(x)
            nested = [u for u in locs if u.endswith(".xml")][:10]
            urls = [u for u in locs if not u.endswith(".xml")]
            for n in nested:
                try:
                    urls += [
                        u for u in _SITEMAP_LOC.findall(self._fetch(n)) if not u.endswith(".xml")
                    ]
                except Exception:  # noqa: BLE001, S112 - one bad sub-sitemap is skipped
                    continue
        except Exception as e:
            raise SourceError(f"sitemap unavailable for {base}: {str(e)[:120]}") from e
        host = urllib.parse.urlparse(base).netloc
        urls = sorted(
            {
                u
                for u in urls
                if urllib.parse.urlparse(u).netloc == host and not _SKIP_PATH.search(u)
            }
        )
        self.store.cache_put(ck, urls)
        return urls

    def kb_article(self, portal: str, sys_id: str, portal_id: str) -> dict[str, Any]:
        """A public ServiceNow KB article through the portal page API (no login)."""
        if not re.fullmatch(r"[0-9a-f]{32}", sys_id):
            return {"url": portal, "error": "bad article id"}
        api = (
            f"{portal.rstrip('/')}/api/now/sp/page?id=kb_article&sys_id={sys_id}"
            f"&portal_id={urllib.parse.quote(portal_id)}"
        )
        view = f"{portal.rstrip('/')}/{portal_id}?id=kb_article&sys_id={sys_id}"
        ck = "src:kb:" + sys_id
        hit = self.store.cache_get(ck, TTL_PAGE)
        if hit:
            return hit[0]
        try:
            d = json.loads(self._fetch(api))
        except Exception as e:  # noqa: BLE001
            return {"url": view, "error": str(e)[:200]}
        art = _find_article(d)
        if not art or not art.get("text"):
            return {"url": view, "error": "article not public or not found"}
        _t, text = html_text(art["text"])
        out = {
            "url": view,
            "title": f"{art.get('number', '')} {art.get('short_description', '')}".strip(),
            "text": text,
            "fetched_at": time.time(),
        }
        self.store.cache_put(ck, out)
        return out

    # ---------------------------------------------------------------- choosing
    def relevant(self, item_text: str, max_pages: int = 5) -> dict[str, Any]:
        """Pages and passages relevant to an item.

        Returns {"passages": [{url, title, text, fetched_at}], "failed": [{url, error}],
        "considered": n}. ``page`` sources are always read; ``site`` sources contribute
        their best-ranked sitemap pages.
        """
        words = key_terms(item_text, 20) or terms(item_text)
        if not words:
            return {"passages": [], "failed": [], "considered": 0}
        # Short topic words ("ai", "it", "hpc" is 3) never make it into terms(); URL
        # paths use them a lot, so they count for page ranking when the item has them.
        low_item = (item_text or "").lower()
        shorts = [
            w
            for w in ("ai", "ml", "gpu", "hpc", "vpn", "mfa", "irb", "dua", "p3", "p4")
            if re.search(rf"\b{w}\b", low_item) or (w == "ai" and "llm" in low_item)
        ]
        rank_words = shorts + [w for w in words if w not in shorts]
        cands: list[tuple[int, str]] = []
        failed: list[dict[str, str]] = []
        for s in self.sources:
            kind = s.get("kind", "page")
            if kind == "page":
                cands.append((100, str(s["url"])))
            elif kind == "site":
                try:
                    for u in self.sitemap(str(s["url"])):
                        path = (
                            urllib.parse.urlparse(u)
                            .path.lower()
                            .replace("-", " ")
                            .replace("/", " ")
                        )
                        score = sum(
                            3 if w in path.split() else (1 if len(w) > 3 and w in path else 0)
                            for w in rank_words[:18]
                        )
                        if score:
                            cands.append((score, u))
                except SourceError as e:
                    failed.append({"url": str(s["url"]), "error": str(e)})
        cands.sort(key=lambda x: (-x[0], x[1]))
        seen: set[str] = set()
        picked = []
        for _score, u in cands:
            if u not in seen:
                seen.add(u)
                picked.append(u)
            if len(picked) >= max_pages + sum(
                1 for s in self.sources if s.get("kind", "page") == "page"
            ):
                break
        out = []
        for u in picked:
            p = self.page(u)
            if p.get("error"):
                failed.append({"url": u, "error": p["error"]})
                continue
            ps = passages(p["text"], words)
            if ps:
                out.append(
                    {
                        "url": u,
                        "title": p.get("title", ""),
                        "text": "\n...\n".join(ps),
                        "fetched_at": p.get("fetched_at"),
                    }
                )
        return {"passages": out[:max_pages], "failed": failed, "considered": len(cands)}


def _find_article(o: Any) -> dict[str, Any] | None:
    """The widget data holding a KB article (has number and text) in a portal page."""
    if isinstance(o, dict):
        if (
            isinstance(o.get("text"), str)
            and isinstance(o.get("number"), str)
            and o["number"].startswith("KB")
        ):
            return o
        for v in o.values():
            r = _find_article(v)
            if r:
                return r
    elif isinstance(o, list):
        for v in o:
            r = _find_article(v)
            if r:
                return r
    return None


# ---------------------------------------------------------------- house facts
FACTS_SQL = """
CREATE TABLE IF NOT EXISTS house_facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    text TEXT NOT NULL,
    topics TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1,
    created REAL NOT NULL,
    updated REAL NOT NULL
);
"""


class HouseFacts:
    """The operator's settled answers. Added and edited only from the operator's UI."""

    def __init__(self, store: Store):
        self.store = store
        with self._db() as c:
            c.executescript(FACTS_SQL)

    def _db(self) -> sqlite3.Connection:
        return self.store._conn()

    def all(self, include_disabled: bool = True) -> list[dict[str, Any]]:
        q = "SELECT id, text, topics, source, enabled, created, updated FROM house_facts"
        if not include_disabled:
            q += " WHERE enabled = 1"
        rows = self._db().execute(q + " ORDER BY id").fetchall()
        return [
            {
                "id": r[0],
                "text": r[1],
                "topics": r[2],
                "source": r[3],
                "enabled": bool(r[4]),
                "created": r[5],
                "updated": r[6],
            }
            for r in rows
        ]

    def add(self, text: str, topics: str = "", source: str = "operator") -> dict[str, Any]:
        text = " ".join((text or "").split())
        if not 3 <= len(text) <= 600:
            raise ValueError("a house fact is 3 to 600 characters")
        # Blank topics = the fact always applies (a general rule). The operator chooses.
        topics = " ".join((topics or "").lower().split())[:300]
        now = time.time()
        with self._db() as c:
            cur = c.execute(
                "INSERT INTO house_facts (text, topics, source, enabled, created, updated) "
                "VALUES (?,?,?,?,?,?)",
                (text, topics, source[:120], 1, now, now),
            )
            fid = int(cur.lastrowid or 0)
        return next(f for f in self.all() if f["id"] == fid)

    def update(self, fid: int, **kw: Any) -> dict[str, Any]:
        f = next((x for x in self.all() if x["id"] == fid), None)
        if not f:
            raise KeyError("no such house fact")
        text = " ".join(str(kw.get("text", f["text"])).split())
        if not 3 <= len(text) <= 600:
            raise ValueError("a house fact is 3 to 600 characters")
        topics = " ".join(str(kw.get("topics", f["topics"])).lower().split())[:300]
        enabled = int(bool(kw.get("enabled", f["enabled"])))
        with self._db() as c:
            c.execute(
                "UPDATE house_facts SET text=?, topics=?, enabled=?, updated=? WHERE id=?",
                (text, topics, enabled, time.time(), fid),
            )
        return next(x for x in self.all() if x["id"] == fid)

    def delete(self, fid: int) -> None:
        with self._db() as c:
            c.execute("DELETE FROM house_facts WHERE id=?", (fid,))

    def relevant(self, item_text: str, limit: int = 12) -> list[dict[str, Any]]:
        """Enabled facts whose topic words appear in the item; facts with no topics
        always apply (general rules like 'no dollar figures unless asked')."""
        low = (item_text or "").lower()
        words = set(re.findall(r"[a-z][a-z0-9+-]{2,}", low))
        out = []
        for f in self.all(include_disabled=False):
            ts = [t for t in f["topics"].split() if t]
            if not ts:
                out.append((1, f))
                continue
            hit = sum(1 for t in ts if t in words or (" " in t and t in low))
            if hit:
                out.append((hit + 1, f))
        out.sort(key=lambda x: (-x[0], x[1]["id"]))
        return [f for _s, f in out[:limit]]
