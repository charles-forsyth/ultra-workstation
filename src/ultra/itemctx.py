"""Item context (v0.8): everything the ledger knows about ONE stream item's group.

A stream item (email thread, Slack conversation, ticket, task) names people and things
in its headers AND in its bodies. This module:

1. Scans the whole item (every message body, not just headers) for ledger entities:
   - people: header addresses, email addresses and netid@ in bodies, and full names
     that match a ledger person exactly (nickname pairs allowed: Mike/Michael);
   - labs, GCP project IDs, research projects, grants (by number), assets;
   Matching is exact and word-bounded. Anything fuzzier becomes a *suggestion*
   (possible match, shown for the operator to confirm) and never enters the context
   until confirmed.
2. Builds the group: the first 10 people by relevance (senders, then named in the
   body, then To, then Cc) get a dossier; everyone else is listed without one.
   Labs / projects / grants / GCP projects get `tree`.
3. Merges it: one de-duplicated history (every ledger interaction touching any of
   them, plus this item's own messages), open tasks for the group, and the entities.

Reads only. Uses the ledger adapter's allow-listed `--json` read commands.
"""

from __future__ import annotations

import email.utils
import hashlib
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from ultra.rules import Rules

MAX_DOSSIERS = 10
MAX_TREES = 12
BROAD = 40  # a named thing with more links than this (HPCC, CephRDS) is a platform:
# list it, but don't merge its whole history into this item's context
TTL_CATALOG = 6 * 3600  # names of every person/lab/project/... (the whole ledger)
TTL_ITEM = 900

# Formal/informal given names, mirroring the ledger's own identity table.
GIVEN_ALIASES = [
    ("michael", "mike"),
    ("christopher", "chris"),
    ("charles", "chuck"),
    ("robert", "bob"),
    ("robert", "rob"),
    ("william", "bill"),
    ("william", "will"),
    ("richard", "rick"),
    ("richard", "dick"),
    ("james", "jim"),
    ("joseph", "joe"),
    ("thomas", "tom"),
    ("daniel", "dan"),
    ("david", "dave"),
    ("steven", "steve"),
    ("stephen", "steve"),
    ("matthew", "matt"),
    ("anthony", "tony"),
    ("edward", "ed"),
    ("nicholas", "nick"),
    ("jonathan", "jon"),
    ("benjamin", "ben"),
    ("samuel", "sam"),
    ("alexander", "alex"),
    ("andrew", "andy"),
    ("elizabeth", "liz"),
    ("elizabeth", "beth"),
    ("katherine", "kate"),
    ("katherine", "kathy"),
    ("jennifer", "jen"),
    ("rebecca", "becky"),
    ("patricia", "pat"),
    ("susan", "sue"),
    ("margaret", "maggie"),
]

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
GCP_ID_RE = re.compile(r"(?<![\w-])([a-z][a-z0-9-]{4,28}[a-z0-9])(?![\w-])")
GRANT_RE = re.compile(r"(?<![\w-])((?:[A-Z]{2,6}-)?\d{6,8}(?:-\d{1,4})?)(?![\w-])")
WORD = r"(?<![\w-]){}(?![\w-])"
STOP_ENTITY = {  # asset / project names too common to match on their own
    "other",
    "none",
    "test",
    "data",
    "lab",
    "server",
    "storage",
    "home",
    "scratch",
    "backup",
    "cloud",
    "gpu",
    "cpu",
    "web",
    "email",
    "slack",
    "project",
    "the",
}


def _strip_paren(name: str) -> str:
    return re.sub(r"\s*\([^)]*\)\s*$", "", name or "").strip()


def _variants(full: str) -> list[str]:
    """'Michael Kent' -> ['michael kent', 'mike kent']."""
    parts = full.lower().split()
    if len(parts) < 2:
        return []
    out = {" ".join(parts)}
    for formal, short in GIVEN_ALIASES:
        for a, b in ((formal, short), (short, formal)):
            if parts[0] == a:
                out.add(" ".join([b, *parts[1:]]))
    return sorted(out)


