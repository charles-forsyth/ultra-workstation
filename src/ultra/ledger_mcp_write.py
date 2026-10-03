"""Ledger writes through the hosted ledger MCP server (SPEC 8.4, 8.8; v1.6).

Both Ultra writers (`ledger_write.LedgerWriter` for the desk, `ledgertab.LedgerTabWriter`
for the Ledger tab) build the exact `nexus ...` argv they would run, then call
`_run(argv, stdin)`. With `[ledger] backend = "mcp"` that argv comes here first and is
mapped to one write tool on the server, which runs the same CLI command in-process
(`@with_db_transaction`, provenance `source=mcp, client=Ultra`). The server sends back
the command's stdout, so the writers' read-back checks work unchanged.

Outcomes, and what the caller may do next:

- the tool ran (the answer has an `exit_code`): `(rc, text)`; the caller reads back.
- `Refused` (nothing ran: the server refused the arguments or the role before running,
  the server was unreachable, or the sign-in was refused): the caller may run the same
  argv on serve or the CLI.
- `NotMapped` (deletes, `projects docs rm`, an option the tool does not take): the
  caller uses serve or the CLI. Deletes stay on the CLI by the operator's decision.
- anything else (timeout, 5xx, lost connection, a tool error): `(1, text)` that says
  the outcome is unknown. Never re-sent anywhere: a second `log` is a duplicate record.
"""

from __future__ import annotations

from typing import Any

from ultra.mcpclient import McpAuthNeeded, McpClient, McpError, McpUnreachable


class NotMapped(Exception):
    """This write has no MCP tool (or an option the tool does not take)."""


class Refused(Exception):
    """The server did not run anything; the CLI may run the same argv."""


VALUE_OPTS = {
    "--priority",
    "--due",
    "--date",
    "--link",
    "--type",
    "--role",
    "--title",
    "--dept",
    "--description",
    "--summary",
    "--name",
    "--agency",
    "--amount",
    "--location",
    "--set",
    "--note",
    "--status",
}
MULTI = {"--link", "--set"}
FLAGS = {"--json", "--strict-links", "--yes", "--force", "--clear-due"}


def parse(argv: list[str]) -> tuple[tuple[str, ...], list[str], dict[str, Any], set[str]]:
    """Split a write argv into (command path, positionals, options, flags)."""
    path_len = 3 if argv[:3] == ["projects", "docs", "add"] else 2 if len(argv) > 1 else 1
    if argv[:1] in (["log"], ["link"], ["unlink"]):
        path_len = 1
    path = tuple(argv[:path_len])
    pos: list[str] = []
    opts: dict[str, Any] = {}
    flags: set[str] = set()
    i, rest = 0, argv[path_len:]
    while i < len(rest):
        a = rest[i]
        if a == "--":
            pos += rest[i + 1 :]
            break
        if a in FLAGS:
            flags.add(a)
        elif a in VALUE_OPTS:
            if i + 1 >= len(rest):
                raise NotMapped(f"{a} has no value")
            v = rest[i + 1]
            if a in MULTI:
                opts.setdefault(a, []).append(v)
            else:
                opts[a] = v
            i += 1
        elif a.startswith("--"):
            raise NotMapped(f"option {a} has no MCP equivalent")
        else:
            pos.append(a)
        i += 1
    return path, pos, opts, flags


def _details(sets: list[str]) -> dict[str, str]:
    out = {}
    for s in sets:
        k, sep, v = s.partition("=")
        if not sep:
            raise NotMapped(f"--set {s!r} is not key=value")
        out[k] = v
    return out


