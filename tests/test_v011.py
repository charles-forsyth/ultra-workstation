"""v0.11 tests: mail search, labels, attachments, send-as, highlights and notes."""

from __future__ import annotations

import base64
import hashlib
from typing import Any

import pytest

from ultra.compose import ComposeError, Composer, build_mime, content_hash, gmail_send
from ultra.config import Config
from ultra.mailx import (
    Annotations,
    DraftFiles,
    MailXError,
    SavedSearches,
    check_query,
    decode_upload,
    is_system_label,
    label_view,
    safe_name,
)
from ultra.store import Store


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "s.db")


@pytest.fixture
def files(store, tmp_path):
    return DraftFiles(store, tmp_path / "att")


@pytest.fixture
def comp(store, files):
    return Composer(
        Config({"mail": {"send_delay_seconds": 0}}),
        store,
        {"ada@example.org"},
        files=files,
        send_as=lambda: ["ada@example.org", "help@example.org"],
    )


# ---------------------------------------------------------------- small helpers
def test_safe_name_strips_paths_and_controls():
    assert safe_name("../../etc/passwd") == "passwd"
    assert safe_name("C:\\\\Users\\\\x\\\\report.pdf") == "report.pdf"
    assert safe_name('a"b<c>\n.txt') == "a_b_c__.txt"
    assert safe_name("") == "attachment"
    assert safe_name("...") == "attachment"


def test_system_labels_are_protected():
    for lid in ("INBOX", "TRASH", "SPAM", "UNREAD", "SENT", "DRAFT", "STARRED", "CATEGORY_SOCIAL"):
        assert is_system_label(lid)
    assert not is_system_label("Label_12")
    v = label_view(
        [{"id": "INBOX", "name": "INBOX", "type": "system"}, {"id": "Label_1", "name": "b"}]
    )
    assert v[0]["id"] == "Label_1" and v[1]["protected"]


def test_query_limits():
    assert check_query("  from:ben   budget ") == "from:ben budget"
    for bad in ("", "   ", "x" * 301):
        with pytest.raises(MailXError):
            check_query(bad)


def test_decode_upload_rejects_garbage():
    assert decode_upload(base64.b64encode(b"hi").decode()) == b"hi"
    with pytest.raises(MailXError):
        decode_upload("not base64!!")


# ---------------------------------------------------------------- highlights
def test_annotations_crud_and_validation(store):
    a = Annotations(store)
    n = a.add(
        {"thread_key": "g-abc123", "message_id": "m1", "quote": "the budget", "note": "ask Dee"}
    )
    assert n["color"] == "amber" and a.list("g-abc123")[0]["note"] == "ask Dee"
    a.update(n["id"], {"color": "cyan", "note": ""})
    assert a.get(n["id"])["color"] == "cyan"
    for bad in (
        {"thread_key": "x-1", "quote": "q"},
        {"thread_key": "g-abc", "quote": ""},
        {"thread_key": "g-abc", "quote": "q" * 2001},
        {"thread_key": "g-abc", "quote": "q", "color": "red"},
        {"thread_key": "t-" + "a" * 36, "quote": "q"},
    ):
        with pytest.raises(MailXError):
            a.add(bad)
    with pytest.raises(MailXError):
        a.update(n["id"], {"color": "puce"})
    a.delete(n["id"])
    assert a.list("g-abc123") == []
    with pytest.raises(MailXError):
        a.delete(n["id"])


def test_saved_searches(store):
    s = SavedSearches(store)
    s.add("Budget", "subject:budget")
    s.add("Budget", "subject:budget has:attachment")  # same name replaces
    assert [x["query"] for x in s.list()] == ["subject:budget has:attachment"]
    with pytest.raises(MailXError):
        s.add("", "x")
    s.delete(s.list()[0]["id"])
    assert s.list() == []


