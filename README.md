# Ultra AI Workstation Desktop

A local web workstation for email, calendar, Slack and a work ledger in one window.
Read, triage, draft with AI, revise, approve twice, send, then log and link the
conversation in the ledger by dragging it into a bucket.

Status: v0.3.0. Mail read and write (double-approval send), AI summary and drafting,
archive with undo, ticket cards, whose-court rules, ledger side panel, Slack read.
Calendar, the bucket and Slack replies are next. See [SPEC.md](SPEC.md) section 19.

Nothing here contains credentials or personal data. Runtime config and tokens live in
`~/.config/ultra-workstation/`; private notes live in the git-ignored `local/` folder.

## Install

```bash
uv tool install "git+https://github.com/charles-forsyth/ultra-workstation.git[google]"
ultra config init        # writes config.toml and style.toml (never overwrites)
ultra doctor             # checks setup without printing secrets
ultra start --demo       # synthetic data, nothing real is read or sent
ultra start              # your own accounts
ultra open
```

It listens on 127.0.0.1 only (default port 7440).

## Sending mail

Nothing is ever sent without two clicks on two different screens:

1. **Approve (1 of 2)** locks the exact text. Any later edit sends it back to Draft.
2. **Review and send (2 of 2)** shows the final message, recipients and warnings, then
   sends after a short delay you can cancel.

The server refuses to send if the approval token is unknown, used, expired, or if the
stored message no longer matches what you approved. AI output only ever becomes a new
draft version; it cannot approve or send.

## Keys

`j`/`k` move, `Enter` open, `r` reply, `a` reply all, `f` forward, `s` AI summary,
`e` archive (never deletes), `c` compose, `m` Mine, `w` Waiting, `Ctrl+K` commands,
`?` help.

## Configuration

- `config.toml`: your addresses, token paths, VIP list, ticket patterns, AI model.
- `style.toml`: outgoing text rules (ASCII level, forbidden patterns, signature).
- `.env`: `GEMINI_API_KEY` for AI features (optional).

Google access reuses token files you point at in `config.toml`; missing scopes can be
added with `ultra auth google <capability>`.

## License

MIT