ORG_WORDS = re.compile(
    r"\b(dept|department|school|college|office|division|services?|university|campus|"
    r"program|operations|ops|board|center|centre|institute|group|team|support|"
    r"partnerships|library|engineering|sciences?|studies|biology|chemistry|physics|"
    r"mathematics|statistics|economics|history|business|iam|pmo|it)\b",
    re.I,
)


def is_research_lab(name: str) -> bool:
    """A named research lab ("Boris Baer Lab (borisbar)"), not an org unit.

    Org units (departments, colleges, IT teams) are ledger Labs too, but naming one in
    an email ("Statistics", "Research Computing") says nothing about who the item is
    about, and pulling its tree would drag in hundreds of people.
    """
    base = _strip_paren(name)
    if re.search(r"\blab(oratory)?\b", base, re.I) or re.search(r"\([a-z0-9._-]+\)\s*$", name):
        return True
    return not ORG_WORDS.search(base) and False


class Catalog:
    """Names of every ledger entity, for exact matching. Rebuilt every few hours."""

    def __init__(self, data: dict[str, list[dict[str, Any]]], me: set[str] | None = None):
        me = {m.lower() for m in (me or set())}
        self.people = [
            p
            for p in data.get("people") or []
            if p.get("netid") and str(p["netid"]).lower() not in me
        ]
        self.labs = data.get("labs") or []
        self.gcp = data.get("gcp") or []
        self.projects = data.get("projects") or []
        self.grants = data.get("grants") or []
        self.assets = data.get("assets") or []
        # full-name -> [netid...] (a name two people share is ambiguous: suggestion)
        self.by_name: dict[str, list[str]] = {}
        self.exact: set[tuple[str, str]] = set()  # (name as written in the ledger, netid)
        self.netids: dict[str, dict[str, Any]] = {}
        self.last_names: dict[str, list[str]] = {}
        for p in self.people:
            netid = str(p["netid"]).lower()
            self.netids[netid] = p
            nm = _strip_paren(str(p.get("name") or ""))
            if len(nm.split()) >= 2:
                self.exact.add((" ".join(nm.lower().split()), netid))
            for v in _variants(nm):
                self.by_name.setdefault(v, [])
                if netid not in self.by_name[v]:
                    self.by_name[v].append(netid)
            parts = nm.lower().split()
            if len(parts) >= 2 and len(parts[-1]) >= 4:
                self.last_names.setdefault(parts[-1], []).append(netid)
        self._name_re = self._alternation(self.by_name)
        self._last_re = self._alternation(self.last_names)
        self.gcp_ids = {
            str(g.get("project_id", "")).lower(): g for g in self.gcp if g.get("project_id")
        }
        self.lab_names: dict[str, str] = {}
        for lab in self.labs:
            n = str(lab.get("name") or "")
            base = _strip_paren(n)
            if base and base.lower() not in STOP_ENTITY and len(base) >= 6 and is_research_lab(n):
                self.lab_names[base.lower()] = n
        self.project_names = {
            str(p.get("name") or "").lower(): p
            for p in self.projects
            if len(str(p.get("name") or "")) >= 4 and str(p.get("name")).lower() not in STOP_ENTITY
        }
        self.grant_nums = {
            str(g.get("c_number") or "").lower(): g for g in self.grants if g.get("c_number")
        }
        self.asset_names: dict[str, dict[str, Any]] = {}
        for a in self.assets:
            n = str(a.get("name") or "")
            # "project/bucket" asset names match on the whole string only
            if len(n) >= 5 and n.lower() not in STOP_ENTITY and not n.isdigit():
                self.asset_names.setdefault(n.lower(), a)
        self._lab_re = self._alternation(self.lab_names)
        self._proj_re = self._alternation(self.project_names)
        self._asset_re = self._alternation(self.asset_names)

    @staticmethod
    def _alternation(keys: Any) -> re.Pattern[str] | None:
        ks = sorted((k for k in keys if k), key=len, reverse=True)
        if not ks:
            return None
        body = "|".join(re.escape(k).replace(r"\ ", r"\s+") for k in ks)
        return re.compile(r"(?<![\w-])(" + body + r")(?![\w-])", re.I)

    def find(self, text: str) -> dict[str, Any]:
        """Exact matches and suggestions in one blob of text."""
        low = text.lower()
        found: dict[str, Any] = {
            "people": {},
            "labs": {},
            "gcp": {},
            "projects": {},
            "grants": {},
            "assets": {},
            "suggest": [],
        }
        if self._name_re:
            for m in self._name_re.finditer(text):
                key = " ".join(m.group(1).lower().split())
                ids = self.by_name.get(key) or []
                if len(ids) == 1 and (key, ids[0]) in self.exact:
                    found["people"].setdefault(ids[0], {"how": "named", "text": m.group(1)})
                elif len(ids) == 1:  # matched only through a nickname: ask first
                    found["suggest"].append(
                        {
                            "kind": "person",
                            "text": m.group(1),
                            "why": "nickname of a ledger name",
                            "options": [{"id": ids[0], "name": self.netids[ids[0]].get("name")}],
                        }
                    )
                elif len(ids) > 1:
                    found["suggest"].append(
                        {
                            "kind": "person",
                            "text": m.group(1),
                            "why": f"{len(ids)} people in the ledger share this name",
                            "options": [
                                {"id": i, "name": self.netids[i].get("name")} for i in ids[:5]
                            ],
                        }
                    )
        for addr in EMAIL_RE.findall(text):
            local = addr.lower().split("@")[0]
            if local in self.netids:
                found["people"].setdefault(local, {"how": "address", "text": addr})
        for gid in set(GCP_ID_RE.findall(low)):
            if gid in self.gcp_ids:
                found["gcp"][gid] = self.gcp_ids[gid]
        for num in set(GRANT_RE.findall(text)):
            if num.lower() in self.grant_nums:
                found["grants"][num.lower()] = self.grant_nums[num.lower()]
        for rx, bag, table in (
            (self._lab_re, "labs", self.lab_names),
            (self._proj_re, "projects", self.project_names),
            (self._asset_re, "assets", self.asset_names),
        ):
            if rx:
                for m in rx.finditer(text):
                    k = " ".join(m.group(1).lower().split())
                    if k in table:
                        found[bag][k] = table[k]
        # suggestions: a lone surname that belongs to exactly one or a few people
        if self._last_re:
            named_last = {
                str(self.netids[n].get("name", "")).lower().split()[-1:][0]
                for n in found["people"]
                if str(self.netids[n].get("name", "")).split()
            }
            # surnames already covered by a nickname suggestion ("Ben Donovan")
            named_last |= {
                s["text"].lower().split()[-1] for s in found["suggest"] if " " in s["text"]
            }
            for m in self._last_re.finditer(text):
                last = m.group(1).lower()
                if last in named_last or not m.group(1)[0].isupper():
                    continue
                # a capitalised common word at a sentence start ("Long Beach") is
                # usually not a person; require the next word to be lowercase or
                # punctuation, or a title before it
                nxt = text[m.end() : m.end() + 2]
                prev = text[max(0, m.start() - 5) : m.start()].lower()
                if (
                    nxt[:1] == " "
                    and nxt[1:2].isupper()
                    and not re.search(r"(dr|prof)\.?\s$", prev)
                ):
                    # "Saltzman Lab" is a person's lab: suggest that person
                    if not re.match(r"\s+lab\b", text[m.end() : m.end() + 5], re.I):
                        continue
                ids = self.last_names.get(last) or []
                if 1 <= len(ids) <= 3:
                    found["suggest"].append(
                        {
                            "kind": "person",
                            "text": m.group(1),
                            "why": "surname only",
                            "options": [{"id": i, "name": self.netids[i].get("name")} for i in ids],
                        }
                    )
        # de-duplicate suggestions by text
        seen: set[str] = set()
        uniq = []
        for s in found["suggest"]:
            k = s["kind"] + ":" + s["text"].lower()
            if k not in seen:
                seen.add(k)
                uniq.append(s)
        found["suggest"] = uniq[:12]
        return found