# ---------------------------------------------------------------- draft files
def test_draft_files_are_private_and_capped(files, tmp_path):
    import stat

    r = files.add(1, "../plan.txt", b"hello", "text/plain")
    a = r["attachments"][0]
    assert a["name"] == "plan.txt" and a["sha256"] == hashlib.sha256(b"hello").hexdigest()
    folder = tmp_path / "att" / "draft-1"
    assert stat.S_IMODE(folder.stat().st_mode) == 0o700
    f = next(folder.iterdir())
    assert stat.S_IMODE(f.stat().st_mode) == 0o600
    with pytest.raises(MailXError):
        files.add(1, "empty.txt", b"")
    with pytest.raises(MailXError):
        files.add(1, "big.bin", b"x" * (25 * 1024 * 1024 + 1))
    assert files.add(1, "evil", b"x", "text/html; charset=x<script>")["attachments"][1]["mime"] == (
        "application/octet-stream"
    )


def test_file_changed_on_disk_is_caught(files, tmp_path):
    files.add(2, "a.txt", b"one")
    p = next((tmp_path / "att" / "draft-2").iterdir())
    p.write_bytes(b"two")
    with pytest.raises(MailXError):
        files.files(2)


# ---------------------------------------------------------------- composer + files + From
def test_attachment_is_part_of_the_approval(comp):
    d = comp.create("new", None, "ada@example.org")
    d = comp.save(
        d["id"], {"to_addrs": "ben@example.org", "subject": "Plan", "body": "Hi Ben"}, "me"
    )
    h0 = content_hash(d, d["current"])
    d = comp.attach(d["id"], "plan.txt", b"hello", "text/plain")
    assert content_hash(d, d["current"]) != h0  # files change the hash
    comp.approve(d["id"])
    rv = comp.review(d["id"])
    assert [a["name"] for a in rv["attachments"]] == ["plan.txt"]
    d = comp.attach(d["id"], "more.txt", b"x")
    assert d["state"] == "DRAFT"  # adding a file voids approval
    with pytest.raises(ComposeError):
        comp.confirm(d["id"], rv["token"], rv["version"], lambda *_: {"id": "x"})


def test_no_attachment_hash_unchanged_for_old_drafts(comp):
    """Drafts without files hash exactly as before v0.11 (no 'attachments' key)."""
    import json

    d = comp.create("new", None, "ada@example.org")
    v = d["current"]
    canon = {
        k: (v.get(k) or "").strip()
        for k in ("from_addr", "to_addrs", "cc", "bcc", "subject", "body")
    }
    canon.update(thread_id="", in_reply_to="")
    assert (
        content_hash(d, v) == hashlib.sha256(json.dumps(canon, sort_keys=True).encode()).hexdigest()
    )


def test_from_must_be_a_send_as_address(comp):
    d = comp.create("new", None, "ada@example.org")
    d = comp.save(
        d["id"], {"from_addr": "mallory@evil.org", "to_addrs": "b@x.org", "body": "x"}, "me"
    )
    assert any(i["code"] == "from_not_allowed" for i in d["current"]["lint"])
    with pytest.raises(ComposeError):
        comp.approve(d["id"])
    d = comp.save(d["id"], {"from_addr": "help@example.org"}, "me")
    assert not any(i["code"] == "from_not_allowed" for i in d["current"]["lint"])


def test_attachment_mention_warning_clears_when_a_file_is_added(comp):
    d = comp.create("new", None, "ada@example.org")
    d = comp.save(d["id"], {"to_addrs": "b@x.org", "subject": "s", "body": "Plan attached."}, "me")
    assert any(i["code"] == "no_attachment" for i in d["current"]["lint"])
    d = comp.attach(d["id"], "plan.txt", b"p")
    assert not any(i["code"] == "no_attachment" for i in d["current"]["lint"])


def test_slack_drafts_refuse_files(comp):
    d = comp.create_slack("C123", "", "#general")
    with pytest.raises(ComposeError):
        comp.attach(d["id"], "a.txt", b"x")


