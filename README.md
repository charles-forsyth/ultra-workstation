# Ultra AI Workstation Desktop

A local web workstation for email, calendar, Slack and a work ledger in one window.
Read, triage, ask, draft with AI, revise, approve twice, send, then log and link the
conversation in the ledger.

Status: v1.2.1. See [SPEC.md](SPEC.md) for the full specification (section 19 is the
delivery plan, section 21 the change log).

Nothing in this repo contains credentials or personal data. Runtime config and tokens
live in `~/.config/ultra-workstation/`, the local store in
`~/.local/share/ultra-workstation/`, and private notes in the git-ignored `local/`
folder.

## What it does

- **Desk.** One stream of email, ServiceNow ticket mail, Slack conversations and open
  ledger tasks, sorted by whose move it is (rules, not AI): Mine, Waiting, Tasks, All.
  A context rail shows everyone on the conversation from the ledger.
- **Board** (`o`). My court, Waiting on (grouped by person, with Nudge), Watching
  (park an item until a date), Done today.
- **Today** (`g`). Your calendar: drag an item onto a time to block it, meeting prep,
  find a time, RSVP.
- **Day** (`d`). A morning check-in plan and an end-of-day report from what you did.
- **Ledger tab** (`n`). Search people, labs, projects and tasks; reviewed writes.
- **Graph** (`v`). Your ledger neighborhood: people, labs, projects and their links.
  Click to open a record, double-click to center on it, drag an item onto a node to
  put both in the bucket.
- **Ask Hermes** (`h`). Ask your Hermes agent about the item, day, person or
  selection in front of you; turn the answer into a reply draft, log card, task card
  or bucket snippet.
- **Research and listening.** Search past deep-research runs and start new ones; read
  aloud or AI audio summaries.

### The layout

Ultra opens in the calm layout: three places at the top (Inbox, Today, Ledger), the
search box (Ctrl+K, which also runs every command) and one status dot. Rows show
Archive and `...` when you point at them; an open email shows Reply all, Archive, AI
and `...`. Everything else is in those menus, in the palette and on its key, so
nothing is gone. Prefer every button on screen? Pick "Use the classic layout" in the
status dot menu (this browser), or set `[ui] layout = "classic"` in `config.toml`.

## Install

```bash
uv tool install "git+https://github.com/charles-forsyth/ultra-workstation.git[google]"
ultra config init        # writes config.toml and style.toml (never overwrites)
ultra doctor             # checks setup without printing secrets
ultra start --demo       # synthetic data, nothing real is read or sent
ultra start              # your own accounts
ultra open
```

Pin a release with `...ultra-workstation.git@vX.Y.Z[google]`. To upgrade, run the
install again with `--force`, then `ultra restart`.

It listens on 127.0.0.1 only by default (port 7440).

### Google access

Ultra uses four separate Google tokens so each power can be granted or revoked on its
own: `read` (Gmail read), `modify` (archive, labels), `send` and `calendar`. Their
paths are set in `[google]` of `config.toml`.

- **Already have token files** from another tool of yours? Point `token_read`,
  `token_modify`, `token_send` and `token_calendar` at them.
- **Starting fresh** (recommended for a new install): create a Desktop OAuth client in
  your own Google Cloud project, save it as the `oauth_client` path in config, then
  mint Ultra its own tokens, one per capability:

  ```bash
  ultra auth google --capability read
  ultra auth google --capability modify
  ultra auth google --capability send
  ultra auth google --capability calendar
  ```

`ultra doctor` reports which tokens are present and whether they carry the scope.

### Other tools (all optional)