def item_text(messages: list[dict[str, Any]]) -> str:
    """Every header and body in the item, as one blob for scanning."""
    parts = []
    for m in messages:
        for f in ("from", "to", "cc", "subject"):
            if m.get(f):
                parts.append(str(m[f]))
        parts.append(str(m.get("body") or ""))
    return "\n".join(parts)


def item_hash(key: str, messages: list[dict[str, Any]]) -> str:
    h = hashlib.sha256(key.encode())
    for m in messages:
        h.update(str(m.get("ts", "")).encode())
        h.update(str(len(str(m.get("body") or ""))).encode())
    return h.hexdigest()[:20]


def header_people(messages: list[dict[str, Any]], rules: Rules) -> list[dict[str, Any]]:
    """From/To/Cc people with roles, most active sender first (me and noise dropped)."""
    people: dict[str, dict[str, Any]] = {}

    def add(name: str, addr: str, role: str, idx: int) -> None:
        addr = (addr or "").strip().lower()
        if not addr or "@" not in addr or rules.is_me(addr) or rules.is_noise(addr):
            return
        p = people.setdefault(addr, {"addr": addr, "name": "", "roles": [], "sent": 0, "where": []})
        if name and not p["name"]:
            p["name"] = name.strip().strip('"')
        if role not in p["roles"]:
            p["roles"].append(role)
        if role == "from":
            p["sent"] += 1
        tag = f"{role} of message {idx}"
        if tag not in p["where"]:
            p["where"].append(tag)

    for i, m in enumerate(messages, 1):
        for n, a in email.utils.getaddresses([m.get("from") or ""]):
            add(n, a, "from", i)
        for field in ("to", "cc"):
            for n, a in email.utils.getaddresses([m.get(field) or ""]):
                add(n, a, field, i)
        if m.get("from_email"):
            add(m.get("from") or "", m["from_email"], "from", i)
    return list(people.values())