def build(argv: list[str], stdin: str = "", me: str = "") -> tuple[str, dict[str, Any]]:
    """argv -> (tool, arguments). Raises NotMapped for anything without a tool."""
    path, pos, o, flags = parse(argv)
    used: set[str] = set()

    def opt(name: str) -> Any:
        used.add(name)
        return o.get(name)

    def need(n: int) -> None:
        if len(pos) != n:
            raise NotMapped(f"nexus {' '.join(path)} expects {n} arguments")

    tool: str
    args: dict[str, Any]
    if path == ("log",):
        if pos != ["-"]:
            raise NotMapped("log text must come on stdin")
        links = list(opt("--link") or [])
        if not links and me:
            links = [me]  # the server needs one; the CLI links the caller anyway
        tool, args = "nexus_log", {"text": stdin.strip(), "links": links}
        if opt("--date"):
            args["date"] = o["--date"]
        flags -= {"--strict-links", "--yes"}  # the tool always runs strict and scripted
    elif path == ("tasks", "add"):
        need(1)
        tool, args = (
            "nexus_tasks_add",
            {"summary": pos[0], "priority": opt("--priority") or "MEDIUM"},
        )
        if opt("--due"):
            args["due"] = o["--due"]
        flags -= {"--json"}
    elif path == ("tasks", "update"):
        need(1)
        tool, args = "nexus_tasks_update", {"task_id": pos[0]}
        for k in ("status", "priority", "due"):
            if opt(f"--{k}"):
                args[k] = o[f"--{k}"]
        if "--clear-due" in flags:
            args["clear_due"] = True
            flags.discard("--clear-due")
    elif path == ("link",):
        need(2)
        if not opt("--type"):
            raise NotMapped("link needs --type")
        tool = "nexus_link"
        args = {"source": pos[0], "target": pos[1], "connection_type": o["--type"]}
        args["role"] = opt("--role") or "Member"  # the CLI's default too
    elif path == ("unlink",):
        need(2)
        tool, args = "nexus_unlink", {"source": pos[0], "target": pos[1]}
        flags.discard("--force")  # the operator confirmed in Ultra; see run()
    elif path == ("interactions", "edit"):
        need(1)
        tool, args = "nexus_interactions_edit", {"interaction_id": pos[0]}
        for k in ("summary", "date", "note"):
            if opt(f"--{k}"):
                args[k] = o[f"--{k}"]
        flags.discard("--force")
    elif path == ("people", "add"):
        need(2)
        tool, args = "nexus_people_add", {"netid": pos[0], "name": pos[1]}
        for k in ("title", "dept"):
            if opt(f"--{k}"):
                args[k] = o[f"--{k}"]
    elif path in (("people", "update"), ("labs", "update")):
        need(1)
        key = "netid" if path[0] == "people" else "name"
        tool = f"nexus_{path[0]}_update"
        args = {key: pos[0], "details": _details(opt("--set") or [])}
    elif path == ("people", "tag"):
        need(2)
        tool, args = "nexus_people_tag", {"netid": pos[0], "tag": pos[1]}
    elif path == ("labs", "add"):
        need(1)
        tool, args = "nexus_labs_add", {"name": pos[0]}
        if opt("--description"):
            args["description"] = o["--description"]
    elif path == ("projects", "add"):
        need(1)
        tool, args = "nexus_projects_add", {"name": pos[0], "summary": opt("--summary") or ""}
    elif path == ("projects", "docs", "add"):
        need(2)
        tool = "nexus_projects_docs_add"
        args = {"project_name": pos[0], "url": pos[1], "title": opt("--title") or ""}
    elif path == ("grants", "add"):
        need(2)
        tool, args = "nexus_grants_add", {"c_number": pos[0], "title": pos[1]}
        if opt("--agency"):
            args["agency"] = o["--agency"]
        if opt("--amount"):
            args["amount"] = int(o["--amount"])
    elif path == ("gcp", "add"):
        need(1)
        tool, args = "nexus_gcp_add", {"project_id": pos[0]}
        if opt("--name"):
            args["name"] = o["--name"]
    elif path == ("assets", "add"):
        need(1)
        tool, args = "nexus_assets_add", {"name": pos[0], "asset_type": opt("--type") or "OTHER"}
        if opt("--location"):
            args["location"] = o["--location"]
    else:
        raise NotMapped(f"nexus {' '.join(path)} stays on the CLI")
    left = (set(o) - used) | flags
    if left:
        raise NotMapped(f"{', '.join(sorted(left))} not taken by {tool}")
    return tool, args


def _text(res: dict[str, Any]) -> str:
    """The command's output, plus the lines the writers look for when the server's
    JSON has them and the (tail-trimmed) output does not."""
    out = f"{res.get('output') or ''}\n{res.get('errors') or ''}"
    raw = res.get("json")
    j: dict[str, Any] = raw if isinstance(raw, dict) else {}
    iid = j.get("interaction_id")
    if iid and "Logged (ID:" not in out:
        out += f"\nLogged (ID: {iid})"
    for u in j.get("unresolved") or []:
        if f"--link '{u}' did not resolve" not in out:
            out += f"\n--link '{u}' did not resolve"
    return out


def _me(client: McpClient, me: str) -> str:
    """Your own ledger id for a log with no other links (the tool needs one)."""
    if me:
        return me
    cached = getattr(client, "_ultra_netid", None)
    if cached is None:
        try:
            cached = str(client.whoami().get("netid") or "")
        except McpError:
            cached = ""
        client._ultra_netid = cached  # type: ignore[attr-defined]
    return cached


def run(client: McpClient, argv: list[str], stdin: str = "", me: str = "") -> tuple[int, str]:
    """Run one write over MCP. See the module docstring for the outcomes."""
    if argv[:1] == ["log"] and "--link" not in argv:
        me = _me(client, me)
    tool, args = build(argv, stdin, me)
    try:
        res = client.call(tool, args)
        if tool == "nexus_unlink" and isinstance(res, dict) and res.get("confirm_required"):
            # Step two with the server's token: Ultra already asked the operator twice.
            res = client.call(tool, {**args, "confirm_token": res.get("confirm_token")})
    except (McpUnreachable, McpAuthNeeded) as e:
        raise Refused(str(e)) from e
    except McpError as e:
        return 1, f"ledger MCP: {e}\nThe outcome is unknown: check the record before trying again."
    if not isinstance(res, dict):
        return 1, f"ledger MCP: {tool} gave no result; the outcome is unknown."
    if "exit_code" not in res:
        err = str(res.get("error") or "refused")
        if "unknown" in err.lower():  # the server's own timeout: it may have run
            return 1, f"ledger MCP: {err}\nCheck the record before trying again."
        # refused before running (validation, policy, role): nothing was written
        raise Refused(err)
    return int(res.get("exit_code") or 0), _text(res)