| Feature | Needs |
|---|---|
| AI summary, drafting, web search, audio | `GEMINI_API_KEY` in `~/.config/ultra-workstation/.env` |
| Slack read and reply | Claude Code with a Slack connector (`claude` on PATH) |
| Ledger, link chips, tasks | the `nexus` CLI (`nexus serve` makes it fast) |
| Ask Hermes | the `hermes` CLI |
| Research | the `deep-research` CLI |
| Hosted ledger / cluster MCP servers (v1.3) | `[mcp.nexus]` / `[mcp.ursa]` in config (URL and the program client id from the server's admin), then `ultra auth nexus` / `ultra auth ursa` once. `[ledger] backend = "mcp"` (v1.4) reads the ledger through it: a person's full history in about 2 s instead of 20. From v1.6 it writes through it too (deletes still need the `nexus` CLI) With `[mcp.ursa]`, a job id in support mail shows a Cluster chip: the job's state, the cause, the end of its log and a draft reply (v1.8) |

Each is checked by `ultra doctor`; a missing one hides its feature, nothing else breaks.

### Phone and other devices

```bash
ultra restart --host 0.0.0.0
ultra remote              # prints the http://<ip>:7440/ addresses to open on the device
```

Only your 192.168 LAN and your own Tailscale devices (same Tailscale user as this
machine, not the whole 100.64.0.0/10 range) may connect; everything else is refused.
Change the list with `[server] remote_networks`. The layout switches to phone size
below 800 px.

## Sending mail and Slack

Nothing is ever sent without two clicks on two different screens:

1. **Approve (1 of 2)** locks the exact text. Any later edit sends it back to Draft.
2. **Review and send (2 of 2)** shows the final message, recipients and warnings, then
   sends after a short delay you can cancel.

The server refuses to send if the approval token is unknown, used, expired, or if the
stored message no longer matches what you approved. AI output (including Ask Hermes
answers and Board nudges) only ever becomes a new draft version; it cannot approve or
send.

Slack replies (`r` on a Slack conversation) go through Claude's Slack connector: Ultra
posts the approved text, reads it back and tells you whether the posted text matches.
It never resends on its own. Slack adds a small "Sent using Claude" line under each
post.

Ticket replies (ServiceNow) are email replies: open the ticket and press Reply all (or
use Draft Studio for a full-context draft). The desk stays on To, the requester on Cc,
and the `Ref:MSG` line the desk needs is kept at the end; approval is blocked without it.

## The bucket and ledger cards

Drag a conversation, a person, a lab or a highlighted snippet into the bucket, then
press Log or Task. A card opens with the date (in your time zone), link chips for
everyone on the thread who is in the ledger plus their labs, and plain text you can
edit or have AI rewrite. Commit runs the ledger CLI, reads the record back, and marks
each chip linked or missing ("Link now" fixes a missing one). A card commits once;
nothing is retried automatically.

The ledger adapter runs only allow-listed commands, passes entities as full UUIDs,
and sends text on stdin.

## Tidy the inbox

The Tidy button next to refresh previews a bulk archive: automated mail, and anything
older than N days (7 by default) that is not your move, VIP, ready, assigned to you,
on your Watching list, or active today. Every row shows why; untick anything to keep
it. Nothing happens until you press Archive. It only removes the Inbox label (nothing
is deleted or marked read), never touches Slack or tasks, and one Undo puts it all
back.

## Tasks and Slack in the stream

Open ledger tasks appear in the stream (Tasks filter, and in All), coloured by priority
and status. Open one to Complete it (with Undo), Start, mark Blocked, change priority,
snooze it for a few days on this laptop, or Log update. Slack conversations have Mark
done: hidden until someone writes again. `e` is Archive for mail, Complete for a task,
Mark done for Slack.

## Board

Press `o`. Four columns built from the same stream:

- **My court**: your move. READY first, then VIPs and tickets assigned to you, then oldest.
- **Waiting on**: grouped by the person you are waiting on, most overdue first.
  **Nudge** writes one follow-up email to that person listing every thread you are
  waiting on them for. It opens as a draft and needs your two approvals.
- **Watching**: items you parked, with an optional date and note (kept only on this
  machine). On the date the item goes back to its column.
- **Done today**: what you archived, completed, logged or sent today.

Drag cards between columns or use the buttons on each card. Done opens a checklist
(archive, mark done, complete the task, log it); nothing runs until you press Do it.

## Today (calendar)

Press `g` or click Today. Drag an email, Slack conversation or task onto a time to block
it on your calendar (only you, nobody is invited). Drag your own blocks to move them;
click one to rename or delete it. Click a meeting for prep: the invite notes and who is
coming, and click a person for their ledger context. Find a time checks free/busy for
you and anyone you list. Ultra only ever changes blocks it created.

## Ask Hermes

Press `h` (or the magenta Ask button) on an item, the Day view, a ledger person or lab,
a research report or a highlighted selection. The panel shows exactly what will be
sent; nothing runs until you press Ask. Hermes runs read-only (session search, plus web
if you tick it for that question). Under each answer: **Use as reply**, **Log it**,
**Task from it**, **+ Bucket**. Follow-ups continue the same session, which you can
also resume from the terminal (`hermes sessions list --source ultra`).

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

Press `?` in the app for this list. Keys work when you are not typing in a box; item
keys need an item open. Browser shortcuts (Ctrl+R, Ctrl+F, ...) are left alone.

| Key | Action | Key | Action |
|---|---|---|---|
| `j / k` | next / previous item | `c` | compose a new email |
| `Enter` | open the selected item | `o` | Board |
| `v` | Graph | | |
| `/` | search mail | `d` | Day |
| `Ctrl+K` | command palette | `g` | Today (calendar) |
| `Esc` | close the open view or dialog | `n` | Ledger tab |
| `r` | reply (email, Slack) | `m` | stream: Mine |
| `a` | reply all | `w` | stream: Waiting |
| `f` | forward | `T` | stream: Tasks |
| `s` | AI summary | `R` | refresh mail and Slack |
| `h` | Ask Hermes | `?` | keyboard help |
| `e` | archive / complete task / mark Slack done | `Ctrl+Enter` | Ask (in the Ask box) |
| `b` | add to bucket | | |
| `l` | log it in the ledger | | |
| `t` | make a ledger task | | |

No key sends anything: email and Slack need two approvals, and ledger writes open a
card first. Archive has Undo.

## Commands

| Command | What it does |
|---|---|
| `ultra start [--host H] [--port P] [--demo]` | start in the background |
| `ultra stop` / `restart` / `status` / `open` | manage the background server |
| `ultra serve` | run in the foreground (Ctrl-C to stop) |
| `ultra doctor` | check config, permissions, tokens, tools, keys (prints no secrets) |
| `ultra remote` | addresses other devices can open |
| `ultra auth google --capability C` | mint a Google token (read, modify, send, calendar) |
| `ultra auth nexus` / `ultra auth ursa` | sign in to a hosted MCP server; `--status` shows who, `--sign-out` forgets it |
| `ultra purge --audio / --uploads / --attachments` | delete local copies |
| `ultra config init` / `ultra config path` | write example config / show the folders |

## Configuration

- `config.toml`: your addresses, time zone, token paths, VIP list, ticket patterns,
  AI model, ledger and Hermes settings, remote networks. `ultra config init` copies
  the annotated example (`src/ultra/config.example.toml`).
- `style.toml`: outgoing text rules (ASCII level, forbidden patterns, signature).
- `.env`: `GEMINI_API_KEY` for AI features (optional).

## Development

```bash
uv sync --all-extras
scripts/gauntlet.sh      # lockfile, lint, format, mypy, JS syntax, tests, private-content check
uv run ultra serve --demo --port 7449
```

CI runs the same gauntlet plus a secrets scan. One branch and PR per change; update
SPEC.md (change-log row and spec version) with every feature.

## License

MIT