def where_named(messages: list[dict[str, Any]], needle: str) -> list[int]:
    rx = re.compile(WORD.format(re.escape(needle).replace(r"\ ", r"\s+")), re.I)
    return [i for i, m in enumerate(messages, 1) if rx.search(str(m.get("body") or ""))]


RANK = {"from": 0, "linked": 0, "named": 1, "to": 2, "cc": 3}


def rank_people(
    rows: list[dict[str, Any]], mine_to: set[str] | None = None
) -> list[dict[str, Any]]:
    """Senders first, then people named in the bodies, then To, then Cc.

    The other side of the conversation always counts as a sender: when the operator
    wrote the item, the people they wrote To rank with senders (otherwise a list of
    names in one email pushes its actual recipient past the dossier cap).
    """
    mine_to = mine_to or set()

    def score(p: dict[str, Any]) -> tuple:
        roles = p.get("roles") or ["named"]
        best = min((RANK.get(r, 4) for r in roles), default=4)
        if best > 0 and any(a in mine_to for a in p.get("addrs") or []):
            best = 0
        return (best, -int(p.get("sent") or 0), -len(p.get("named_in") or []), p.get("name") or "")

    return sorted(rows, key=score)


def operator_recipients(messages: list[dict[str, Any]], rules: Rules) -> set[str]:
    """Addresses the operator wrote To (not Cc) in this item."""
    out: set[str] = set()
    for m in messages:
        frm = [a for _, a in email.utils.getaddresses([m.get("from") or ""])]
        if m.get("mine") or any(rules.is_me(a) for a in frm):
            out |= {a.lower() for _, a in email.utils.getaddresses([m.get("to") or ""])}
    return out