def test_mime_carries_files():
    m = build_mime(
        {}, {"to_addrs": "b@x.org", "subject": "s", "body": "hi"}, [("a.txt", "text/plain", b"abc")]
    )
    parts = list(m.iter_attachments())
    assert len(parts) == 1 and parts[0].get_filename() == "a.txt"
    assert parts[0].get_content() == "abc"


def test_gmail_send_rechecks_from_and_files(monkeypatch, files):
    sent: list[Any] = []

    class G:
        def users(self) -> Any:
            return self

        def messages(self) -> Any:
            return self

        def send(self, userId: str, body: dict) -> Any:
            sent.append(body)
            return type("X", (), {"execute": lambda s: {"id": "m1", "threadId": "t1"}})()

        def get(self, **kw: Any) -> Any:
            return type("X", (), {"execute": lambda s: {"labelIds": ["SENT"]}})()

    from ultra import google_auth

    monkeypatch.setattr(google_auth, "service", lambda *a, **k: G())
    fn = gmail_send(Config({}), files, lambda: ["ada@example.org"])
    v = {"from_addr": "evil@x.org", "to_addrs": "b@x.org", "subject": "s", "body": "b"}
    with pytest.raises(RuntimeError):
        fn({"id": 9, "attachments": []}, v)
    files.add(9, "a.txt", b"one")
    d = {"id": 9, "attachments": files.list(9)}
    v["from_addr"] = "ada@example.org"
    assert fn(d, v)["id"] == "m1"
    assert len(sent) == 1
    raw = base64.urlsafe_b64decode(sent[0]["raw"]).decode()
    assert "a.txt" in raw
    d2 = {"id": 9, "attachments": [*files.list(9), {"name": "ghost", "size": 1, "sha256": "0"}]}
    with pytest.raises(RuntimeError):  # approved list differs from what is on disk
        fn(d2, v)
    assert len(sent) == 1


# ---------------------------------------------------------------- Gmail adapter
class FakeGmail:
    def __init__(self) -> None:
        self.modified: list[tuple[str, dict]] = []
        self.labels_db = [
            {"id": "INBOX", "name": "INBOX", "type": "system"},
            {"id": "Label_1", "name": "Follow up", "type": "user"},
        ]
        self.created: list[dict] = []
        self.reads = 0

    def users(self) -> Any:
        return self

    def labels(self) -> Any:
        return self

    def threads(self) -> Any:
        return self

    def messages(self) -> Any:
        return self

    def attachments(self) -> Any:
        return self

    def list(self, **kw: Any) -> Any:
        return X({"labels": self.labels_db})

    def create(self, userId: str, body: dict) -> Any:
        self.created.append(body)
        return X({"id": "Label_9", "name": body["name"]})

    def modify(self, userId: str, id: str, body: dict) -> Any:
        self.modified.append((id, body))
        return X({})

    def get(self, userId: str, id: str, format: str = "", **kw: Any) -> Any:
        if format == "minimal":
            return X({"messages": [{"labelIds": ["INBOX", "Label_1"]}]})
        att = base64.urlsafe_b64encode(b"x").decode()
        self.reads += 1  # like Gmail: a NEW attachmentId on every read of the message
        return X(
            {
                "payload": {
                    "mimeType": "multipart/mixed",
                    "parts": [
                        {"partId": "0", "mimeType": "text/plain", "body": {"data": att}},
                        {
                            "partId": "1",
                            "mimeType": "application/pdf",
                            "filename": "../r.pdf",
                            "body": {"attachmentId": f"ATTREAD{self.reads:04d}", "size": 3},
                        },
                    ],
                }
            }
        )


class X:
    def __init__(self, v: Any):
        self.v = v

    def execute(self) -> Any:
        return self.v


