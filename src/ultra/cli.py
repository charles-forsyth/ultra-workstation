"""`ultra` command line."""

from __future__ import annotations

import argparse
import os
import sys
import webbrowser

from ultra import __version__


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ultra",
        description=(
            "Ultra AI Workstation Desktop: email, calendar, Slack and your work "
            "ledger in one local web workstation. Listens on 127.0.0.1 only."
        ),
    )
    p.add_argument("-v", "--version", action="version", version=f"ultra {__version__}")
    sub = p.add_subparsers(dest="command")

    s = sub.add_parser("start", help="Start the server in the background")
    s.add_argument("--port", type=int, default=None)
    s.add_argument("--demo", action="store_true", help="Serve synthetic data only")
    sub.add_parser("stop", help="Stop the background server")
    r = sub.add_parser("restart", help="Stop and start again")
    r.add_argument("--port", type=int, default=None)
    r.add_argument("--demo", action="store_true")
    sub.add_parser("status", help="Is it running?")
    sub.add_parser("open", help="Open the running workstation in the browser")
    fg = sub.add_parser("serve", help="Run in the foreground (Ctrl-C to stop)")
    fg.add_argument("--port", type=int, default=None)
    fg.add_argument("--demo", action="store_true")
    sub.add_parser("doctor", help="Check config, file permissions, tools and keys")

    a = sub.add_parser("auth", help="Authorize access to Google")
    asub = a.add_subparsers(dest="auth_cmd")
    g = asub.add_parser(
        "google",
        help="Run the Google sign-in for one capability and save its token",
        description=(
            "Opens a browser for Google sign-in and saves a token to the path set in "
            "config ([google] token_<capability>). Only needed when a token is "
            "missing, revoked, or lacks the scope; existing token files can be reused "
            "by pointing config at them."
        ),
    )
    g.add_argument(
        "--capability",
        choices=["read", "modify", "send", "calendar"],
        required=True,
    )

    pg = sub.add_parser("purge", help="Delete local copies (audio files, research uploads)")
    pg.add_argument("--audio", action="store_true", help="Delete generated audio files")
    pg.add_argument("--uploads", action="store_true", help="Delete research thread uploads")

    c = sub.add_parser("config", help="Configuration files")
    csub = c.add_subparsers(dest="config_cmd")
    csub.add_parser("init", help="Write example config.toml and style.toml (never overwrites)")
    csub.add_parser("path", help="Print the config and data folders")
    return p


def _config_init() -> int:
    from ultra.config import (
        config_dir,
        example_config_text,
        example_style_text,
        private_dir,
    )

    d = private_dir(config_dir())
    private_dir(d / "tokens")
    for name, text in (
        ("config.toml", example_config_text()),
        ("style.toml", example_style_text()),
    ):
        target = d / name
        if target.exists():
            print(f"[INFO] {target} exists; left unchanged.")
            continue
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(text)
        print(f"[INFO] Wrote {target} (edit it: addresses, time zone, ids).")
    return 0


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    from ultra import config, daemon

    cfg = config.load()
    port = getattr(args, "port", None) or cfg.port
    cmd = args.command
    if cmd is None:
        build_parser().print_help()
        return
    if cmd == "start":
        sys.exit(daemon.start(port, args.demo))
    if cmd == "stop":
        sys.exit(daemon.stop())
    if cmd == "restart":
        daemon.stop()
        sys.exit(daemon.start(port, args.demo))
    if cmd == "status":
        sys.exit(daemon.status())
    if cmd == "open":
        state = daemon.read_state()
        if not state:
            print("[INFO] Ultra is not running. Start it with: ultra start")
            sys.exit(3)
        url = f"http://127.0.0.1:{state['port']}/"
        print(url)
        webbrowser.open(url)
        return
    if cmd == "serve":
        from ultra.server import serve

        serve(cfg, port, demo=args.demo)
        return
    if cmd == "doctor":
        from ultra.doctor import run

        checks = run(cfg)
        bad = 0
        for c in checks:
            mark = "ok  " if c.ok else ("FAIL" if c.required else "--  ")
            bad += int(c.required and not c.ok)
            print(f"  {mark} {c.name:26} {c.detail}")
        sys.exit(1 if bad else 0)
    if cmd == "purge":
        if not (args.audio or args.uploads):
            print("usage: ultra purge [--audio] [--uploads]")
            sys.exit(2)
        from ultra.store import Store

        store = Store()
        if args.audio:
            from ultra.audio import Audio

            n = Audio(cfg, store, None).purge()
            print(f"[INFO] Deleted {n} audio file(s).")
        if args.uploads:
            folder = config.data_dir() / "research-uploads"
            n = 0
            for p in folder.glob("thread-*.txt") if folder.exists() else []:
                p.unlink(missing_ok=True)
                n += 1
            print(f"[INFO] Deleted {n} research upload(s).")
        return
    if cmd == "auth":
        if args.auth_cmd != "google":
            print("usage: ultra auth google --capability read|modify|send|calendar")
            sys.exit(2)
        from ultra import google_auth

        try:
            path = google_auth.authorize(cfg, args.capability)
        except (FileNotFoundError, google_auth.AuthNeeded) as e:
            print(f"[ERROR] {e}")
            sys.exit(1)
        print(f"[INFO] Saved {args.capability} token to {path}")
        return
    if cmd == "config":
        if args.config_cmd == "init":
            sys.exit(_config_init())
        print(f"config: {config.config_dir()}\ndata:   {config.data_dir()}")
        return


if __name__ == "__main__":
    main()
