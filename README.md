# Ultra AI Workstation Desktop

A local web workstation for email, calendar, Slack and a work ledger in one window.
Read, triage, draft with AI, revise, approve twice, send, then log and link the
conversation in the ledger by dragging it into a bucket.

Status: v0.5.1. Mail read and write (double-approval send), AI summary and drafting,
archive with undo, ticket cards, whose-court rules, Slack read, a context rail that
covers everyone on a conversation, and the bucket: drag conversations and people in,
then log or make a task in the ledger with links checked by read-back; web search
and deep-research from any highlight; read aloud and AI audio summaries. Calendar and
Slack replies are next. See [SPEC.md](SPEC.md) section 19.

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

It listens on 127.0.0.1 only by default (port 7440).

To use it from your phone or another laptop on your network or tailnet:

```bash
ultra restart --host 0.0.0.0
ultra remote              # prints the http://<ip>:7440/ addresses to open on the device
```

Only clients on the Tailscale range (100.64.0.0/10) and 192.168 LANs may connect;
everything else is refused. Change the list with `[server] remote_networks`.

## Sending mail

Nothing is ever sent without two clicks on two different screens:

1. **Approve (1 of 2)** locks the exact text. Any later edit sends it back to Draft.
2. **Review and send (2 of 2)** shows the final message, recipients and warnings, then
   sends after a short delay you can cancel.

The server refuses to send if the approval token is unknown, used, expired, or if the
stored message no longer matches what you approved. AI output only ever becomes a new
draft version; it cannot approve or send.

## The bucket (ledger log and linker)

Drag a conversation, a person, a lab or a highlighted snippet into the bucket, then
press Log or Task. A card opens with the date (converted to your time zone), link
chips for every person on the thread who is in the ledger plus their labs, and a
plain template text you can edit or have AI rewrite. Commit runs the ledger CLI,
reads the record back, and marks each chip linked or missing ("Link now" fixes a
missing one). A card commits once; nothing is retried automatically.

The ledger adapter only runs `log`, `tasks add`, `tasks update`, `link` and `unlink`,
passes entities as full UUIDs, and sends text on stdin.

## Tasks and Slack in the stream

Open ledger tasks appear in the stream (Tasks filter, and in All), coloured by priority
and status. Open one to Complete it (with Undo), Start, mark Blocked, change priority,
snooze it for a few days on this laptop, or Log update. Slack conversations have Mark
done: hidden until someone writes again. `e` is Archive for mail, Complete for a task,
Mark done for Slack.

## Research and listening

Highlight text in a message for Copy, Quote in reply, Search ledger, Web search (a
short Google-grounded answer with sources) and Add to bucket; More has Explain,
Search research (your past deep-research runs), Research this, and Read aloud.

The Research tab searches past research, lists recent runs, opens reports, and starts
a new run. Starting costs money, so it only happens from the launcher after an
estimate, and the conversation text is included only if you tick it for that run.

Read aloud uses the browser voice (free, nothing leaves the machine). AI audio makes
a spoken summary or a full read with a Gemini voice; files are cached, stay in the
data folder, and `ultra purge --audio` deletes them.

## Keys

`j`/`k` move, `Enter` open, `r` reply, `a` reply all, `f` forward, `s` AI summary,
`e` archive (never deletes), `b` add to bucket, `l` log, `t` task, `c` compose, `m` Mine, `w` Waiting, `Ctrl+K` commands,
`?` help.

## Configuration

- `config.toml`: your addresses, token paths, VIP list, ticket patterns, AI model.
- `style.toml`: outgoing text rules (ASCII level, forbidden patterns, signature).
- `.env`: `GEMINI_API_KEY` for AI features (optional).

Google access reuses token files you point at in `config.toml`; missing scopes can be
added with `ultra auth google <capability>`.

## License

MIT