@pytest.fixture
def mail(store, monkeypatch):
    from ultra.mail import Mail
    from ultra.rules import Rules

    m = Mail(Config({}), store, Rules(me={"ada@example.org"}))
    g = FakeGmail()
    monkeypatch.setattr(m, "_svc", lambda: g)
    monkeypatch.setattr(m, "_modify_svc", lambda: g)
    m.fake = g  # type: ignore[attr-defined]
    return m


def test_labels_refuse_system_and_unknown(mail):
    for add, remove in (
        (["INBOX"], []),
        ([], ["INBOX"]),
        (["TRASH"], []),
        (["UNREAD"], []),
        (["Label_404"], []),
    ):
        with pytest.raises(MailXError):
            mail.apply_labels(["abcdef12"], add, remove)
    with pytest.raises(MailXError):
        mail.apply_labels(["abcdef12"], ["Label_1"], ["Label_1"])
    assert mail.fake.modified == []
    r = mail.apply_labels(["abcdef12", "bad id!"], ["Label_1"], [])
    assert r["ok"] and mail.fake.modified == [
        ("abcdef12", {"addLabelIds": ["Label_1"], "removeLabelIds": []})
    ]
    assert mail.store.journal_recent(1)[0]["action"] == "labels_changed"


def test_create_label_checks_name_and_duplicates(mail):
    with pytest.raises(MailXError):
        mail.create_label("follow UP")  # exists, case-insensitive
    with pytest.raises(MailXError):
        mail.create_label("bad<name>")
    assert mail.create_label("Projects/ada-lab")["id"] == "Label_9"


def test_attachment_name_comes_from_the_message(mail):
    mail.fake.attachments = lambda: mail.fake  # attachments().get(...)
    orig_get = mail.fake.get

    fetched: list[str] = []

    def get(**kw: Any) -> Any:
        if "messageId" in kw:
            fetched.append(kw["id"])
            return X({"data": base64.urlsafe_b64encode(b"%PDF").decode()})
        return orig_get(**kw)

    mail.fake.get = get
    # the page got partId "1" from an earlier read; the fetch must use the attachmentId
    # from the fresh read, not a stale one
    mail.fake.reads = 7
    name, mime, data = mail.attachment("abc123def", "1")
    assert (name, mime, data) == ("r.pdf", "application/pdf", b"%PDF")
    assert fetched == ["ATTREAD0008"]
    for mid, aid in (("../x", "1"), ("abc123def", "x"), ("abc123def", "9"), ("abc123def", "0")):
        with pytest.raises(MailXError):
            mail.attachment(mid, aid)
    assert fetched == ["ATTREAD0008"]  # none of the refused requests fetched anything


def test_an_inline_named_part_without_attachment_id_is_refused(mail):
    """A part with a filename but its data inline has no attachmentId: refuse it
    rather than asking Gmail for an attachment with an empty id."""
    orig = mail.fake.get

    def get(**kw: Any) -> Any:
        if "messageId" in kw:
            raise AssertionError("must not fetch")
        r = orig(**kw).execute()
        r["payload"]["parts"][1]["body"] = {"data": "eA==", "size": 1}
        r["payload"]["parts"][1]["partId"] = "1"
        return X(r)

    mail.fake.attachments = lambda: mail.fake
    mail.fake.get = get
    with pytest.raises(MailXError):
        mail.attachment("abc123def", "1")


def test_thread_attachments_expose_part_id_not_gmail_internals():
    from ultra.mail import extract_body

    payload = {
        "mimeType": "multipart/mixed",
        "parts": [
            {"partId": "0", "mimeType": "text/plain", "body": {"data": ""}},
            {
                "partId": "1",
                "mimeType": "image/png",
                "filename": "a.png",
                "body": {"attachmentId": "SECRETISH", "size": 10},
            },
        ],
    }
    _t, _h, atts = extract_body(payload)
    assert atts[0]["id"] == "1" and atts[0]["_att"] == "SECRETISH"


