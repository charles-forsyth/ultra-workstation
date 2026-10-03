#!/usr/bin/env python3
"""Ledger parity check (read-only, live): each read on both backends, compared.

Runs every ledger read Ultra makes through the CLI path (`nexus serve` or the `nexus`
binary) and through the hosted MCP server, then compares the fields Ultra uses. Prints
timings and differences; writes nothing anywhere. Needs [mcp.nexus] configured and
`ultra auth nexus` done.

    uv run python scripts/ledger_parity.py NETID [NETID ...]
"""

from __future__ import annotations

import json
import sys
import time
from typing import Any

from ultra import config, ledger_mcp, mcpclient
from ultra.ledger import Ledger
from ultra.store import Store


def timed(fn: Any, *a: Any) -> tuple[Any, float]:
    t = time.monotonic()
    try:
        return fn(*a), time.monotonic() - t
    except Exception as e:  # noqa: BLE001 - reported
        return e, time.monotonic() - t


def ids(rows: Any, key: str = "id") -> set[str]:
    return {str(r.get(key)) for r in rows or [] if isinstance(r, dict)}


def main() -> int:
    cfg = config.load()
    netids = sys.argv[1:] or [str(cfg.get("ledger", "my_id", "") or "")]
    if not netids[0]:
        print(__doc__)
        return 2
    client = mcpclient.client_from_config(cfg, "nexus")
    if client is None or not client.signed_in():
        print("configure [mcp.nexus] and run `ultra auth nexus` first")
        return 2
    my_id = str(cfg.get("ledger", "my_id", "") or "")
    cli = Ledger(cfg, _mem_store(), mcp=None)  # backend ignored: no MCP client given

    def via_cli(argv: list[str]) -> Any:
        return cli._run(argv)

    def via_mcp(argv: list[str]) -> Any:
        return ledger_mcp.run(client, argv, my_id)

    reads: list[tuple[str, list[str], Any]] = []
    for n in netids:
        reads += [
            (f"people show {n}", ["people", "show", n], lambda d: (d or {}).get("id")),
            (f"tree {n}", ["tree", n], lambda d: ids((d or {}).get("connections"))),
            (
                f"dossier {n} interactions",
                ["dossier", n],
                lambda d: ids((d or {}).get("interactions")),
            ),
            (f"dossier {n} labs", ["dossier", n], lambda d: ids((d or {}).get("labs"))),
        ]
    reads += [
        ("tasks list (mine, open)", ["tasks", "list"], ids),
        ("tasks list --global", ["tasks", "list", "--global"], ids),
        ("labs list --all", ["labs", "list", "--all"], ids),
        ("gcp list --all", ["gcp", "list", "--all"], ids),
        ("projects list --all", ["projects", "list", "--all"], ids),
        ("people list", ["people", "list", "--limit", "20000"], ids),
        ("interactions list 15", ["interactions", "list", "--limit", "15"], ids),
        ("stats counts", ["stats"], lambda d: json.dumps((d or {}).get("counts"), sort_keys=True)),
    ]
    bad = 0
    print(f"{'read':38} {'cli s':>7} {'mcp s':>7}  result")
    for label, argv, key in reads:
        a, ta = timed(via_cli, argv)
        b, tb = timed(via_mcp, argv)
        if isinstance(a, Exception) or isinstance(b, Exception):
            print(f"{label:38} {ta:7.2f} {tb:7.2f}  ERROR cli={a!r:.60} mcp={b!r:.60}")
            bad += 1
            continue
        ka, kb = key(a), key(b)
        if ka == kb:
            n = len(ka) if isinstance(ka, set) else ""
            print(f"{label:38} {ta:7.2f} {tb:7.2f}  same {n}")
        elif isinstance(ka, set):
            print(
                f"{label:38} {ta:7.2f} {tb:7.2f}  DIFF cli-only={len(ka - kb)} "
                f"mcp-only={len(kb - ka)} (cli {len(ka)}, mcp {len(kb)})"
            )
            bad += 1
        else:
            print(f"{label:38} {ta:7.2f} {tb:7.2f}  DIFF {str(ka)[:40]} vs {str(kb)[:40]}")
            bad += 1
    print("all match" if not bad else f"{bad} differences")
    return 1 if bad else 0


def _mem_store() -> Store:
    import tempfile
    from pathlib import Path

    return Store(Path(tempfile.mkdtemp()) / "parity.db")


if __name__ == "__main__":
    sys.exit(main())