# ---------------------------------------------------------------- the builder
class ItemContext:
    """Builds the People list and the Full context for one stream item.

    ``ledger`` is the read adapter (``ultra.ledger.Ledger`` or the demo fake). It must
    provide ``catalog()``, ``resolve()``, ``dossier(netid)``, ``tree(ident)``,
    ``search(term)`` and ``open_tasks()``; all are allow-listed reads, cached.
    """

    def __init__(self, ledger: Any, store: Any, rules: Rules, me_netids: set[str] | None = None):
        self.ledger = ledger
        self.store = store
        self.rules = rules
        self.me = {m.lower() for m in (me_netids or set()) if m}
        self._catalog: Catalog | None = None
        self._catalog_at = 0.0

    # -- catalog
    def catalog(self) -> Catalog:
        import time

        if self._catalog is None or time.time() - self._catalog_at > TTL_CATALOG:
            self._catalog = Catalog(self.ledger.catalog(), me=self.me)
            self._catalog_at = time.time()
        return self._catalog

    # -- step 1: who and what is in this item
    def entities(self, key: str, messages: list[dict[str, Any]], task: dict | None = None) -> dict:
        cat = self.catalog()
        text = item_text(messages)
        if task:
            text = "\n".join([str(task.get("summary") or ""), text])
        found = cat.find(text)
        rows: dict[str, dict[str, Any]] = {}  # netid or addr -> row
        # header people first
        hdr = header_people(messages, self.rules)
        with ThreadPoolExecutor(max_workers=4) as ex:
            resolved = list(ex.map(self._resolve_safe, hdr))
        for p, r in zip(hdr, resolved, strict=True):
            nid = str((r or {}).get("netid") or "").lower()
            k = nid or p["addr"]
            if nid in self.me:
                continue
            row = rows.get(k)
            if row:
                row["addrs"] = sorted(set(row["addrs"]) | {p["addr"]})
                row["roles"] = sorted(set(row["roles"]) | set(p["roles"]), key=list(RANK).index)
                row["sent"] += p["sent"]
                row["where"] += [w for w in p["where"] if w not in row["where"]]
                continue
            rows[k] = {
                "netid": nid,
                "id": (r or {}).get("id") or "",
                "name": (r or {}).get("name") or p["name"] or p["addr"],
                "title": (r or {}).get("title") or "",
                "addrs": [p["addr"]],
                "roles": list(p["roles"]),
                "sent": p["sent"],
                "where": list(p["where"]),
                "named_in": [],
                "in_ledger": bool(nid),
                "source": "header",
            }
        # people named in the bodies (and the task text)
        for nid, how in found["people"].items():
            if nid in self.me:
                continue
            msgs_with = where_named(messages, how["text"]) if how["how"] == "named" else []
            row = rows.get(nid)
            if row:
                row["named_in"] = sorted(set(row["named_in"]) | set(msgs_with))
                continue
            p = cat.netids.get(nid) or {}
            rows[nid] = {
                "netid": nid,
                "id": "",
                "name": p.get("name") or how["text"],
                "title": p.get("title") or "",
                "addrs": [],
                "roles": ["named"],
                "sent": 0,
                "where": [],
                "named_in": msgs_with,
                "in_ledger": True,
                "source": "task" if task and not msgs_with else "body",
            }
        people = rank_people(list(rows.values()), operator_recipients(messages, self.rules))
        for i, p in enumerate(people):
            p["dossier"] = bool(p["in_ledger"]) and i < MAX_DOSSIERS
        things = {
            "labs": [{"name": v, "type": "Lab"} for v in found["labs"].values()],
            "gcp": [
                {"name": g.get("project_id"), "label": g.get("name"), "type": "GCPProject"}
                for g in found["gcp"].values()
            ],
            "projects": [
                {"name": p.get("name"), "type": "ResearchProject"}
                for p in found["projects"].values()
            ],
            "grants": [
                {"name": g.get("c_number"), "label": g.get("title"), "type": "Grant"}
                for g in found["grants"].values()
            ],
            "assets": [{"name": a.get("name"), "type": "Asset"} for a in found["assets"].values()],
        }
        # hide suggestions for people already in the list
        have = {p["netid"] for p in people if p["netid"]}
        sugg = [
            s for s in found["suggest"] if not all(o["id"] in have for o in s.get("options") or [])
        ]
        return {
            "key": key,
            "people": people,
            "things": things,
            "suggest": sugg,
            "counts": {
                "people": len(people),
                "in_ledger": sum(1 for p in people if p["in_ledger"]),
                "dossiers": sum(1 for p in people if p["dossier"]),
            },
        }

    def _resolve_safe(self, p: dict[str, Any]) -> dict[str, Any] | None:
        try:
            return self.ledger.resolve(p["addr"], p.get("name", ""))
        except Exception:  # noqa: BLE001
            return None

    # -- step 2+3: the group's combined context
    def full(
        self,
        key: str,
        messages: list[dict[str, Any]],
        task: dict | None = None,
        confirmed: list[str] | None = None,
        fresh: bool = False,
    ) -> dict[str, Any]:
        """Everything for the item's group, merged. Cached per item version."""
        confirmed = sorted(
            {c.lower() for c in confirmed or [] if re.fullmatch(r"[a-z0-9._-]{1,64}", c.lower())}
        )
        ck = f"itemctx:{item_hash(key, messages)}:{','.join(confirmed)}"
        if not fresh:
            hit = self.store.cache_get(ck, TTL_ITEM)
            if hit:
                return {**hit[0], "age": round(hit[1])}
        ents = self.entities(key, messages, task)
        cat = self.catalog()
        for nid in confirmed:  # suggestions the operator confirmed
            if nid in cat.netids and not any(p["netid"] == nid for p in ents["people"]):
                p = cat.netids[nid]
                ents["people"].append(
                    {
                        "netid": nid,
                        "id": "",
                        "name": p.get("name"),
                        "title": p.get("title") or "",
                        "addrs": [],
                        "roles": ["named"],
                        "sent": 0,
                        "where": [],
                        "named_in": [],
                        "in_ledger": True,
                        "source": "confirmed",
                        "dossier": True,
                    }
                )
        with_dossier = [p for p in ents["people"] if p.get("dossier")][
            : MAX_DOSSIERS + len(confirmed)
        ]
        trees = [t for bag in ("labs", "gcp", "projects", "grants") for t in ents["things"][bag]][
            :MAX_TREES
        ]
        subject = next((m.get("subject") for m in messages if m.get("subject")), "") or (
            task or {}
        ).get("summary", "")
        with ThreadPoolExecutor(max_workers=4) as ex:
            f_dos = {
                p["netid"]: ex.submit(self._safe, self.ledger.dossier, p["netid"])
                for p in with_dossier
            }
            f_tree = {t["name"]: ex.submit(self._safe, self.ledger.tree, t["name"]) for t in trees}
            f_srch = ex.submit(self._safe, self.ledger.search_raw, subject, 25) if subject else None
            f_tasks = ex.submit(self._safe, self.ledger.open_tasks)
            dossiers = {k: f.result() for k, f in f_dos.items()}
            tree_res = {k: f.result() for k, f in f_tree.items()}
            srch = (f_srch.result() if f_srch else None) or []
            open_map = f_tasks.result() or {}
        return self._merge(key, messages, task, ents, dossiers, tree_res, srch, open_map, ck)

    @staticmethod
    def _safe(fn: Any, *a: Any) -> Any:
        try:
            return fn(*a)
        except Exception:  # noqa: BLE001
            return None

    def _merge(
        self,
        key: str,
        messages: list[dict[str, Any]],
        task: dict | None,
        ents: dict[str, Any],
        dossiers: dict[str, Any],
        trees: dict[str, Any],
        srch: list[dict[str, Any]],
        open_map: dict[str, Any],
        ck: str,
    ) -> dict[str, Any]:
        history: dict[str, dict[str, Any]] = {}
        group: dict[str, dict[str, dict[str, Any]]] = {
            k: {} for k in ("labs", "gcp_projects", "projects", "grants", "assets")
        }
        task_ids: dict[str, set[str]] = {}
        people_by_netid = {p["netid"]: p for p in ents["people"] if p["netid"]}

        def touch(iid: str, date: str, summary: str, who: str, why: str) -> None:
            h = history.setdefault(
                iid,
                {
                    "id": iid,
                    "date": (date or "")[:16],
                    "summary": summary,
                    "who": [],
                    "why": [],
                    "kind": "ledger",
                },
            )
            if len(summary) > len(h["summary"]):
                h["summary"] = summary
            if date and not h["date"]:
                h["date"] = date[:16]
            if who and who not in h["who"]:
                h["who"].append(who)
            if why and why not in h["why"]:
                h["why"].append(why)

        def add_thing(bag: str, c: dict[str, Any], via: str) -> None:
            k = str(c.get("id") or c.get("name"))
            g = group[bag].setdefault(
                k, {"id": c.get("id", ""), "name": c.get("name", ""), "via": []}
            )
            if via not in g["via"]:
                g["via"].append(via)

        for nid, d in dossiers.items():
            if not isinstance(d, dict) or "researcher" not in d:
                continue
            r = d["researcher"]
            who = re.sub(r"\s*\([^)]*\)\s*$", "", str(r.get("name") or nid))
            row = people_by_netid.get(nid)
            if row is not None:
                row["id"] = row.get("id") or r.get("id") or ""
                row["title"] = row.get("title") or r.get("title") or ""
                row["dept"] = r.get("dept") or ""
            for i in d.get("interactions") or []:
                touch(i["id"], i.get("date") or "", i.get("summary") or "", who, "linked")
            for lab in d.get("labs") or []:
                add_thing("labs", lab, who)
            for g in d.get("grants") or []:
                add_thing("grants", g, who)
            for p in d.get("projects") or []:
                add_thing("projects", p, who)
            for a in d.get("assets") or []:
                add_thing("assets", a, who)
            for c in d.get("connections") or []:
                et = c.get("entity_type")
                if et == "GCPProject":
                    add_thing("gcp_projects", c, who)
                elif et == "Task":
                    task_ids.setdefault(c["id"], set()).add(who)
        broad: list[dict[str, Any]] = []
        for name, t in trees.items():
            if not isinstance(t, dict) or not t.get("root"):
                continue
            root = t["root"]
            label = str(root.get("name") or name)
            bag = {
                "Lab": "labs",
                "GCPProject": "gcp_projects",
                "ResearchProject": "projects",
                "Grant": "grants",
            }.get(str(root.get("type")), "")
            conns = t.get("connections") or []
            if bag:
                add_thing(bag, root, "named in the item")
                group[bag][str(root.get("id") or root.get("name"))]["links"] = len(conns)
            if len(conns) > BROAD:  # a platform: listed, history not merged
                broad.append({"name": label, "type": root.get("type"), "links": len(conns)})
                continue
            for c in conns:
                et = c.get("entity_type")
                if et == "Interaction":
                    touch(c["id"], "", c.get("name") or "", label, "linked")
                elif et == "Task":
                    task_ids.setdefault(c["id"], set()).add(label)
                elif et == "Asset":
                    add_thing("assets", c, label)
        for r in srch if isinstance(srch, list) else []:
            if r.get("type") == "Interaction" and r.get("id"):
                touch(r["id"], "", r.get("name") or "", "", "topic search")
            elif r.get("type") == "Task" and r.get("id"):
                task_ids.setdefault(r["id"], set()).add("topic search")
        # tasks: linked to anyone/anything in the group, found by topic, or the item itself
        if task and task.get("id"):
            task_ids.setdefault(task["id"], set()).add("this item")
        name_res = [
            re.compile(WORD.format(re.escape(_strip_paren(str(p["name"])))), re.I)
            for p in ents["people"]
            if p["in_ledger"] and len(str(p["name"]).split()) >= 2
        ]
        tasks = []
        for tid, t in (open_map or {}).items():
            why = sorted(task_ids.get(tid, set()))
            if not why and any(rx.search(str(t.get("summary") or "")) for rx in name_res):
                why = ["names someone here"]
            if why:
                tasks.append(
                    {
                        "id": tid,
                        "summary": t.get("summary", ""),
                        "status": t.get("status", ""),
                        "priority": t.get("priority", ""),
                        "why": why,
                    }
                )
        prio = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}
        tasks.sort(key=lambda x: (prio.get(str(x["priority"]), 9), x["summary"]))
        # this item's own messages join the timeline
        for i, m in enumerate(messages, 1):
            iid = f"{key}#{i}"
            history[iid] = {
                "id": iid,
                "date": str(m.get("ts") or "")[:16],
                "kind": "item",
                "summary": re.sub(r"\s+", " ", str(m.get("body") or ""))[:600],
                "who": [re.sub(r"\s*<[^>]*>", "", str(m.get("from") or "")).strip('" ')],
                "why": [f"message {i} of {len(messages)}"],
            }
        hist = sorted(history.values(), key=lambda h: h["date"] or "0000", reverse=True)
        # ledger entries with no date (from tree/search) go after dated ones, kept
        dated = [h for h in hist if h["date"]]
        undated = [h for h in hist if not h["date"]]
        out = {
            "key": key,
            "subject": next((m.get("subject") for m in messages if m.get("subject")), "")
            or (task or {}).get("summary", ""),
            "people": ents["people"],
            "things": ents["things"],
            "suggest": ents["suggest"],
            "group": {k: list(v.values()) for k, v in group.items()},
            "tasks": tasks,
            "broad": broad,
            "history": dated + undated,
            "counts": {
                **ents["counts"],
                "history": sum(1 for h in hist if h["kind"] == "ledger"),
                "tasks": len(tasks),
                "dossiers_loaded": sum(1 for d in dossiers.values() if isinstance(d, dict)),
            },
            "age": 0,
        }
        self.store.cache_put(ck, out)
        return out