# ---------------------------------------------------------------- demo routes
def _api():
    from ultra.server import build

    httpd, api = build(Config({}), 0, demo=True)
    httpd.server_close()
    return api


def _call(api, method: str, path: str, body: Any = None, q: dict | None = None) -> Any:
    return api.dispatch(method, path, q or {}, body)[1]


def test_demo_mail_extras_end_to_end():
    from ultra.server import ApiError, BytesFile

    api = _api()
    r = _call(api, "GET", "/api/mail/search", q={"q": ["handover"]})
    assert r["count"] >= 1 and r["items"][0]["key"] == "g-100"
    with pytest.raises(ApiError):
        _call(api, "GET", "/api/mail/search", q={"q": [""]})
    labs = _call(api, "GET", "/api/mail/labels")["labels"]
    assert any(x["protected"] for x in labs)
    with pytest.raises(ApiError):
        _call(api, "POST", "/api/mail/labels/apply", {"threads": ["g-100"], "add": ["INBOX"]})
    _call(api, "POST", "/api/mail/labels/apply", {"threads": ["g-100"], "add": ["Label_2"]})
    assert api.demo_applied["100"] == {"Label_2"}
    f = _call(api, "GET", "/api/mail/attachment/demomsg0001/2", q={"inline": ["1"]})
    assert isinstance(f, BytesFile) and f.inline and f.ctype == "text/plain"
    f2 = _call(api, "GET", "/api/mail/attachment/demomsg0001/2")
    assert not f2.inline and f2.ctype == "application/octet-stream"

    d = _call(api, "POST", "/api/drafts", {"kind": "forward", "thread": "g-100"})
    d = _call(
        api,
        "POST",
        f"/api/drafts/{d['id']}/attach-from",
        {"message": "demomsg0001", "attachment": "2"},
    )
    assert [a["name"] for a in d["attachments"]] == ["handover-plan.txt"]
    up = base64.b64encode(b"notes").decode()
    d = _call(api, "POST", f"/api/drafts/{d['id']}/attach", {"name": "n.txt", "data": up})
    assert len(d["attachments"]) == 2
    d = _call(api, "POST", f"/api/drafts/{d['id']}/detach", {"id": d["attachments"][1]["id"]})
    d = _call(
        api,
        "POST",
        f"/api/drafts/{d['id']}/versions",
        {"to_addrs": "dee@example.org", "from_addr": "help@example.org"},
    )
    _call(api, "POST", f"/api/drafts/{d['id']}/approve")
    rv = _call(api, "POST", f"/api/drafts/{d['id']}/review")
    assert [a["name"] for a in rv["attachments"]] == ["handover-plan.txt"]
    assert rv["message"]["from_addr"] == "help@example.org"

    n = _call(
        api,
        "POST",
        "/api/notes",
        {"thread_key": "g-100", "message_id": "demomsg0001", "quote": "budget"},
    )
    assert _call(api, "GET", "/api/notes/g-100")["notes"][0]["id"] == n["id"]
    _call(api, "POST", f"/api/notes/{n['id']}/delete")
    assert _call(api, "GET", "/api/notes/g-100")["notes"] == []
    sa = _call(api, "GET", "/api/mail/sendas")
    assert [a["email"] for a in sa["addresses"]] == ["ada@example.org", "help@example.org"]


def test_per_file_and_total_caps_are_separate(files, monkeypatch):
    import ultra.mailx as mx

    monkeypatch.setattr(mx, "MAX_ATT", 10)
    with pytest.raises(MailXError) as e:
        files.add(3, "a.bin", b"x" * 11)
    assert "each" in str(e.value)  # the per-file message, not the total
    files.add(3, "a.bin", b"x" * 6)
    with pytest.raises(MailXError) as e2:
        files.add(3, "b.bin", b"x" * 6)
    assert "together" in str(e2.value)