def context_text(ctx: dict[str, Any], limit: int = 150_000) -> str:
    """Plain text of an item context, for the AI briefing, drafts and audio."""
    L = [f"Stream item: {ctx.get('subject', '')}"]
    L.append("People in this item:")
    for p in ctx.get("people") or []:
        bits = [p.get("name") or ""]
        if p.get("netid"):
            bits.append(f"({p['netid']})")
        if p.get("title"):
            bits.append(f"- {p['title']}")
        roles = ", ".join(p.get("roles") or [])
        where = ""
        if p.get("named_in"):
            where = f"; named in message {', '.join(str(x) for x in p['named_in'])}"
        L.append(
            f"- {' '.join(bits)} [{roles}{where}]{'' if p.get('in_ledger') else ' (not in ledger)'}"
        )
    g = ctx.get("group") or {}
    for label, k in (
        ("Labs", "labs"),
        ("GCP projects", "gcp_projects"),
        ("Projects", "projects"),
        ("Grants", "grants"),
        ("Assets", "assets"),
    ):
        xs = g.get(k) or []
        if xs:
            L.append(
                f"{label}: "
                + "; ".join(f"{x['name']} (via {', '.join(x['via'][:3])})" for x in xs[:40])
            )
    if ctx.get("tasks"):
        L.append("Open ledger tasks for this group:")
        for tk in ctx["tasks"]:
            L.append(
                f"- task {tk['id'][:8]} [{tk['priority']}/{tk['status']}] {tk['summary']} "
                f"(why: {', '.join(tk['why'])})"
            )
    L.append("History, newest first (ledger interactions and this item's messages):")
    for h in ctx.get("history") or []:
        src = "THIS ITEM" if h["kind"] == "item" else "ledger"
        who = ", ".join(h.get("who") or [])
        why = ", ".join(h.get("why") or [])
        L.append(f"- [{h['date'] or 'undated'}] ({src}; {who}; {why}) {h['summary']}")
    return "\n".join(L)[:limit]
