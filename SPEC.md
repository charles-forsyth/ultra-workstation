# Ultra AI Workstation Desktop: Specification

Status: v0.30 of the spec; app at v0.12.1 (mail, calendar, Slack, Day, Draft Studio, ledger desk writes, and the Ledger tab with reviewed writes; see the delivery plan in section 19)
Repo: ultra-workstation (planned public GitHub repo, installed as a uv tool)
CLI: `ultra` (working name; see open question Q1)
Last updated: 2026-10-01

This document is public. It must never contain real names, email addresses, NetIDs,
Slack IDs, billing or project IDs, ticket numbers, or anything else specific to one
operator. Operator specifics live in `~/.config/ultra-workstation/` and in the git-ignored
`local/` folder. Examples use `example.org` and made-up people (Ada, Ben, Cy).

---

## 1. Purpose

A local web workstation that puts one person's work communications and their work ledger
in one window:

- Email (Gmail): read, triage, draft, revise, send, archive.
- Calendar (Google Calendar): today, free/busy, focus blocks.
- Slack: read DMs, group DMs and mentions; reply after review.
- Ticket notices (ServiceNow by email): grouped per ticket, replied to by email.
- Ledger (the `nexus` CLI): see who someone is and what's open with them; log
  interactions, create tasks and link records by dragging items into a bucket.
- Research (the `deep-research` CLI): search past research and start new research
  from any highlighted text; a panel lists runs and opens their reports.
- Listening: read any thread, brief or report aloud, or make an AI voice summary.

It is built around one daily loop that currently takes many chat turns:

    read thread -> "what are they actually asking?" -> draft -> revise (many rounds)
    -> approve -> send -> log in the ledger, linked to the person, lab, project, org

and around one failure it must prevent: a peer says "done" or "ready" (by email or
Slack), the ball moves to the operator, and nothing shows it.

## 2. Goals and non-goals

Goals

- G1. Reading, drafting and logging one thread takes under a minute, not a chat session.
- G2. "Whose move is it" is visible for every open conversation across email and Slack.
- G3. Nothing leaves the machine (mail, Slack post, ledger write) without explicit,
  reviewed approval. Mail and Slack need two separate approvals (section 9).
- G4. The ledger stays the source of truth and is written only through its CLI.
- G5. Safe to publish: no credentials or private data in the repo, ever.
- G6. Fast feel even though the ledger CLI takes 6-10 s per call and Slack reads take
  20-90 s: cache, prefetch, never block the UI on a slow source.

Non-goals (v1)

- Not a general mail client (no multi-account, no rules engine, no filters UI).
- No background AI. Models run only when a button is pressed.
- No automatic sending, archiving, logging or Slack posting, ever.
- No direct database access to the ledger.
- No ServiceNow web/API writes; tickets are handled through email only.
- No mobile layout in v1 (laptop screen, 1440 px and up; must still work at 1280).
- No multi-user or remote access. Loopback only.

## 3. Principles

1. The CLIs and APIs are the source of truth; the app is a view plus a staging area.
   Every write is a call to Gmail, Calendar, Slack (via Claude Code) or the ledger CLI,
   and every write is read back to confirm it landed.
2. Glass wall: anything a model produces (summary, draft, suggested link, suggested
   task text) appears as an editable card and is inert until the operator commits it.
3. Deterministic first. Court state, link resolution, ticket grouping and noise
   filtering use rules and regexes. Models are for language (drafts, summaries) and
   for suggestions only when rules find nothing.
4. Least privilege per action: read-only credentials for reads, modify for archive and
   labels, send only at send time.
5. Archive, never delete. The app has no trash, delete or mark-as-read action.
6. Every write is journaled (what, when, target ids, exit status) so end-of-day
   reporting and undo are possible.
7. Plain text first. Outgoing mail is plain text, ASCII-checked; HTML mail is rendered
   read-only in a sandbox.
8. Same proven stack as the operator's other local dashboard: stdlib HTTP server,
   vanilla JS, no build step, loopback guard.

## 4. The operator's daily loops (requirements source)

Taken from about a month of real usage (counts are the operator's own requests over
30 days, summarized, no content):

| Loop | Share | What the app must do |
|---|---|---|
| Email read/draft/revise/send | largest (~885 requests) | Thread view, AI draft, revision loop, two-step send |
| Ledger log / link / task | ~500 | Drag-to-bucket, pre-resolved link chips, read-back |
| Check-in / day plan | ~200 | Today view, "in my court" list, calendar blocks |
| Calendar | ~170 | Focus blocks, private blocks, free/busy slot finder |
| GCP/budget work | ~180 | Out of scope; context rail can show linked projects |
| Tickets | ~40 | Ticket cards from notification mail |
| Slack | ~30 via chat, more via Claude Code | Stream + reply with double confirm |

Recurring sub-asks the UI answers without a chat turn:

- "Did we already reply to X?" -> Replied badge (Sent + ledger), days waiting.
- "What is he actually asking me?" -> Ask summary button on the thread.
- "Send it as reply-all and log and link it to his project, GCP, org" -> Send, then
  a log card prefilled with those links.
- "Archive anything not in my court, keep what I answered today" -> Tidy action with
  preview and undo file.
- "Check Slack for replies from X" -> Slack items appear in the same stream, per person.
- "Put blocks on my calendar for this, private" -> drag to Today or the Block target.

## 5. Architecture

    browser (127.0.0.1:7440)
       |  JSON over HTTP, CSRF token
    ultra server  (Python stdlib ThreadingHTTPServer)
       |-- MailAdapter      -> Gmail API (google-api-python-client)
       |-- CalendarAdapter  -> Google Calendar API
       |-- SlackAdapter     -> `claude -p` with the Slack connector (read-only tool list;
       |                        a send-only tool list at send time)
       |-- LedgerAdapter    -> `nexus` CLI (--json reads, file-fed writes)
       |-- TicketParser     -> rules over MailAdapter results
       |-- AIAdapter        -> Gemini API (google-genai), on demand only
       |-- AudioAdapter     -> Gemini TTS (same key), ffmpeg for MP3 (optional)
       |-- ResearchAdapter  -> `deep-research` CLI (search, start, list, show, estimate)
       |-- Store            -> SQLite in the user data dir (cache, drafts, approvals, journal)
       `-- Jobs             -> small worker pool for slow calls (ledger, Slack, AI)

Runtime layout (outside the repo):

    ~/.config/ultra-workstation/
        config.toml              operator settings (section 13)
        .env                     GEMINI_API_KEY and similar (mode 600)
        oauth_client.json        Google OAuth client (mode 600), if not reusing one
        tokens/                  per-scope OAuth tokens (mode 700 dir, 600 files)
        style.toml               outgoing-text rules (section 10)
        vip.txt                  VIP senders (optional; can point at an existing list)
    ~/.local/share/ultra-workstation/
        state.db                 SQLite (mode 600)
        journal.jsonl            append-only write journal (mode 600)
        undo/                    archive undo files
        audio/                   generated audio files (mode 700 dir)
        attachments/             attachments saved on request (mode 700 dir)
        ultra.log                server log (no bodies, no tokens; section 12)
        ultra.pid

Process model: `ultra start` detaches the server (PID file); `ultra open` opens the
browser; `ultra stop` / `ultra status` / `ultra restart`. Same pattern as the
operator's existing dashboard daemon.

### 5.1 Dependencies

- Python 3.12+, uv-managed. Runtime deps kept small: `google-api-python-client`,
  `google-auth`, `google-auth-oauthlib`, `google-genai`. Everything else stdlib.
- External binaries (optional, detected by `ultra doctor`): `nexus` (ledger), `claude`
  (Slack connector), `deep-research` (research panel), `ffmpeg` (MP3 audio; WAV
  without it). The app runs without any of them; the matching panels show "not
  configured".
- Frontend: vanilla JS modules, vendored `marked` and `DOMPurify` (license files kept).
  No npm, no bundler.

## 6. Data model (local store)

SQLite tables. All ids are the source system's ids where one exists.

| Table | Purpose | Key fields |
|---|---|---|
| `items` | One row per stream entry (email message, Slack message, calendar event, ticket notice) | `source`, `source_id`, `thread_key`, `person_key`, `ts_utc`, `subject`, `snippet`, `from_addr`, `labels`, `court` |
| `threads` | Conversation roll-up | `thread_key`, `source`, `last_from_me`, `last_ts`, `court`, `court_reason`, `done_signal` |
| `people` | Person resolution cache | `person_key` (email or Slack id), `display`, `ledger_id`, `ledger_kind`, `confidence`, `resolved_at` |
| `ledger_cache` | Cached CLI JSON | `cmd_key`, `json`, `fetched_at`, `ttl_s` |
| `drafts` | One per reply/new message | `draft_id`, `kind` (mail/slack), `thread_key`, `gmail_draft_id`, `state`, `approved_version`, `approved_hash` |
| `draft_versions` | Every revision | `draft_id`, `version`, `to`, `cc`, `bcc`, `subject`, `body`, `author` (me/ai), `instruction`, `lint_json`, `created_at` |
| `approvals` | Send approvals (section 9) | `token`, `draft_id`, `version`, `content_hash`, `issued_at`, `expires_at`, `used_at` |
| `bucket` | Current linker bucket contents | `slot`, `kind`, `ref`, `added_at` |
| `ledger_writes` | Staged and committed ledger writes | `id`, `kind` (log/task/link), `payload_json`, `state`, `result_json` |
| `journal` | Mirror of journal.jsonl for queries | `ts`, `action`, `target`, `ok`, `detail_json` |
| `audio_exports` | Cached audio (7.10) | `id`, `kind`, `ref`, `mode` (read/summary), `voice`, `text_hash`, `path`, `seconds`, `cost_usd`, `created_at` |
| `research_runs` | Runs started from Ultra (7.9) | `run_id`, `question`, `started_at`, `source_ref`, `status`, `last_checked` |
| `saved_searches` | Named mail searches | `name`, `query`, `created_at` |
| `annotations` | Local highlights and notes (7.8) | `id`, `thread_key`, `message_id`, `start`, `end`, `quote`, `color`, `note`, `created_at` |

Retention: `items` and caches older than 30 days are pruned; drafts are kept until sent
or discarded, then 90 days; annotations until deleted; journal kept forever (it is small
and is the audit trail).

## 7. Screens

### 7.0 Visual design

Ultra is its own app. It is not an extension of the deep-research dashboard and does not
share its data or embed it; it talks to deep-research only through its CLI (7.9), with
an optional plain link to its dashboard. It borrows that dashboard's look (dark engineering
console) and its export and selection tools (7.8). The frontend helpers named below are
adapted from deep-research (MIT, same author), so copying code is fine; keep the license
header.

Design tokens (CSS custom properties, one `theme.css`):

| Token | Value | Use in Ultra |
|---|---|---|
| `--bg` / `--bg-2` | `#07090d` / `#0b0f15` | page, side panels |
| `--panel` / `--panel-2` | `#0e131b` / `#121926` | cards, composer, rail |
| `--line` / `--line-2` | `#1c2533` / `#26324a` | borders, dividers |
| `--text` / `--dim` / `--faint` | `#d6deeb` / `#8b98ad` / `#7a869b` | body, secondary, tertiary (4.5:1 contrast) |
| `--cyan` | `#22d3ee` | primary action, selection, focus ring, links |
| `--amber` | `#f5a524` | WAITING, warnings, lint warnings, send countdown |
| `--magenta` | `#e879f9` | anything a model wrote (AI drafts, summaries, suggested chips) |
| `--green` | `#34d399` | done, linked (read-back ok), sent, source healthy |
| `--red` | `#f87171` | MY COURT overdue, READY signal, errors, missing link |
| each color `-d` variant | same hue at 12-16% alpha | tinted backgrounds for badges and rows |
| `--mono` | JetBrains Mono, IBM Plex Mono, ui-monospace, ... | ids, times, counts, status bar |
| `--sans` | Inter, IBM Plex Sans, system-ui, ... | UI and message text |
| `--serif` | Iowan Old Style, Charter, Georgia, ... | long reading view (optional per user) |
| `--radius` | 6px | all cards and buttons |

- Magenta is reserved for model output, so the glass wall (principle 2) is visible at a
  glance: an AI draft is magenta-edged until the operator edits or approves it; an
  approved version turns cyan; a sent one green.
- Base font 13px/1.5; faint engineering grid on the page background; thin custom
  scrollbars; `.kbd` key hints in menus and tooltips.
- Fonts are system or vendored files only. No web font or CDN request (the CSP in 12.1
  allows `'self'` only).
- Layout pieces taken from the same pattern: top bar with a live telemetry strip (time,
  next meeting, per-source sync), three-pane layout with collapsible side panels that
  become slide-out drawers under 1200 px, tab strip, bottom status bar, toasts,
  Ctrl-K palette, and a modal helper (Escape closes, focus trap, dirty-check before
  closing an edited composer).
- Frontend helpers to port: `toast()`, `busy(btn, fn)` (disable + spinner while a call
  runs), `safe()`, `MODAL`, `confirmBox()` (never confirms on a stray Enter),
  `copyText()` (clipboard with fallback), `download()`, `standaloneHtml()`, the
  selection bar, and the print stylesheet.
- A light theme is not in v1. Print output is always light (7.8).

### 7.1 Desk (default view)

    +------------------------------------------------------------------------------+
    | Tue 9/29 10:52  | next: Office Hours 1:00 (optional) | Slack 2 new | sync ok  |
    +---------------------+-------------------------------------+------------------+
    | STREAM              | THREAD                              | CONTEXT          |
    | [Mine 5][Waiting 8] | Re: Lab project handover            | Ada Lovelace     |
    | [All][Slack][Tix]   | Ben: "The project is ready. Please  | Lovelace Lab     |
    |                     |  work with Ada to hand it over."    | proj: ada-lab    |
    | ! Ben (VIP)   2h    | Ada: "When will the account be      | Open tasks: 2    |
    |   Lab handover      |  ready?"                            | Last log: 9/28   |
    |   READY SIGNAL      |                                     | Replied: NO, 1d  |
    | # Cy (Slack)  3h    | [Ask summary] [Reply] [Reply all]   +------------------+
    |   budget updated    | [Draft with AI] [Archive]           | BUCKET        (3)|
    | T RITM ... 1d       |                                     | [email][slack]   |
    |   comments added    |                                     | [Ada]            |
    |                     |                                     | Log | Task | Blk |
    +---------------------+-------------------------------------+------------------+

Stream

- Merges email threads, Slack conversations and ticket cards, sorted by last activity.
- Grouping toggle: by thread (default) or by person (all of Ada's email and Slack
  together).
- Filters: Mine, Waiting, All, Slack, Tickets, VIP. Counts on each.
- Row markers: `!` VIP, `#` Slack, `T` ticket, a READY badge for a done-signal (7.3).
- Noise (newsletters, alerts, automated notices from configured senders) is collapsed
  into one "Low priority (N)" row at the bottom.

Thread

- Email: plain-text body by default, quoted text folded. "Show original" renders HTML
  in a sandboxed iframe (section 12.4). Attachments listed with name, type and size;
  Save (to the attachments folder, or browser download) and Preview for text, images
  and PDF, on click only (8.1).
- Slack: messages in the conversation or thread, with a link to open it in Slack.
- Ticket: every notice for that ticket number in time order, with state changes pulled
  out (assigned, comment added, resolved). Own-comment echoes are dimmed.
- Actions: Ask summary, Reply, Reply all, Forward, Draft with AI, Archive, Labels,
  Add to bucket, Snooze to Waiting, Listen (read aloud / AI voice summary, 7.10),
  Export (7.8).

Context rail (ledger)

- Resolved person: name, title, department, lab(s), linked projects.
- Open tasks that reference them; last 5 interactions (date + one-line summary).
- Replied badge: YES (date) / NO (days waiting), from Sent mail plus ledger entries.
- Loads asynchronously. Shows cached data with its age immediately, refreshes behind.
- Unresolved sender: "Not in ledger" with Search and "Add person" (the latter opens a
  staged `people add` card; nothing is created without Commit).

### 7.2 The Bucket (log and linker bucket)

The bucket is a tray at the bottom of the context rail that collects things, then turns
them into one ledger write.

- Drag in: email messages or threads, Slack messages, calendar events, ticket cards,
  ledger entities (from the context rail or search), or a text snippet selected in a
  thread.
- Several items can go in at once, e.g. the email, the Slack follow-up and the meeting
  about the same topic become one interaction.
- Three drop actions (also the three buttons on the bucket):
  - Log: builds a staged interaction (7.2.1).
  - Task: builds a staged task (7.2.2).
  - Block: builds a staged calendar block (7.4).
- Dragging a single item straight onto Log/Task/Block skips the tray.
- The bucket persists across reloads (table `bucket`) and has Clear.

#### 7.2.1 Staged interaction card

    LOG INTERACTION                                    [Commit] [Discard]
    Date  [2026-09-29 12:04]   (from the email Date header, converted to local time)
    Links [Ada Lovelace x] [Lovelace Lab x] [ada-lab x] [Me x] [+ add]
    Text  +-----------------------------------------------------------+
          | Email, reply-all to Ben and Ada: ...                        |
          +-----------------------------------------------------------+
    [Draft text with AI]   Lint: ok

- Links are chips pre-resolved by the rules in section 11; each shows its source
  (sender, regex match, graph edge) on hover. Suggestions from a model are shown dashed
  and are not included until clicked.
- Text defaults to a deterministic template (source, participants, subject, key lines,
  local time). "Draft text with AI" rewrites it into the operator's log style; still
  editable.
- Commit runs the ledger write (section 8.4), then reads back the record and turns
  every chip green (linked) or red (missing), with a one-click "Link now" on red chips.
- The resulting interaction id is shown and copied to the clipboard.

#### 7.2.2 Staged task card

- Text, priority (LOW/MEDIUM/HIGH/CRITICAL), link chips (default: people and projects
  from the bucket plus "assigned to me"), optional follow-up date.
- Due date (ledger 0.1.206+): the task card has a Due field (date picker plus Today /
  Tomorrow / +1 week / None). Only an absolute `YYYY-MM-DD` reaches the ledger
  (`tasks add --due`); the review confirm shows it and the commit steps confirm it.
- Commit: `tasks add --json` (id from JSON, prose fallback for older ledgers), create
  the links, read back.
- Open tasks can change their due date in the task view (date input or Quick: today,
  tomorrow, this Friday, a week, two weeks, clear). Each change is confirmed
  (old -> new), written by exact UUID (`tasks update <uuid> --due D | --clear-due`),
  and read back from the ledger before success is shown. Stream rows carry
  OVERDUE / DUE TODAY / DUE SOON badges and overdue sorts above its priority band.

### 7.3 Board (court view)

Toggle on the Desk stream: the same threads as columns.

    MY COURT (5)       | WAITING ON (8)          | WATCHING (4)   | DONE TODAY (6)
    Ada: account?      | Cy: 4 open items      | Cy copy, 10/2  | Lab VM resize
      1d, VIP, READY   |   4d, last ask 9/25     |                |   logged 1a2b3c4d

- Court is computed by rules (section 11.3).
- Waiting On groups by the person being waited on. "Nudge" drafts one follow-up
  listing all their items (goes through the normal draft and approval flow).
- Drag between columns:
  - to Done: opens a combined card: archive the thread, log it, close a linked task.
    Each part has its own checkbox; nothing runs until Commit.
  - to Waiting: asks for a follow-up date, stages a task and optional calendar block.
  - to Watching: local-only flag (no external write).
- Watching items with a date (from task text) move back to My Court on that date.

### 7.4 Today (calendar view)

- Vertical timeline of today (and a tomorrow tab) from the operator's calendar, with
  attendees' response status and the video link.
- Left: "To place": My Court threads and open high-priority tasks.
- Drag any thread, task or bucket onto a free slot: stages a Focus block (default 30
  minutes, title `Focus: <subject>`, description holds the thread summary, the draft
  text if one exists, and the task id). Work blocks use the configured work color and
  default visibility; items marked personal use private visibility.
- Meeting card: prep panel with the context rail for each attendee who resolves to
  the ledger, plus the invite's own description. Never modifies someone else's event.
- Slot finder: pick attendees, get mutual free windows (free/busy API), click one to
  insert it into the current draft as text.
- Check-in button (v0.4): builds a proposed plan (fixed meetings, then My Court oldest
  first, quick unblocks, a logging slot at the end) as ghost blocks; accept one by one.
- *Week view (v0.10).* Day / Week switch (remembered). Monday-to-Sunday grid from one
  read, all-day row, now line, work-hours band; events the operator has not answered are
  striped; a "Needs your answer" list sits on top with Yes / Maybe / No / Open. Click a
  day's name to open it; click a meeting for its panel; click an Ultra block to jump to
  that day, where it can be dragged.
- *RSVP (v0.10).* Yes / Maybe / No on any invitation the operator is a guest on (not
  their own events). The page asks once (Decline asks for an optional note to the
  organiser) and names the event, time and that Google tells the organiser; a
  recurring invite is answered for that occurrence only. The server issues a
  single-use 5-minute token bound to the event and the response; the write changes only
  the operator's own attendee entry (`sendUpdates=all`) and the read-back checks that
  the answer took and no other guest's status changed.
- *Repeating focus blocks (v0.10).* The block card has "Repeat: every week on" with
  weekday boxes and 2-26 weeks. One RRULE with an UNTIL, so a series never runs on
  forever; same tag, visibility and colour rules as a single block, no attendees; one
  confirm. Any block of a series offers "Delete series" (only Ultra-tagged series).
- *Meeting prep (v0.10).* The meeting panel: "People & context" puts everyone on the
  invite in the right rail (the event is an item, key `c-<calendar>~<event id>`, so the
  People / Full tabs and the cited AI briefing work exactly as for email); "Prep
  briefing" shows that briefing in the panel (editable, copy, rebuild); "Log this
  meeting" stages the normal log card with the guests as chips, the meeting time, and a
  text scaffold (title, who accepted, notes). Nothing is written until Commit.
- *Meetings with guests (v0.10), two approvals like email.* "New meeting" (Today and
  Week) opens a card: title, day, start, length, guests, Meet link on/off, location,
  notes, and "Check their free time" (shared free windows; calendars Google won't show
  are named). Approval 1 stores the invite and locks it (hash of every field; any edit
  voids the approval and every token). Approval 2 is a review screen with the exact
  invite, who is busy then, whose calendar can't be seen, and guests outside the
  operator's domain; "Not yet" is focused and Send is disabled for 2 s. Send presents a
  single-use token that expires in 10 minutes; the server re-checks the token, the
  invite id, the approved hash and that the start is still in the future, then creates
  the event (`sendUpdates=all`, `guestsCanModify=false`) and reads it back (time, guest
  list, tag). The server refuses non-addresses, more than 40 guests, lengths outside
  5 min - 8 h, and times in the past; the operator's own addresses are dropped from the
  guest list. A sent meeting is read-only in Ultra (it has guests, so moving or
  deleting it would notify people): `is_ultra_block` is false for anything with
  another guest or `ultra_kind=meeting`.
- End-of-day button (v0.4): compiles the day from the journal, Sent mail and today's
  ledger entries into a staged report (DONE / NOT DONE / WAITING ON / UPCOMING /
  TOMORROW FIRST), which is logged through the normal staged interaction card.

### 7.5 Command palette (Ctrl-K)

- Fuzzy commands (every action in the app), people and entity search through the
  ledger, and "Ask" (free-text request to the AI adapter, answered as cards: a draft,
  a staged log, a staged task). Ask never executes anything by itself.

### 7.6 Graph (v1.x, optional)

- The operator's neighborhood from `nexus tree --json`: people, labs, projects, recent
  interactions. Drag stream items onto nodes to add them to the bucket with that link.
  For exploring back story, not for triage.

### 7.7 Keyboard

| Key | Action | Key | Action |
|---|---|---|---|
| j / k | next / previous item | e | archive (with undo toast) |
| Enter | open | r / a / f | reply / reply all / forward |
| b | add to bucket | l | log (bucket or current item) |
| t | task | c | calendar block |
| s | ask summary | d | draft with AI |
| m / w | Mine / Waiting filter | g d / g b / g t | Desk / Board / Today |
| / | search | Ctrl-K | palette |
| Ctrl-Enter | approve (in composer) | ? | help overlay |
| n | search Nexus for selection | g r | Research panel |
| p | read aloud (play/pause) | Shift-p | AI voice summary dialog |

Destructive or external actions are never on a single unmodified key: archive has undo,
send needs the two-step flow.

### 7.8 Export, copy and selection tools

Modeled on the deep-research dashboard's Export menu and selection bar. Every export is
generated locally and downloaded by the browser (or copied); nothing is uploaded or
written anywhere else. Exports are available on these objects:

| Object | Formats |
|---|---|
| Email thread / Slack thread / ticket card | Markdown, plain text, standalone HTML, JSON, `.eml` (email only, original message), Print / PDF |
| Stream selection or filter (e.g. "Waiting, 12 items") | Markdown checklist, CSV, JSON |
| Board | Markdown by column, CSV |
| Today / a date range of calendar | Markdown agenda, `.ics`, Print / PDF |
| Context rail (a person's ledger view) | Markdown brief, JSON, Print / PDF |
| Staged or committed ledger card | Markdown, the exact `nexus` command it ran (copy only) |
| Draft (any version) | Markdown, plain text, diff between two versions |
| End-of-day report / check-in plan | Markdown, standalone HTML, Print / PDF |
| Journal (date range) | CSV, JSON |

Behavior shared by all exports:

- Export menu on each object's header ("Export..." select, as in deep-research), plus
  "Copy" as a first-class button next to it.
- Markdown exports carry a small header (title, date range, source, generated time in
  local zone) and keep message order; quoted text stays folded as `>` blocks.
- Standalone HTML embeds the app stylesheet and the rendered content in one file that
  opens offline (the `standaloneHtml()` approach). No scripts inside it.
- Print / PDF uses a print stylesheet: white background, black text, chrome hidden
  (bars, panels, tabs, toasts), one message per block with page-break avoidance, and
  highlights printed as light yellow.
- JSON exports use the app's internal shapes, documented in `docs/EXPORT.md`, stable
  within a major version.
- Copy formats: "Copy as text", "Copy as Markdown", and for ids "Copy id". The
  clipboard helper falls back to a hidden textarea when the Clipboard API is blocked.
- File names: `<kind>_<short-subject>_<YYYY-MM-DD>.<ext>`, ASCII only.
- Privacy reminder: exports contain real mail and ledger data. They are never written
  inside the repo checkout; the default download folder is the browser's, and the
  `.gitignore` in 12.5 also covers `exports/`.

Selection bar (appears when text is selected in a thread, draft or ledger view):

- Copy.
- Quote into reply: inserts the selection as a `>` quote into the open composer (or
  opens one).
- Add to bucket: adds the selection as a snippet item, keeping a pointer to its source
  message so the log text can cite it.
- Highlight (amber by default; cyan, magenta, green via the arrow). Highlights are local
  annotations stored in `annotations` (thread key, message id, text range, color, note)
  and appear in Markdown exports as `==text==` or bold, and in Print as yellow.
- Note: attach a short private note to the highlight. Notes are local only and are
  never sent or logged unless the operator adds them to the bucket.
- *As built (v0.11).* Selection bar: Highlight (amber); More: Highlight with note, cyan,
  magenta, green. Stored in `annotations` (thread key, message id, quote, 40 chars
  before it, colour, note); re-applied when the thread opens by finding the quote in
  that message. A "Highlights and notes" box above the messages lists them; clicking a
  highlight or Edit changes colour or note or removes it. Highlights stay within one
  paragraph. Thread Export menu: Markdown (highlights as `==text==` with
  `[note: ...]`), plain text, JSON (messages, attachment names, highlights), Copy as
  Markdown, Print / PDF (print stylesheet: no chrome, highlights light yellow).
- Search Nexus: runs `nexus search "<selection>" --json` and shows results in a popover
  (type, name, score). Click a result to open it in the context rail, add it to the
  bucket as a link chip, or copy its id. Selections over 200 characters are refused
  with a hint to select a name, id or phrase.
- Search research: runs `deep-research search "<selection>"` (semantic search over past
  research with a cited answer) and shows the answer and the matching run ids in the
  Research panel (7.9).
- Research this: opens the research launcher (7.9) with the selection as the question,
  editable, with its cost estimate. Nothing starts until Start is pressed.
- Read aloud: reads just the selection with the browser voice (7.10).
- Explain: on-demand AI explanation of a term or passage in the context of the thread
  (magenta card, not saved unless copied or added to the bucket).
- Web search: a quick grounded answer from Google Search (Gemini with the
  `google_search` tool) for the selection or a typed question. Returns a short answer
  with numbered source links (title + URL from the grounding metadata) in a magenta
  card with Copy / Add to bucket / "Research this" (to escalate to a full deep-research
  run). Seconds and cents, not minutes and dollars. Only the selection or typed
  question is sent, never the whole thread, unless "include thread" is ticked.

The bar keeps the most common five visible (Copy, Highlight, Search Nexus, Web
search, Add to bucket) and puts the rest under "More", with keyboard shortcuts shown. Highlights can
also carry a function: clicking an existing highlight reopens the bar for it, so a
highlighted name can be searched again later or turned into a link chip.

AI brief builder (on threads, the context rail, a Board column, or a date range):

- Styles: executive brief, meeting prep sheet, status email, slide outline. Output is
  Markdown in a magenta-edged card with Copy / Export / "Open as draft" (which starts
  a new composer in DRAFT state; it still needs both approvals to send).
- The prompt rule from deep-research carries over: keep every source reference
  (message date + sender, ledger id) attached to the claim it supports; add no facts
  that are not in the input.

### 7.9 Research panel (deep-research)

A right-side panel (toggle `g r`, or opened by a selection action). Ultra is a client
of the `deep-research` CLI here: it runs the CLI's commands and reads their output. It
does not read deep-research's database or files directly, and it does not embed or
proxy the deep-research dashboard.

- Search tab: a query box (prefilled from a selection) that runs `deep-research search
  "<q>" --limit N`. Shows the cited answer and the matching run ids. Each id has Open
  (shows the report in the panel via `deep-research show <id>`), Copy, Add to bucket
  (so a research report can be cited in a ledger log), and "Open in deep-research"
  (a plain link to the operator's deep-research dashboard URL from config, if set).
- Runs tab: recent runs from `deep-research list --limit 20`, with status. Open shows
  the report (rendered Markdown, sanitized), with Copy, Export (7.8) and Listen (7.10).
- Launcher: question (from selection or typed), depth and breadth, and an "Include
  this thread" checkbox. Including the thread is the operator's choice per run: when
  ticked, the thread text is written to a temp file (mode 600, deleted after the run
  finishes) and passed with `--upload`, and the dialog states that the mail content
  goes to Gemini's research agent. Shows `deep-research estimate` output before Start. Start runs
  `deep-research start "<q>" [--depth D --breadth B] [--upload <file>]` detached and
  records the new run in the journal. This spends money, so Start is a single
  deliberate button in a dialog showing the estimate; no keyboard shortcut starts it.
- A finished run posts a toast with Open. Ultra checks run status only while the panel
  or a started run is open (no background polling otherwise).
- Research context in a draft: "Insert summary" from a report puts a short, cited
  summary (AI, magenta) into the composer, as a normal draft edit.
- Every call uses the CLI's `--json` output (deep-research v0.36.0+): one JSON document
  on stdout, logs on stderr, non-zero exit with `{"error": ...}` on failure. `ultra
  doctor` checks the installed version supports it.

### 7.10 Listening: read aloud and AI audio

Two modes, same as deep-research, available on threads (email, Slack, ticket), a
selection, a draft version, the context rail brief, AI briefs, research reports shown
in the panel, the Today agenda and the end-of-day report.

Read aloud (browser voice, free, word for word)

- Uses the browser's `speechSynthesis` with the operator's chosen English voice and
  rate. A listen bar appears: play/pause, previous/next block, rate, voice, stop.
- The block being read is highlighted and scrolled into view. Quoted history, long
  URLs, signatures and tracking text are skipped; ids are read as characters.
- Nothing leaves the machine.

AI voice (Gemini TTS, costs money, cached)

- Two outputs:
  - Full read: the text read word for word by a Gemini voice.
  - AI voice summary: a model first writes a 1-3 minute spoken briefing (who wants what,
    by when, what the operator owes), then it is voiced. For a day or a stream filter,
    it is a spoken run-down ("Morning brief": meetings, My Court, READY signals).
- Dialog before creating: voice picker (the Gemini prebuilt voices, remembered),
  estimated length and cost, and the model name. Create runs as a job with progress.
- Implementation follows deep-research: text is cleaned for speech, split into chunks
  of about 3,500 characters, each chunk synthesized and joined into one 24 kHz mono
  WAV, converted to MP3 with ffmpeg when present. Files go to the `audio/` data folder
  and a row in `audio_exports`; the same text + mode + voice reuses the cached file.
- Player: an audio bar with play/pause, seek, speed and download. The server serves
  audio with HTTP Range support so seeking works.
- Export menu entries: "Audio: read aloud (AI voice)" and "Audio: AI voice summary" on
  every object in 7.8 that has text.
- Privacy: AI voice sends the text to Gemini, like any AI action. Audio files contain
  real mail content; they stay in the data folder (mode 600), never in the repo, and
  `ultra purge --audio` deletes them.
- Model ids and prices are config values (`[audio]`), not hard-coded.

### 7.11 Ledger tab

A full dashboard for the ledger tool, opened from the top-bar Ledger button, `n`, or
the palette. It fills the centre pane like Day and Today.

- **Source.** Reads go through the ledger's local server (`nexus serve`, 8.4) when it
  is running, otherwise the CLI. The header shows which. All reads use the existing
  read allow-list and cache; Refresh bypasses the cache.
- **Home.** Counts per entity type, open and overdue tasks, logs this week, the
  overdue list, the next due dates, blocked tasks and recent interactions. Dates use
  the server clock.
- **Browse and search.** Lists for people, labs and units, GCP projects, projects,
  grants and assets, with a filter; full-graph search from the header.
- **Entity pages.** The record (including details, cached cloud scan, documents and
  links), its connections grouped by type (each with Unlink), open tasks, interactions,
  the write actions allowed for that kind, and an AI briefing. The briefing is written
  from the page's data only, cites ids as `[E:xxxxxxxx]` / `[L:...]` / `[T:...]`,
  flags any cited id that is not in the data, is editable and is never saved unless
  logged. Citation chips jump to the source row.
- **Task board.** TODO, In progress, Blocked; sorted by priority then due date;
  overdue cards marked. Dragging a card to another column opens a review card.
- **Interactions.** Filter by text and start date; log a new one.
- **Org.** Unit tree with leads and members.
- **Health and audit.** Graph health (`doctor`, read-only; fixes stay in the terminal),
  relationship health (cold and stale people), and the GCP audit (live, slow). They run
  as background jobs; the result is cached.

Writes:

- Allow-list (25): add/update/delete tasks (with due dates), log, correct an
  interaction (`interactions edit`, keeps the prior text), delete an interaction,
  link, unlink, update/tag/delete people, add/update/delete labs, add/delete projects,
  add/remove project documents, add/delete GCP projects, grants and assets.
  **Not allowed:** `people add` (only the Add to ledger button, 7.2), and anything
  the ledger server refuses (db-reset, init, doctor fixes, sync, bulk changes).
- Every write is two steps. Review validates the form, re-resolves every entity to a
  full UUID or exact key, builds the exact command, and returns a card in plain
  language with a single-use token bound to that command (5 minutes). Commit takes only
  the token; nothing sent at commit can change the command.
- Destructive writes (every delete, unlink, document removal) need a second, separate
  confirmation: review returns `confirm_1`, a separate click exchanges it for
  `confirm_2`, and the final button arms after 2 s. Each token is single use.
- After every write the record is read back and the card says what the ledger holds
  (fields match, interaction id and links, record gone). Never retried. Journaled.
- Text goes on stdin, never the command line. Text with `[` is checked so the ledger's
  own printing cannot fail after a write.

## 8. Adapters

### 8.1 Mail (Gmail API)

Reads

- List inbox threads (paged) and the Sent folder for the last N days (default 14) with
  `format=metadata`; fetch `format=full` on open. Poll only while the app is open,
  every 2 minutes (configurable), plus a Refresh button. Use `history.list` from the
  last `historyId` when available to fetch only changes.
- Body: prefer `text/plain`; else convert HTML to text for the default view.
- Header dates are converted to the configured time zone for display and for logging.
- Search: a search box that takes Gmail query syntax (`from:`, `to:`, `subject:`,
  `has:attachment`, `newer_than:`, `in:sent`, `label:`, etc.) and shows results in the
  stream as a temporary filter with a result count; searches can be saved as named
  filters.
- Threads: full conversation view for any message id or thread id.
- Drafts list: every Gmail draft (not only those Ultra made), openable in the composer.
  A draft started in Gmail and opened in Ultra is versioned from that point and goes
  through the same approval flow.
- Labels list: all labels with counts, usable as stream filters.
- Attachments: list per message; Save downloads to the attachments folder (or the
  browser); Preview for text, images and PDF in the sandbox. Nothing is fetched until
  clicked.

Writes (each needs its own scope; section 12.2)

- Archive: `threads.modify(removeLabelIds=[INBOX])`. Undo toast for 10 s; bulk archive
  (Tidy) writes an undo file of thread ids first.
- Labels: add/remove labels on a message or thread; create a new label (name checked
  against existing ones). No label delete or rename in v1.
- Drafts: create and update a Gmail draft for every composer version, so the draft is
  visible in Gmail on other devices. Reply drafts carry `threadId`, `In-Reply-To` and
  `References` so they stay in the thread.
- Send: only through section 9. Sends the approved version as a new message in the
  thread (or `drafts.send` on the synced draft after verifying it matches the approved
  hash). Then re-reads Sent to confirm the message id and thread id.
- Composer covers what astropost's `send` does: To, Cc, Bcc, Subject, body (plain text
  or a Markdown body sent as plain text plus simple HTML), attachments (file picker or
  drag a file in; also "attach from this thread" to forward an attachment), send-as
  address (any alias configured in Gmail "Send mail as", chosen per message), reply,
  reply all, and forward (original message and attachments included, editable). Files
  are part of the approval hash (name, size, SHA-256), so changing an attachment voids
  approval.
- Send a saved draft: any Gmail draft can be sent from Ultra after it passes the two
  approvals in section 9.
- *As built (v0.11).*
  - Search: a box above the stream (`/` focuses it), Gmail syntax, scope Inbox (default)
    or All mail; results replace the stream until Esc or "Back to the stream"; rows
    from outside the inbox carry an ARCHIVED badge. Saved searches (name + query) are
    local. `GET /api/mail/search?q=&scope=` (read token, cached 2 min).
  - Labels: a Labels button on email threads opens a checklist of user labels (tick to
    add, untick to remove, type to filter or create). System labels (INBOX, SPAM,
    TRASH, SENT, DRAFT, UNREAD, STARRED, IMPORTANT, CHAT, CATEGORY_*) are never offered
    and the server refuses them, so labels can't archive, delete, mark read or file as
    spam. Unknown ids and add+remove of the same label are refused. The change is read
    back from one thread, journaled (`labels_changed`), and the toast offers Undo (the
    reverse change). Create checks the name (letters, digits, space, `. _ / & ( ) + -`)
    and refuses a case-insensitive duplicate.
  - Attachments: each chip has Preview (text, CSV, PNG/JPEG/GIF, PDF only, in a
    sandboxed frame served with `sandbox` CSP and nosniff), Download (always
    `application/octet-stream`), Save (to the private `attachments/saved` folder, mode
    700/600, a safe unique name) and Attach to draft. The page addresses an attachment
    by message id + MIME partId, because Gmail issues a new attachmentId on every read;
    the server finds the part in a fresh read and fetches it with that read's id. The
    name and type always come from the message, never the request. 25 MB cap.
  - Composer files: Attach file (picker or drop on the composer), up to 10 files,
    25 MB each and in total, stored per draft in the private folder. Name, size and
    SHA-256 of each file are part of the approval hash (left out when there are no
    files, so older approvals are unaffected); adding or removing a file voids
    approval. At send the files are re-read and re-hashed; a changed file or list
    stops the send. The review screen lists the files. Slack replies can't carry files.
  - From: a picker with the verified Gmail "Send mail as" addresses (cached 1 day). A
    From outside that list is a lint error (blocks approval) and is refused again at
    send.
- No automatic CC. (astropost always CCs the operator's own address; Ultra doesn't,
  because Sent already holds the copy. Config option `mail.self_cc` if wanted.)

astropost parity (so astropost is not needed alongside Ultra):

| astropost | Ultra |
|---|---|
| `list` / `ls` | Stream (inbox) |
| `search` | Mail search box, saved filters |
| `summarize` (unread) | "Summarize unread" on the stream (AI, magenta card) and AI voice summary |
| `scan` (interactive) | Desk keyboard triage (j/k, e, r, l, b) |
| `show` | Thread view |
| `thread show` | Thread view by thread id |
| `send` (to, cc, bcc, subject, body, file, from, attach, reply-to, forward) | Composer + two approvals |
| `drafts list / create / send` | Drafts list, composer versions synced to Gmail, send after approvals |
| `labels list / create / add / remove` | Labels list, create, add, remove |
| `attachments download` | Attachment Save / Preview |
| trash (client function) | Not offered: Ultra never deletes mail |


Tidy (rule-based archive)

- Operator states the rule; defaults: archive every inbox thread except those where
  the operator sent a message today, VIP threads with no reply yet, and My Court items.
- Always shows the full list (kept with reason, archived with reason) before running.
  Writes `undo/inbox_archive_<date>_<time>.json`; "Undo last tidy" restores INBOX.

### 8.2 Calendar (Google Calendar API)

- Reads: events for today and the next 7 days on configured calendars; free/busy for
  attendee lists.
- Writes: create a block (summary, start, end, description, color, visibility,
  optional attendees none by default), edit or delete only events the app created
  (tagged with an extended private property `ultra=1`). Never edits events owned by
  others.
- After a write, re-read the event and check start/end/visibility.

### 8.3 Slack (Claude Code Slack connector)

The operator's Slack access is through Claude Code's Slack connector, not a Slack app
token. The adapter runs `claude -p` as a subprocess with tightly scoped tool lists.

Environment: the subprocess env removes `ANTHROPIC_API_KEY` (if it is set, the claude.ai
connectors are disabled and every read looks like "no messages"). Any output that
mentions disabled connectors is surfaced as "Slack unreachable", never as an empty
inbox.

Reads (read-only tools only)

- `--allowedTools`: `slack_search_public_and_private`, `slack_read_channel`,
  `slack_read_thread`, `slack_search_users`, `slack_read_user_profile`,
  `slack_search_channels` (fully qualified MCP names in config).
- `--disallowedTools`: every send/draft/schedule/react/canvas tool plus `Bash`, `Edit`,
  `Write`, `NotebookEdit`. `--permission-mode dontAsk`.
- Prompt asks for JSON only: `[{channel_id, channel_name, ts, thread_ts, from_id,
  from_name, text, permalink, needs_me}]`. Output is parsed strictly; anything that
  isn't valid JSON is an error, not "no messages".
- Refresh cadence: on app open, then every 15 minutes while open, and on the Refresh
  button. One Slack call at a time; results cached. Each call takes 20-90 s, so the
  stream shows cached Slack items with their age and a spinner while refreshing.
- Opening a Slack item fetches the full thread with `slack_read_thread` (cached).

Send (reply in a thread or DM, after section 9)

- A separate subprocess with `--allowedTools` set to only `slack_send_message` plus
  the read tools (for verification), and everything else disallowed.
- The prompt carries the approved text inside a delimited block, the target
  `channel_id` and `thread_ts`, and an instruction to send the text byte-for-byte and
  output only `{ts, permalink}`.
- Verification: a read-only call fetches the posted message; the app compares its text
  to the approved text (normalized for Slack's escaping of `&`, `<`, `>`). A mismatch
  is shown as a red warning with both texts and journaled; the app never auto-retries a
  send.
- Because a model sits in the send path, the approval dialog says so, and the send
  button is disabled if the verification read tools are unavailable.
- Future (open item S-1): a direct Slack Web API backend with a user token would
  remove the model from the send path.

### 8.4 Ledger (`nexus` CLI)

Reads, all with `--json`, through a cache with TTLs:

| Need | Command | TTL |
|---|---|---|
| Person by id | `nexus people show <id> --json` | 1 day |
| Person/entity search | `nexus search "<term>" --json` (keep type matches, score >= 0.85) | 1 day |
| Neighborhood (labs, tasks, interactions, projects) | `nexus tree <id> --json` | 30 min |
| Open tasks | `nexus tasks list --json` | 5 min |
| Recent interactions | `nexus interactions list --since <date> --limit 200 --json` | 5 min |
| One interaction + links | `nexus interactions show <id> --json` | 5 min |
| GCP project check | `nexus gcp show <project_id> --json` | 1 day |

Notes that shape the design:

- Each call takes about 6-10 s against the production database. The worker pool runs
  at most 3 ledger calls at once; the context rail fetches `people show` and `tree` in
  parallel on thread open, and prefetches for the top 10 My Court threads at startup.
- List commands for labs, assets and GCP projects cap at 100 rows, so the app never
  builds a full local index from them; it verifies candidates one at a time instead.
- `interactions list --json` returns only id, date and summary; use `--contains` and
  `interactions show` when a body match is needed.

Writes (only after a staged card is committed):

- Interaction: write the text to a temp file (mode 600, deleted after), then
  `nexus log --date "<YYYY-MM-DD HH:MM>" --link <id> [--link ...] --strict-links --yes -
  < <file>`. Text never goes on the command line, so `$`, `&` and quotes are never
  shell-expanded (subprocess is called with an argument list, no shell).
  - Parse `Logged (ID: <uuid>)` from stdout. If exit is non-zero with strict links,
    show which link failed; the record may still exist, so look it up with
    `interactions list --since <today> --contains <phrase> --json` before offering a
    retry. Never auto-retry (a retry makes a duplicate).
  - An "AI Error: invalid JSON" line means the ledger's summarizer failed but the
    record was saved; show it as a warning and offer "Set summary" (staged
    `interactions edit <id> --force --summary '<text>' --note '<why>'`).
  - Read back with `interactions show <id> --json`; compare `links` to the chips.
- Task: `nexus tasks add "<text>" --priority <P>`; recover the id by listing
  `tasks list --json` and matching the unique text prefix; then
  `nexus link <task_id> <target> --type REFERENCED_IN` per target and
  `--type ASSIGNED_TO <me>`. Task commands need the full UUID.
- Link: `nexus link <source> <target> --type <TYPE> [--role <text>]`. Unlink is staged
  and asks for confirmation (the CLI prompts, so the adapter pipes the confirmation
  only after the operator has confirmed in the UI).
- Close task: `nexus tasks update <uuid> --status DONE`.
- The desk never calls destructive ledger commands. The Ledger tab (7.11) may run
  single-record deletes and unlinks, only after two confirmations; `db-reset`, `init`,
  doctor fixes, sync and bulk commands are never available.
- **Local server (v0.12).** When `nexus serve` is running (127.0.0.1, token file
  `~/.config/nexus/serve.token`), every read and write goes through it: one warm
  process, so calls take 0.3-2 s instead of 6-10 s. Ultra's own allow-lists, review
  cards and read-backs apply unchanged. If the server is down the CLI is used. A write
  whose outcome is unknown (connection lost after sending) is never re-sent through
  the CLI; it is reported as unknown so the operator can check, because a retry could
  make a duplicate.

### 8.5 Tickets (ServiceNow notifications in email)

- Sender patterns and ticket regex come from config (defaults cover
  `RITM\d{7}`, `INC\d{7}`, `SCTASK\d{7}`, `REQ\d{7}`).
- All notices for one ticket number become one ticket card (a SCTASK created in the
  same second as a RITM is folded into it).
- State phrases are parsed into events: assigned to you, assigned to your group,
  comments added, resolved, closed, approval requested.
- A comment notice that only echoes the operator's own comment is dimmed and counted as
  noise.
- Reply to ticket = an email reply to the latest notice that keeps the original subject
  (with the ticket number) and the notice's watermark line, so the ticketing system
  attaches it to the ticket. Goes through the normal composer and double approval.
- The composer offers two templates for ticket text: "customer visible comment" and
  "internal work note" (the latter is copy-to-clipboard only, since email replies post
  as comments).

### 8.6 AI (Gemini, on demand)

- Uses `GEMINI_API_KEY` from `~/.config/ultra-workstation/.env`. Model and fallback are
  config values. No call is made unless a button is pressed.
- Functions:
  - Ask summary: what the latest message asks of the operator, deadlines, who is
    waiting on whom, in 3-6 lines.
  - Draft: reply draft from thread + context rail + operator instruction + style rules.
  - Revise: apply an instruction to the current version ("shorter", "remove the second
    paragraph", "ISO classifies this, not me").
  - Log text: rewrite a bucket's template into log style.
  - Link suggestions: only when rules resolved nothing, shown dashed.
- Every call sends the minimum context (the thread, not the mailbox). Prompts and
  responses are not written to logs. Token counts per call are shown in the status bar.
- Failure (quota, timeout) shows an error on the card; manual editing always works.

## 9. Composer and double approval

The operator iterates on drafts many times, then approves twice. The server enforces
this; the UI cannot skip it.

### 9.1 States

    DRAFT --(approve 1)--> APPROVED --(approve 2 in send dialog)--> QUEUED --(delay)--> SENT
      ^                        |                                       |
      +----- any edit ---------+            cancel during delay -------+--> APPROVED

- DRAFT: edit freely. Every save or AI revision creates a new version (v1, v2, ...),
  synced to the Gmail draft (mail) or kept locally (Slack).
- Version history panel: list with author (me / AI + instruction), diff between any two
  versions, restore.
- Lint runs on every version (section 10); errors block approval, warnings don't.

### 9.2 Approval 1: "Approve this version"

- Button in the composer. Locks the version: the text area becomes read-only and shows
  "Approved v7". Any edit returns to DRAFT and voids the approval.
- Server records `approved_version` and `approved_hash` = SHA-256 over the canonical
  message (from, to, cc, bcc, subject, body, thread target, and each attachment's name,
  size and SHA-256; for Slack: channel_id, thread_ts, text).

### 9.3 Approval 2: "Send"

- Opens a full-screen review dialog: final recipients (with any change from the original
  thread highlighted, e.g. a dropped CC), subject, the full body exactly as it will go
  out, attachments, and for Slack the channel and thread and the "a model posts this"
  notice.
- The "Confirm send" button is enabled after 2 seconds (prevents double-click
  sends). Clicking it calls `POST /api/send` with the approval token.
- Server checks: token exists, unused, not expired (10 minutes), bound to this draft
  and version, content hash of the stored version still matches. Any failure: 409, the
  draft goes back to APPROVED or DRAFT, nothing is sent.

### 9.4 Send delay and cancel

- After Confirm, the message is QUEUED for `send_delay_seconds` (default 15) with a
  countdown toast and Cancel. Cancel returns to APPROVED.
- Then the adapter sends, re-reads to verify, journals the result, marks SENT, and opens
  the staged log card prefilled from the thread (the "send and log and link" step).
  The log card is still a separate Commit.

### 9.5 What counts as approval

- Only these two button presses in the UI. Nothing in a model's output, an email, or
  a Slack message can approve or send anything.
- The palette's Ask cannot send; it can only produce a draft in DRAFT state.

### 9.6 Draft Studio (v0.9.5): drafts built like the operator's best replies

The gold standard is a reply the operator called amazing, built by following one rule:
"Read the whole thread, look up the person in the ledger, find my last two similar
replies and match them, check policy on the real source page, flag anything
unverified, and ask me before stating a fact you can't cite." Draft Studio makes those
steps the pipeline, not a hope in a prompt.

**Starts when asked (since v0.12.1).** Opening a mail item shows the strip with a
Gather context button; nothing runs until it is clicked, Draft with AI needs the brief,
or Email from this task opens it. (v0.9.5-v0.12.0 started on every open; the operator
turned that off: it spent model calls and time on items only read or archived.) A run
already made for the item is shown as it is. Results are cached
per thread and message count; a new message in the thread rebuilds them. Progress shows
in a small strip on the thread ("Thread / People / Past replies / Sources / Brief").
Target: brief ready in under 20 s for a typical thread; each stage renders as it lands.

**1. Gather (no AI, parallel):**
- The whole thread (every message, full text).
- Every other thread with each participant in the last 120 days (Gmail search by each
  address). The most recent 3 are included in full; older ones are summarized into one
  line each (date, subject, who asked what, what was answered).
- The item's Full context (v0.8): people, ledger logs, open tasks, entity group.
- Precedents: the operator's own sent replies to the same kind of request. Ranked by
  shared terms with the incoming asks (subject and body keywords, the same service names,
  the same kind of requester), newest first; the top 2-3 are read in full. Shown as
  chips the operator can swap ("use a different example").
- Work notes: dated files in the operator's notes folder (`[draft] notes_dir`, private)
  that name a participant.
- Policy sources (below): pages relevant to the asks, fetched and cached.

**2. Policy sources (private list, `[draft.sources]`):**
The operator lists the sites whose pages count as policy (their IT office, campus and
system-wide policy libraries, their help desk's public knowledge base, their own KB
site). The real list, and the notes on which hosts answer, live in the private config
and `local/`, not in this repo. Supported source kinds:
- `site`: a public site; pages found through its sitemap.xml and fetched directly.
- `page`: one fixed URL (an AI guidance page, a rate card).
- `servicenow_kb`: a ServiceNow portal's public articles. Articles are found by web
  search restricted to the portal host, then read without login through the portal
  page API (`/api/now/sp/page?id=kb_article&sys_id=<id>&portal_id=<portal>`, which
  returns number, title, author and full text). Guest search through that API returns
  nothing, so search goes through the web search tool. Authenticated KB search is out
  of scope.
Fetching: plain HTTPS GET with a normal user agent, 20 s timeout, text extracted
(scripts, nav and footers dropped), cached 24 h in the state DB with the fetch time.
The page's relevant passages (keyword windows around the asks) go to the model with
the URL; the draft cites the URL. A source that fails to load is shown as failed, never
silently skipped. A host that does not answer is reported in `ultra doctor`.

**3. House facts (private, `house_facts` table):**
Settled answers the operator has given, for example "credits means our central cloud
program", "provider X is not centrally covered; own funds only", "do not mention the
pilot service", "no dollar figures unless asked". Each has text, topic words, source ("operator,
2026-09-30"), and an on/off switch. Relevant ones (by topic words) go into every brief
and draft. New ones come only from the operator: answering a question in step 5 offers
"Save as a house fact" (default on, editable). Never learned silently from mail.

**4. Brief (AI, gemini-3.8-flash, with the real date and time):**
Returned as JSON and shown for correction before any drafting:
- asks: the numbered questions or requests, in the sender's words
- constraints: facts that shape the answer ("public datasets only", "grant pending")
- audience: student / faculty / staff / peer / leadership / vendor (sets depth, per the
  operator's rules), and the tone of the operator's precedents with this person
- known: each fact with its source (message, ledger log, policy URL, house fact)
- unknown: what the answer needs but no source gives
- risks: at most two worth flagging unprompted (a DUA, a stale draft, a promise in the
  thread, a dropped Cc)
- plan: one line per ask saying how the reply answers it

**5. Questions first:**
Every "unknown" becomes a question card with answer buttons where possible (Yes / No /
Other...) and a free-text box. The draft waits for answers or for "Draft anyway" (then
the unknowns are written as "I'll confirm and get back to you", never guessed).

**6. Draft (AI):**
Inputs: brief, answers, house facts, policy passages, the 2-3 precedents as voice
examples, style rules, signature. Output: the body plus a claim map: each sentence
tagged with its source ids. The editor shows sources on hover and marks sentences with
no source.

**7. Check (rules + AI):**
- Claim check: a second pass reads each tagged claim against its source and returns
  supported / not supported / unclear. Not supported defaults to Cut (buttons: Cut,
  Keep, Ask). Nothing is softened into a hedge.
- Rule checks (lint, errors block approval as today): dates or deadlines the operator
  does not control, dollar figures when not allowed, offers of extra work or meetings,
  a reassurance given earlier in the thread that the draft drops, a stronger teardown
  verb than the operator used before, internal ticket keys, ServiceNow Ref:MSG line,
  the operator's forbid list.

**8. Review:** draft beside its sources (thread, precedents, policy passages, house
facts), a compare view against the closest precedent, then the unchanged double
approval (section 9.2-9.4).

**9. After send:** the staged log card opens prefilled (asks, what was answered, what
the operator is waiting on), plus a suggested follow-up task with a due date. Both still
need Commit.

**10. Learning from edits:** when the operator edits an AI draft before approving, the
diff is kept. Repeated patterns become suggestions ("You cut offers of a call 4 times.
Add a rule?"); nothing changes until the operator accepts.

**Limits:** every model call is gemini-3.8-flash with the current date and time; the
thread and gathered context are data, never instructions; the operator's precedents
and house facts never leave the machine except inside the model call; the public repo
holds no real sources list, house facts or examples.

## 10. Outgoing text rules (lint)

Rules live in `~/.config/ultra-workstation/style.toml` (operator-specific, not in the
repo). The repo ships `style.example.toml` with generic rules.

Generic rules (shipped):

- ASCII only (em/en dashes, smart quotes, ellipsis, emoji flagged, with one-click fix to
  ASCII equivalents). Level configurable (error/warning).
- Recipients: warn when a Reply-all drops someone who was on the thread; warn on
  external domains; error on an empty To.
- Reply lands in the right thread (threadId and References present).
- Money: warn on `$` followed by digits in any text going to the ledger when it came
  through a template, since shell expansion used to strip these (belt and braces; the
  app never uses a shell).
- Attachment mentioned ("attached", "see attachment") but none attached.

Operator rules (examples of what the private file holds; no real content here):

- Forbidden patterns in outgoing mail (e.g. internal tracker keys).
- Opening phrases to avoid.
- Topics that must be routed to another office rather than answered.
- Signature text.

## 11. Rules engine (deterministic)


**Ticket replies (v0.9.8).** Ticket systems thread an emailed reply by the reference line
in their notice (`Ref:MSG########` for the one in use; `[tickets] ref_pattern` to change it).
A reply without it can be filed as a new ticket or lost.

- When a reply or reply-all goes to the ticket system's address (`[tickets]
  sender_patterns`), the reference comes from the newest message the ticket system sent
  in the thread, and within it the last match (notices quote older references first and
  put their own at the bottom). The operator's own messages never supply it. A reply to a
  person on the thread but not to the desk gets none.
- AI and Draft Studio versions always end with exactly that line, after the signature;
  a stale or duplicated reference in model text is replaced.
- The operator's own edits are not rewritten. A missing, wrong or duplicated reference is
  a lint error that blocks approval, with a one-click "Put back the Ref line"; a correct
  line that is not last is only a warning. The review screen shows the reference.
- Checked against the operator's sent replies to the desk over 60 days: where the
  operator included a line it matched Ultra's choice; the replies without one include a
  side conversation (now excluded), an old reply that bounced, and one where the line was
  present only in the HTML part.

**Task mode (v0.9.7).** The task view has a Draft email button that runs the same pipeline
for an email that moves a ledger task forward (key `t-<uuid>`):

- *Gather.* The "thread" is the task: summary, status, priority, due date, details and
  links, with internal ticket keys (private `[ai] hide_ticket_prefix`) removed before any
  model sees them. Related mail comes from Gmail queries on ticket numbers named in the
  task (strongest), all of its top key words, any of them, and linked people's names;
  task-verb filler ("follow up", "confirm", "note") never drives the search. Newsletters
  and notification senders are dropped, and a thread found only by the any-word query
  needs three shared key words. The people on the best related threads become the
  participants for precedents and notes; notes also match on three shared task words.
- *Recipients.* An allow-list of every address seen in the related mail or the ledger
  context (never the operator, never bulk senders). The brief's To/Cc must come from it;
  anything else the model returns is dropped. The operator may type another address in
  the envelope; it is shown as "typed by you, not seen in the material".
- *Brief.* Adds where the task stands, the email's purpose, To, Cc, subject, and whether
  to reply-all on a related thread (must be one of the gathered threads) or start a new
  email.
- *Draft and check.* The writer is told it moves task [T] forward, whether it continues a
  thread or starts one, and never to mention the task list, the ledger or internal keys;
  the body is scrubbed of them again. The subject is never the raw task text: a reply
  uses the thread's subject, a new email uses the brief's or is left for the operator.
- *Standing rules* (both modes): sentences offering calls or meetings nobody asked for,
  and apologetic openers, are flagged "unsupported" and cut by default whatever the
  checker thought.
- *Hand-off.* `POST /api/drafts/from-task` makes (or reuses) the composer draft for the
  task: reply-all on the chosen thread (recipients and subject from the thread) or a new
  email (To required, every address validated, subject required). The body is saved as an
  AI version and still needs both approvals. `GET /api/drafts/task/<uuid>` resumes it
  when the task is reopened; after the send the task's progress-log card opens.


### 9.7 One drafting path, checked versions, before/after, Tidy (v0.9.9)

- *Composer AI goes through Draft Studio.* For replies and task emails, "Draft with AI"
  uses the Studio brief and gathered sources, and "Revise with AI" applies the change
  with the same sources, house facts and rules (no new facts without a source; keep the
  Ref line). New blank emails, forwards and Slack keep the plain path. The result is an
  AI version as before.
- *Every AI version is checked.* The source check runs after each AI draft or revise
  and is stored with that version (only verdicts for sentences still in the text). The
  composer shows it with the flagged sentences ticked for cutting; "Cut the ticked
  sentences" makes a new version by the operator. A hand edit makes a new, unchecked
  version ("Check sources" re-runs it). Draft Studio's hand-off keeps its check and the
  operator's Cut/Keep choices.
- *Before/after.* Word-level comparison between any two versions (whitespace changes are
  not counted), header fields included; opened automatically after an AI revise, a cut or
  a Tidy. "Since the AI" compares the last AI version with the current text. The review
  screen (approval 2) shows the source check of the exact version being sent and, when
  the operator edited after the AI, those changes.
- *Tidy* is deterministic and form-only: typographic characters to ASCII, trailing and
  double spaces, extra blank lines, a repeated greeting or signature, and the Ref line
  placed once, last. It never adds or removes a content word; the version's source check
  carries over.
- *Reply-all recipients fix.* On Python 3.13, `email.utils.getaddresses` given a list
  with two blank fields returns nothing; header fields are now parsed one at a time
  (`compose.addresses`). This removed a false "not included from the thread" warning on
  review and could have dropped To recipients from a reply-all Cc when the notice had
  no Cc. Checked: no sent reply-all was affected (the two live reply-all drafts kept
  their recipients).

### 9.8 Learning from edits (v0.9.9)

After a send, the last AI version is compared with the sent text; removed sentences
(three or more words) and short replaced phrases are recorded locally. When the same
edit repeats in three different emails (shared 4-6 word phrases count as the same
habit), the composer offers "Make it a style rule" or "No, leave it". Accepting appends a
`[[forbid]]` warning to the private style.toml (backed up first, and only if the result
parses); the AI is told to avoid it and the composer warns when it appears. Dismissed or
accepted suggestions do not return. No model is involved.

### 11.1 Person resolution

In order, stopping at the first confident hit:

1. Cache hit in `people` (unless older than 1 day).
2. Address in the configured org domain and `netid_from_local_part = true`: take the
   local part as the id and confirm with `nexus people show <id> --json`.
3. Address or alias match against `email_alias` in cached people records.
4. `nexus search "<display name>" --json`, keep type = Researcher with score >= 0.85,
   exactly one hit.
5. Otherwise unresolved. Show "Not in ledger". Never guess.

Slack users resolve through their profile email (from the read call) and then the same
chain.

### 11.2 Link chips for a bucket

- Every resolved person in the bucket (senders and recipients who resolve, minus noise).
- The operator (configured ledger id).
- Labs: from each person's `tree` where the edge is MEMBER_OF or PI_OF to a Lab.
- Projects: GCP project ids found by regex in the text and confirmed with
  `nexus gcp show`; projects on the lab's OPERATES edges (one hop, lazy).
- Tickets: numbers in the text are put in the log text; a ledger node is linked only if
  one exists (found by search).
- Chips are ranked; people and explicit matches are checked by default, graph-derived
  ones (lab, project) are checked too but marked "derived".

### 11.3 Court state (per thread)

- MINE: last message not from the operator, thread in inbox, sender not noise; or a
  done-signal (below) from a peer after the operator's last message; or a ticket
  assigned to the operator with no reply since.
- WAITING: last message from the operator. Shows days since; turns amber after
  `waiting_amber_days` (default 3), red after `waiting_red_days` (default 5).
- WATCHING: operator-set flag, optionally with a date.
- DONE: archived today, or moved to Done.
- Done-signal: a message from a peer matching configurable patterns such as
  "is ready", "is done", "completed", "all set", "finished", "deployed",
  "has been resolved", "you can now". It forces MINE with a READY badge, and the
  context rail highlights any open task whose text suggests waiting on that person
  ("waiting on", "chase", "follow up with") as possibly stale.
- The same rules run over Slack conversations (operator's Slack id from config).

### 11.4 Replied badge

YES if Sent has a message to any of the person's addresses in the thread after their
last message, or a ledger interaction linking that person is dated after it. Otherwise
NO with days waiting. The badge tooltip says which source answered.

### 11.5 Noise

Sender patterns (config) plus Gmail categories (promotions, social, forums) go to the
Low priority row. VIP list overrides noise.

## 12. Security and privacy

### 12.1 Network

- Binds to 127.0.0.1 only. Rejects requests whose peer is not loopback, whose Host is
  not `127.0.0.1:<port>` or `localhost:<port>`, or whose Origin (when present) differs.
- Every state-changing request needs a CSRF token (random per server start, delivered
  in the page, sent as a header). GET requests never change state.
- Content-Security-Policy: `default-src 'self'; script-src 'self'; frame-src 'self';
  img-src 'self' data:; media-src 'self'; connect-src 'self'; object-src 'none';
  base-uri 'none'`.

### 12.2 Credentials

- Google OAuth: `ultra auth google` runs the installed-app flow with the operator's own
  OAuth client file and stores one token per capability under
  `~/.config/ultra-workstation/tokens/`: `read` (gmail.readonly), `modify`
  (gmail.modify, for archive/labels/drafts), `send` (gmail.send), `calendar`
  (calendar). Alternatively, config can point each capability at an existing token file
  the operator already has; the app then never writes to that file except to refresh.
- Files are created mode 600 in a mode 700 directory; `ultra doctor` fails if they are
  group- or world-readable.
- The Gemini key is read from `~/.config/ultra-workstation/.env` (mode 600). It is
  never written to the store, logs, journal, or the page.
- Claude Code handles its own Slack authorization; the app stores nothing for Slack.

### 12.3 Data at rest

- `state.db`, `journal.jsonl`, `undo/`, `audio/` and `attachments/` hold mail
  metadata, draft text, audio of mail and saved files; mode 600 files in mode 700
  folders.
- `ultra.log` holds actions and errors only: no message bodies, no draft text, no
  tokens. Subjects are truncated to 40 characters in logs. A redaction filter masks
  anything that looks like a key or token.
- `ultra purge --cache` clears cached mail and Slack content; `--audio` deletes audio
  files; `--attachments` deletes saved attachments; drafts and journal stay unless
  `--all`.

### 12.4 Rendering untrusted content

- Email HTML is sanitized with DOMPurify and rendered in an iframe with
  `sandbox=""` (no scripts, no same-origin, no forms, no top navigation) via `srcdoc`.
- Remote images are blocked by default (tracking pixels). "Load images for this
  message" is a per-message button.
- Links open in a new tab with `rel="noopener noreferrer"`.
- Slack and ledger text is inserted as text, never as HTML. Markdown (AI summaries) is
  rendered with marked + DOMPurify.
- Email and Slack content passed to a model is wrapped as quoted data with an
  instruction that it is not instructions. Model output can only fill cards; it has no
  path to an action (section 9.5).

### 12.5 Public repo hygiene

- `.gitignore` covers `local/`, `.env*` (except `.env.example`), token and credential
  JSON, key files, SQLite files, logs, captures and private fixtures.
- `local/` holds the operator's private notes and pattern list; never committed.
- CI runs `gitleaks` on every push and PR.
- `scripts/check-private.sh` greps tracked files for patterns listed in
  `local/private_patterns.txt` (real names, domains, ids) and fails if any match. It
  runs as part of the local gauntlet and as a pre-commit hook; CI can't run it (the
  pattern file is private), which is why gitleaks also runs there.
- Tests use synthetic fixtures only (`example.org`, invented people, invented ticket
  numbers in a format the regex accepts).
- Screenshots in docs use a demo mode (`ultra start --demo`) that serves synthetic
  data, never real mail.

## 13. Configuration (`config.toml`)

Shipped as `config.example.toml` with placeholders; the real file lives only in
`~/.config/ultra-workstation/`.

    [operator]
    name = "Ada Example"
    addresses = ["ada@example.org", "a.example@example.org"]   # all "from me" addresses
    timezone = "America/New_York"
    ledger_id = "adaex"                  # operator's id in the ledger
    slack_user_id = "U00000000"

    [server]
    port = 7440
    poll_mail_seconds = 120
    poll_slack_minutes = 15

    [google]
    oauth_client = "~/.config/ultra-workstation/oauth_client.json"
    token_read = "~/.config/ultra-workstation/tokens/read.json"
    token_modify = "~/.config/ultra-workstation/tokens/modify.json"
    token_send = "~/.config/ultra-workstation/tokens/send.json"
    token_calendar = "~/.config/ultra-workstation/tokens/calendar.json"
    calendars = ["primary"]
    work_block_color = "tomato"
    personal_block_visibility = "private"

    [ledger]
    binary = "nexus"
    org_email_domain = "example.org"
    netid_from_local_part = true
    max_parallel = 3

    [slack]
    enabled = true
    claude_binary = "claude"
    claude_cwd = "~"                      # directory whose Claude Code config has the connector
    tool_prefix = "mcp__claude_ai_Slack__"

    [tickets]
    sender_patterns = ["@service-now\\.com$"]
    number_patterns = ["RITM\\d{7}", "INC\\d{7}", "SCTASK\\d{7}", "REQ\\d{7}"]

    [mail]
    vip_file = "~/.config/ultra-workstation/vip.txt"
    noise_patterns = ["noreply", "no-reply", "newsletter", "notification"]
    send_delay_seconds = 15
    sent_lookback_days = 14

    [court]
    waiting_amber_days = 3
    waiting_red_days = 5
    done_signal_patterns = ["\\bis ready\\b", "\\bcompleted\\b", "\\ball set\\b", "\\bdone\\b"]

    [ai]
    provider = "gemini"                   # or "none"
    model = "<model id>"
    fallback_model = "<model id>"

    [audio]
    tts_model = "<gemini tts model id>"
    voices = ["Charon", "Kore", "Puck", "Aoede", "Fenrir", "Leda", "Orus", "Zephyr"]
    default_voice = "Charon"
    price_in_per_1m = 0.0                 # set from current pricing, used for estimates
    price_out_per_1m = 0.0
    chunk_chars = 3500

    [research]
    enabled = true
    binary = "deep-research"
    dashboard_url = ""                    # optional link, e.g. http://127.0.0.1:<port>
    default_depth = 1
    default_breadth = 3

## 14. HTTP API (server)

All JSON. Writes need `X-CSRF-Token`. Long calls return `202 {job_id}`; poll
`GET /api/jobs/<id>` (or the page listens on a single SSE stream `/api/events`).

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/health` | versions, adapter status |
| GET | `/api/stream?filter=mine\|waiting\|all\|slack\|tickets&group=thread\|person` | stream rows |
| GET | `/api/thread/<source>/<key>` | full thread |
| GET | `/api/context/<person_key>` | context rail (cached + refresh job) |
| POST | `/api/mail/archive` | archive thread(s); returns undo token |
| POST | `/api/mail/unarchive` | undo |
| POST | `/api/mail/tidy/preview` / `/api/mail/tidy/run` | rule-based archive |
| POST | `/api/drafts` | new draft (reply/reply-all/forward/new/slack) |
| POST | `/api/drafts/<id>/versions` | save manual edit |
| POST | `/api/drafts/<id>/revise` | AI revision with instruction |
| POST | `/api/drafts/<id>/approve` | approval 1; returns approval token |
| POST | `/api/send` | approval 2; token + draft id + version |
| POST | `/api/send/<id>/cancel` | cancel during delay |
| GET/POST/DELETE | `/api/bucket` | bucket contents |
| POST | `/api/ledger/stage` | build staged log/task/link card from bucket |
| POST | `/api/ledger/commit/<staged_id>` | run the CLI write + read-back |
| GET | `/api/calendar?day=` | events |
| POST | `/api/calendar/freebusy` | slots |
| POST | `/api/calendar/blocks` | create block (staged -> commit in one call from the card) |
| POST | `/api/ai/summary` / `/api/ai/draft` | on-demand AI |
| GET | `/api/journal?since=` | write journal |
| GET | `/api/events` | SSE: job progress, new items, sync status |
| GET | `/api/mail/search?q=` | Gmail query search |
| GET | `/api/mail/drafts` | all Gmail drafts |
| GET/POST | `/api/mail/labels` | list / create labels |
| POST | `/api/mail/labels/apply` | add or remove labels on messages or threads |
| GET | `/api/mail/attachment/<msg>/<att>` | preview or download one attachment |
| POST | `/api/mail/attachment/<msg>/<att>/save` | save to the attachments folder |
| POST | `/api/ledger/search` | Nexus search for a selection |
| POST | `/api/research/search` | deep-research semantic search |
| GET | `/api/research/runs` / `/api/research/runs/<id>` | list runs / one report |
| POST | `/api/research/estimate` / `/api/research/start` | estimate / start a run |
| POST | `/api/ai/explain` | explain a selection |
| POST | `/api/ai/websearch` | grounded Google Search answer with sources |
| POST | `/api/audio/estimate` / `/api/audio` | estimate / create audio (job) |
| GET | `/api/audio/<id>` | stream audio (Range) |

## 15. CLI

    ultra start [--port N] [--demo]     start the server detached
    ultra stop | restart | status
    ultra open                          open the browser at the running server
    ultra doctor                        check config, token perms, scopes, nexus, claude,
                                        Slack connector (and ANTHROPIC_API_KEY), Gemini key,
                                        deep-research, ffmpeg
    ultra auth google [--capability read|modify|send|calendar]
    ultra config init                   write config.example to ~/.config (never overwrites)
    ultra purge [--cache|--audio|--attachments|--all]
    ultra journal [--since DATE] [--json]
    ultra --version

## 16. Error handling and status

- Status bar shows each source: Mail, Calendar, Slack, Ledger, AI, with last success
  time. A failing source is amber with the error on hover; the rest keep working.
- Token expired or revoked: banner with the exact `ultra auth google --capability X`
  command.
- Ledger CLI missing or failing: context rail and bucket show "Ledger unavailable";
  staged cards can still be saved locally and committed later.
- Slack connector disabled: "Slack unreachable" with the reason, never "no messages".
- No silent retries on writes. Reads retry with backoff (3 tries).

## 17. Performance targets

| Action | Target |
|---|---|
| App shell loads | < 1 s |
| Stream from cache | < 300 ms |
| Open email thread | < 1 s (Gmail API) |
| Context rail, cached | instant; fresh within 10-20 s |
| Archive with UI update | < 500 ms (optimistic, rolled back on error) |
| Ledger log commit + read-back | 15-30 s (CLI-bound), progress shown |
| Slack refresh | 20-90 s in background |
| AI draft | 5-20 s |

## 18. Testing

- Unit tests with fakes for every adapter (no network in CI).
- Contract tests: recorded synthetic JSON shaped like each CLI's real output (`nexus
  search/people show/tree/tasks list/interactions show`) and Gmail/Calendar API
  responses; parser tests against them.
- Approval tests: every path around section 9 (edit after approve, expired token, reused
  token, hash mismatch, cancel during delay, double click) must refuse to send.
- Security tests: loopback/Host/Origin/CSRF rejections, CSP header present, sandboxed
  iframe attributes, no inline scripts in static files.
- Private-data tests: `check-private.sh` in the local gauntlet; gitleaks in CI.
- Browser smoke test (Playwright, optional, local only) against `--demo`.
- Local gauntlet before any push: `uv run ruff check . --fix`, `uv run ruff format .`,
  `uv run mypy src`, `uv run pytest`, `scripts/check-private.sh`. CI repeats the first
  four plus `ruff format --check`, `uv lock --check` and gitleaks.
- Versioning: bump `pyproject.toml` on every feature branch and run `uv lock`; tag after
  merge; install with a pinned script (uv.lock exported as constraints), not bare
  `uv tool install`.

## 19. Delivery plan

| Version | Scope |
|---|---|
| v0.1 Skeleton | Repo, CLI (start/stop/status/doctor/auth), server with security guard, theme and layout shell (7.0), frontend helpers, demo mode, config loading, gauntlet + CI + gitleaks + check-private |
| v0.2 Read | Mail stream and threads, Sent, ticket cards, noise row, court rules, context rail via ledger reads, Slack read (cached), status bar, Copy + Export (Markdown, text, HTML, JSON, .eml, Print/PDF) on threads, selection bar with highlights and notes |
| v0.3 Write (shipped 0.3.0) | Composer with versions + restore, lint (ASCII, forbidden patterns, reply-all drops, external, missing attachment), Gmail draft sync, AI summary/draft/revise (Gemini, on demand), double approval with single-use expiring token and content hash, 15 s send delay + cancel, send verified in Sent, archive + undo, forward, Ctrl-K palette, keyboard r/a/f/s/e/c |
| v0.35 Write, part 2 | Version diff view, Tidy, astropost parity (search, all-drafts list, labels create/apply, attachments save/preview/attach, send-as picker) |
| v0.4 Ledger (shipped 0.4.0) | Context rail tabs (People on the whole conversation, Person, Ledger search); bucket (conversations, people, labs, projects, snippets) with drag and drop; staged Log and Task cards with pre-resolved chips (every resolved participant, labs via graph, GCP ids and ticket numbers in the text), deterministic template text, Rewrite with AI; commit through the CLI with UUID-only args, text on stdin, one commit per card, progress steps, read-back, Link now on missing chips; mark task Done; send-then-log; selection bar (Search ledger, Add to bucket, Copy); inbox-only stream by default |
| v0.45 Listen + Research (shipped 0.5.0) | Read aloud (browser voice, block highlight, rate/voice, prev/next); AI audio (spoken summary or full read, Gemini TTS, cached by text+mode+voice, MP3 via ffmpeg, mode 600, served with HTTP Range, player with speed/download/script, `ultra purge --audio`); Research tab (web search with Google grounding and sources, past-research search, recent runs, report viewer, launcher with estimate, confirm flag, opt-in thread upload); selection bar Copy / Quote in reply / Search ledger / Web search / Add to bucket / More: Explain, Search research, Research this, Read aloud. Highlights with notes and export menus move to v0.46 |
| v0.5 Slack reply (shipped 0.7.0) | Reply on a Slack conversation opens a Slack composer (target channel and thread fixed from the cached stream row, part of the approval hash); versions, AI draft/revise in chat style (no signature), Fix ASCII, Slack lint (4000 chars, @channel warning); approval 1, review dialog with the model-in-path notice and a 2 s disabled Post button, approval 2 with a single-use token; delay and cancel; send run allowed only slack_send_message, verify run allowed only read tools; the connector's "Sent using Claude" line is ignored when comparing; mismatch or unclear result is shown, never retried; send-then-log card for the conversation |
| v0.6 Calendar (shipped 0.6.0) | Today view (day timeline, prev/next day, now line, work hours band, overlapping events in columns, all-day row, response and privacy marks); drag a stream item onto a time to stage a block (title Focus: subject, notes from the last message, open draft and link, 15-120 min, personal = private); Block time buttons on threads, tasks and the bucket open a block card at the next free slot; move and delete only Ultra-tagged blocks (server-enforced), re-read after every write; meeting prep (invite notes, attendees with responses, click one for ledger context); Find a time (free/busy over work hours, others you cannot see are listed, Copy as text, Hold); `g` toggles Today. Check-in and end-of-day move to v0.8 |
| v0.7 Board (later, operator's call) | Court board, nudge-all, drag between columns |
| v0.8 Day (shipped 0.9.0) | Day view (`d` key, top-bar Day): check-in plan (meetings minus declined, free windows in work hours from now, your-move items READY/VIP first, waiting 3+ days, overdue / due today / high-priority tasks, suggested focus blocks fitted into free windows that open the normal block card), AI read of the day (draft only); end-of-day report from the journal (sent, Slack posted, archived with subjects, logged, task changes, calendar block writes, research, audio; repeats collapsed; still open), editable, Copy / Listen / Save to ledger via a staged log card; exports agenda .ics (no attendee emails) and journal .csv. AI brief builder and Board export move with the Board |
| v0.9.5 Draft Studio (shipped 0.9.5) | Section 9.6: gather on open (thread, 120-day history per participant, Full context, 2-3 precedents from Sent, work notes, policy pages), private policy sources with 24 h cache (site sitemaps, fixed pages, public ServiceNow KB via the portal page API), house facts, brief with asks / constraints / audience / known / unknown / risks, question cards before drafting, draft with claim map, claim check (cut by default) plus rule checks, source-side review, send-then-log with follow-up task, suggestions from edits |
| v0.10 Calendar (shipped 0.10.0) | Week view with "Needs your answer"; RSVP with one confirm and a bound single-use token; meetings with guests through two approvals (hash-locked invite, review with busy/unknown/external guests, 10-minute single-use token, read-back); meeting prep from item context (`c-` item keys: People, Full, cited briefing); log a meeting through the staged card; weekly repeating Ultra blocks (RRULE with UNTIL, 26-week cap) and Delete series; sent meetings are never movable |
| v0.11 Email, rest (shipped 0.11.0) | Mail search with saved searches, labels (user labels only, read-back, Undo), attachments (preview/download/save/attach by partId), composer files in the approval hash with re-hash at send, send-as picker checked at lint and send, highlights and notes, thread export (Markdown/text/JSON/Print). The all-drafts list was dropped by the operator. ServiceNow replies, version diff and Tidy shipped earlier (0.9.8, 0.9.9) |
| v0.12 Ledger tab (shipped 0.12.0) | Full dashboard for the ledger tool: home (counts, activity, tasks, overdue, going-cold people), browse/search every entity type, entity pages with links/history/tasks/cited briefing, task board, interactions (edit via `interactions edit`, link/unlink), org tree, GCP audit and cost reports, graph health (`doctor`); writes only through allow-listed commands with review cards, read-back, double confirmation for destructive or bulk changes; ledger additions each on their own reviewed PR |
| v1.0 | Polish, keyboard help, docs, public release |
| v1.x | Graph view, palette Ask, Slack Web API backend (S-1) |

## 20. Open items

| Id | Item | Notes |
|---|---|---|
| Q1 | Final name and CLI command | Working: "Ultra AI Workstation Desktop", repo `ultra-workstation`, command `ultra`. |
| Q2 | Default port | 7440 proposed (the operator's other dashboard uses 7420). |
| Q3 | Reuse existing Google tokens or mint app-specific ones | Config supports both; app-specific is cleaner for a public tool. |
| S-1 | Slack send has a model in the path | Mitigated by exact-text prompt, send-only tool list and verification read. A user-token Web API backend would remove it. |
| S-2 | Slack read cadence and cost | Each refresh is a `claude -p` run; 15 min while open is the proposal. |
| L-1 | Ledger reads are slow (6-10 s per call) | RESOLVED without ledger changes (v0.7.1): the Full context tab combines `dossier --json`, `search --json` and `tasks list --json` in parallel. Remaining ledger-side gaps, optional: dossier omits tasks linked to the person (it fetches them but only emits them under connections when present), and `search` has no type filter. |
| L-2 | ~~No due-date flag on `tasks add`~~ Done: ledger 0.1.206 (`tasks add --due`, `tasks update --due/--clear-due`, `tasks list --overdue/--due-before`); Ultra 0.9.1 uses them. | - |
| L-3 | ~~Task id not printed reliably by `tasks add`~~ Done: `tasks add --json` (ledger 0.1.206). Also 0.1.207: `tasks update/delete` by UUID never fuzzy-match another task and exit 1 when nothing matches; `people add` refuses NetIDs starting with '-'. | - |
| T-1 | Ticket watermark format | DECIDED from the operator's sent mail: reply-all to the "comments added" notice, To the ticket desk, requester in Cc, subject kept (Re:), and the notice's `Ref:MSG########` line kept unquoted at the end of the body. Built in v0.10. |
| A-1 | AI model choice | Config value; default set at build time. |
| U-1 | Hermes / agent hand-off from the palette | Later; would pass a prompt to an external agent command and show its output as staged cards. Not designed yet. |
| R-1 | deep-research `--json` | DONE: deep-research v0.36.0 adds `--json` to every command (PR #142). |
| R-2 | Research context from mail | DECIDED: allowed when the operator ticks "Include this thread" for that run; unticked by default. |

## 21. Change log

| Date | Version | Change |
|---|---|---|
| 2026-10-01 | 0.30 | App v0.12.1 (sections 7.1, 9.6): Draft Studio no longer gathers on open (operator: wasted model calls and time on items only read or archived); it starts from its Gather context button, from Draft with AI, or from Email from this task. Archive (mail, tickets) / Done (Slack) on every stream row, always visible, so items can be cleared without opening them; Undo in the toast. BLOCKED ledger tasks also show under Waiting (the operator files waits as BLOCKED). |
| 2026-10-01 | 0.29 | App v0.12.0 (sections 7.11, 8.4): Ledger tab (home, browse, search, entity pages with cited briefing, task board, interactions, org, health and audit reports) with 25 allow-listed writes through review cards, single-use tokens, two confirmations for destructive changes and read-back; `people add` stays on the Add to ledger button only. Reads and writes use the ledger's new local server when it runs (0.3-2 s instead of 6-10 s). |
| 2026-09-30 | 0.28 | App v0.11.0 (sections 7.8, 8.1): mail search, labels, attachments, composer files in the approval hash, send-as, highlights and notes, thread export. Attachments are addressed by MIME partId because Gmail's attachmentId changes on every read (found by the live check). |
| 2026-09-30 | 0.27a | App v0.10.1: Google API clients are cached per thread. The shared client's HTTP transport (httplib2) is not thread-safe; concurrent requests (for example People and Full for one item) could fail with TLS "record layer" or "NoneType has no attribute close" errors. Found by the live check of meeting prep. |
| 2026-09-30 | 0.27 | App v0.10.0 (section 7.4): week view, RSVP, meetings with guests through two approvals, meeting prep from item context, meeting log card, weekly repeating blocks. Built before the remaining email items (mail search, labels, attachments, send-as, highlights), which follow as v0.11; the delivery table is renumbered. Ultra-sent meetings are excluded from block moves and deletes. |
| 2026-09-30 | 0.26 | App v0.9.9 (sections 9.7, 9.8): composer AI through Draft Studio with a check on every AI version, check stored per version and shown in composer and review, before/after comparison, deterministic Tidy, learning from edits into style rules; Python 3.13 address-parsing fix for reply-all. |
| 2026-09-30 | 0.25 | App v0.9.8: ticket replies carry and enforce the Ref:MSG line (section 9.6): chosen from the newest desk notice, added to AI text, lint error with one-click fix for operator edits, shown on review; only for replies that go to the desk. |
| 2026-09-30 | 0.24 | App v0.9.7: Draft Studio task mode (section 9.6): Draft email from a task, related-mail gather, recipient allow-list, thread-or-new choice, internal keys scrubbed, standing-rule flags (no unasked meeting offers, no apologies) in both modes; hand-off to the composer as an AI version with both approvals; task log card after the send. |
| 2026-09-30 | 0.23 | App v0.9.6: house facts are never stored in the gather cache; they are looked up fresh on every gather, so a fact the operator turns off or deletes drops out of the next brief at once. |
| 2026-09-30 | 0.22 | App v0.9.5: Draft Studio (section 9.6) shipped. Opening a mail thread starts the gather in the background (thread; each participant's other threads over 120 days; precedents from Sent: two key-word queries plus the operator's own topic words, rarity-weighted and length-normalized, mass mail counted once, then one short model call picks the 2-3 that answer the same kind of request; dated work notes naming a participant; policy passages from the private source list via sitemap ranking; relevant house facts; the item's ledger context), then the brief (asks, constraints, audience, known with validated source tags, unknown for the operator, need_from_sender, at most two risks, plan, precedent shape). Question cards with option buttons and 'save as house fact'; draft with a claim map (only sentences present in the body, only real tags); check marks each factual sentence supported / unsupported / unclear with unverified defaulting to Cut (paraphrased verdicts dropped); the chosen text lands in the composer as an AI version (new `POST /api/drafts/<id>/studio`) and still needs both approvals. House facts: local table, operator-only CRUD dialog, blank topics = always applies. Private config `[draft] sources / notes_dir / generic_words`. Real run on a live thread with the operator's reply hidden: gather + brief 7 s, draft 4 s, check 4 s; it reached the operator's own structure and facts, flagged three process claims found only in past replies as unclear. |
| 2026-09-30 | 0.21 | Draft Studio designed (section 9.6, v0.9.5, next) from the operator's gold-standard reply and one-line rule. Policy source kinds site / page / servicenow_kb; public ServiceNow KB articles are readable without login through the portal page API (guest search returns nothing). The real source list and host notes are private (`local/draft_sources.md`). Operator decisions: house facts yes; all generation on gemini-3.8-flash; gather and brief start when an email is opened. |
| 2026-09-30 | 0.20 | App v0.9.2 (operator rules): every text-generating AI call uses gemini-3.8-flash with no silent fallback to another model (a failure is shown). Every AI call starts with the real local date and time read from the clock at call time; `/api/now` gives the page the server clock; the Day page reads it before building and on every rebuild (shows it in the header, picks plan vs report from it, plans only the rest of today); client-side dates use the configured time zone, not UTC or the device zone. |
| 2026-09-30 | 0.19 | App v0.9.1 (operator ask): task due dates. Ledger 0.1.206 added `tasks add --due/--json`, `tasks update --due/--clear-due`, `tasks list --overdue/--due-before` (the column already existed; no schema change); 0.1.207 made task writes by UUID exact (no fuzzy fallback). Ultra: Due field on the task card, due editor in the task view (confirm, exact UUID, read-back), OVERDUE / DUE TODAY / DUE SOON badges and sort, Day plan's overdue / due-today lists now fill from real dates. Verified end to end against the ledger test DB (5433). |
| 2026-09-30 | 0.18 | App v0.9.0: Day (see the delivery table). Journal now also records Slack posts (`slack_sent`), Ultra calendar block create/move/delete, and archive subjects, so the report is complete. Report text and AI note never include the configured internal ticket prefix (`[ai] hide_ticket_prefix`, private config). Plan: operator reprioritised: Day, Email, Calendar, Ledger tab; Board later. |
| 2026-09-30 | 0.17 | App v0.8.0: item context. People tab = everyone in the item: header addresses plus people named anywhere in the bodies or task text, matched exactly against the whole ledger (catalog of people/labs/GCP/projects/grants/assets, cached 6 h; needs ledger v0.1.205 `--all`). Nicknames, shared names and surname-only mentions are "possible" matches confirmed by click; org units and the operator are never matched. AI possible matches pick only from ledger search results. Full tab = the item's group: dossiers for the first 10 people (senders, named, To, Cc; the operator's recipients rank as senders), trees for named labs/GCP/projects/grants (platforms with >40 links listed but not merged), topic search, one de-duplicated history with the item's own messages, open tasks; runs by itself, cached 15 min per item version. AI briefing (What / History / Issues / Current state / Next steps) cites [L:id] [T:id] [Mn], editable, listen, save-to-ledger via a staged log card. Add to ledger: the only `people add` path; strict ASCII netid/name/title/dept checks in browser and server, duplicate netid refused, same-name warning, two confirmations with single-use 5-minute tokens bound to a hash of the fields, final button disabled 2 s with "Not yet" focused, read-back. The general write path refuses `people add`. |
| 2026-09-30 | 0.16a | App v0.7.2: bucket actions on every stream row (email, Slack, ticket, task): + Bucket, Log, Task (not on tasks), Block. Shown on hover/selection on desktop, always on touch screens; clicking one does not open the item. Dragging to the bucket and onto the Today timeline still works (real mouse drag verified). |
| 2026-09-30 | 0.16 | App v0.7.1 (operator report: audio summary and full read cut off). Causes found and fixed: (1) thinking models spent 1,965 of the 2,048 output tokens on hidden reasoning, so spoken summaries stopped after one sentence; every AI call now gets a capped thinking allowance on top of its answer budget, and summaries/drafts/audio scripts refuse a MAX_TOKENS answer instead of using it; (2) text for speech and for the AI was tail-cut (audio 60,000 chars, summary 30,000, explain 20,000, web context 8,000); now the whole thread goes (runaway guard 400,000, marked if ever applied); (3) link cleanup left stray brackets and "Reply to Sender :" footers. Spoken summaries scale 250/450/700 words with thread length and cover every message; full reads announce each message ("Message 2 of 5, from ..."). Full context tab (L-1 resolved without ledger changes): `dossier --json` (labs, lab assets, grants, projects, GCP projects, every linked interaction in full, cloud audit) + `search --json` (logs that mention them but are not linked) + `tasks list --json` (open tasks linked, related by search, or naming them: full name, "First ... Last", "Last Lab", netid), run in parallel (~10 s), cached 15 min; Copy all, Use for AI draft, Add to bucket. `dossier` added to the read allow-list. |
| 2026-09-30 | 0.15 | App v0.7.0: Slack replies (delivery table row v0.5). Real test: one message to the operator's own DM posted in 19 s including verification; the connector appends "*Sent using* <@...|Claude>" to every post, which the verifier now strips as a final line only. Numbering note: the Slack step shipped as app 0.7.0 because 0.6.x was used by the calendar. |
| 2026-09-30 | 0.14 | App v0.6.1: fix "Missing or wrong token" in tabs left open across a server restart (each start issues a new CSRF token). The page now fetches the current token and retries the write once on a token refusal. The check itself is unchanged. |
| 2026-09-30 | 0.13 | App v0.6.0: Today view (see the delivery table). Calendar writes carry the private extended property `ultra=1` and move/delete refuse anything without it or not organised by the operator (409); blocks never have attendees. Positions use CSSOM because the CSP forbids inline styles. |
| 2026-09-30 | 0.12 | App v0.5.4: fix, an idle open tab never refreshed (refresh only started on /api/stream, which an idle tab does not call). The 5 s status poll now starts any refresh older than its interval (mail 120 s, Slack 15 min, tasks 5 min); one job per source at a time; nothing polls with no tab open. Task data changes also rebuild the stream. |
| 2026-09-30 | 0.11 | App v0.5.3 (operator ask): tailnet access narrowed from the whole 100.64.0.0/10 range to the operator's own devices. `tailnet:mine` in `[server] remote_networks` (default with 192.168.0.0/16) = addresses of nodes owned by the same Tailscale user as this machine, from `tailscale status --json`; shared-in nodes and other users' devices are refused. An unknown tailnet address triggers at most one refresh a minute, so new devices work without a restart. |
| 2026-09-30 | 0.10 | App v0.5.2 (operator ask): the access key did not work on the phone and is removed. Remote mode (`--host 0.0.0.0`) now admits clients by address only: this machine, the Tailscale range 100.64.0.0/10 and 192.168.0.0/16 (`[server] remote_networks`); all others get 403 before routing. Host allow-list, Origin check and CSRF token still apply. `ultra remote` lists the addresses. |
| 2026-09-30 | 0.9 | App v0.5.1 (operator asks). Tasks in the stream: open ledger tasks are stream rows (Tasks filter, also in All), colour-coded by priority (CRITICAL red, HIGH amber) and status (BLOCKED red text, started marker), sorted by priority; opening one shows status, due date, ledger links, and actions Complete (DONE, with Undo), Start, Blocked, Back to to-do, priority, local Snooze (1/3/7 days) and Log update (a staged log card with the task and its people as chips). `e` completes a task. Slack conversations get Mark done (local only, `e`): hidden until a newer message arrives, with Undo; nothing is written to Slack. Remote access: `--host 0.0.0.0` on start/restart/serve (restart keeps the previous host); non-loopback clients need an access key (mode-600 file, `ultra remote-key` prints the links, `--rotate` signs out every device), swapped for an HttpOnly SameSite=Strict cookie; Host allow-list in remote mode accepts IPs, bare machine names and LAN/tailnet suffixes, and refuses public names (DNS rebinding). |
| 2026-09-30 | 0.8 | App v0.5.0 (the v0.45 step). Research adapter runs only `search/list/show/estimate/start` with `--json`; `--json` goes before `--` (after it everything is the prompt, and argparse rejected a trailing `--json`: found against the real CLI, now covered by a fake that behaves like argparse). Ultra's GEMINI_API_KEY is removed from the deep-research child environment. Start needs `confirm: true` and a fresh estimate in the dialog; any edit disables Start again. Audio follows deep-research's chunk/WAV/MP3 path; prices come from `[audio]` config. Server gained `FileResponse` with Range for audio. Verified live: grounded web search (10 s, 5 sources), research search (5.5 s), one 53 s TTS summary ($0.012), cache hit on repeat. |
| 2026-09-30 | 0.7 | App v0.4.0. Operator asks: context must cover the whole email and every person on it (not only the sender), so the rail is now tabbed: People (all participants, each matched to the ledger), Person (full card for one), Ledger search. The stream is inbox-only by default (`mail.include_sent = false`); archived threads never show. Ledger writes: `ledger_write.py` allow-lists log / tasks add / tasks update / link / unlink; every entity argument must be a full UUID; log text goes on stdin; `tasks add` uses `--` before the text; square brackets in task text become parentheses (the ledger's Rich print raises on `[/x]` and rolls the add back); COLUMNS=4000 and NO_COLOR so ids never wrap; `nexus link` exit 0 with "not found" counts as a failure. Cards commit once (claim/finish), validation errors release the card, success publishes after the bucket is cleared. Verified against the real CLI on a throwaway local database, never production. |
| 2026-09-29 | 0.6 | App v0.3.0: composer (reply, reply all, forward, new), every edit a version, restore; lint with errors that block approval; AI summary/draft/revise on Gemini (mail passed as data, output only ever a DRAFT version); approval 1 locks a content hash, approval 2 issues a single-use 10-minute token; send refused if token unknown, used, expired, wrong version, or the stored text no longer hashes to the approved hash; 15 s cancel window; send verified by re-reading the message's SENT label; archive removes INBOX only, with Undo; Ctrl-K palette; demo mode runs the real state machine with a recording outbox. Ruff line length 100. |
| 2026-09-29 | 0.5 | App v0.2.0 shipped: Gmail read (inbox + 14 days of Sent, cached by historyId, batched), ticket cards, court rules (MINE/WAITING/LOW, READY/VIP/SLOW/OVERDUE), calendar RSVP mail and notes-to-self hidden, ledger rail (people show / search / tree / tasks list, cached, parallel), Slack read via claude -p (ANTHROPIC_API_KEY stripped), `ultra auth google`, status bar. Existing token files are reused through config. TOML regex patterns are literal strings. |
| 2026-09-29 | 0.4 | Build started. R-1 done (deep-research --json), R-2 decided (thread text sent to research only when ticked per run). Added Web search (Google Search grounding) to the selection bar and API. |
| 2026-09-29 | 0.3 draft | Stays Python. Added selection functions (Search Nexus, Search research, Research this, Read aloud, Explain; highlights keep their functions), Research panel over the deep-research CLI (7.9), read aloud and AI voice audio (7.10), astropost parity in the mail adapter and composer (search, drafts list, labels create, attachments, send-as, forward, send saved draft), new tables and API routes, `[audio]` and `[research]` config, v0.45 milestone. Replaced X-1 with R-1/R-2. |
| 2026-09-29 | 0.2 draft | Clarified Ultra is a separate app from the deep-research dashboard: borrows its theme (7.0 design tokens, layout, helpers) and its export/selection tools (7.8: Export menu, Copy, standalone HTML, Print/PDF, highlights and notes, AI brief builder). Added `annotations` table, export items in the delivery plan, audio exports as open item X-1. |
| 2026-09-29 | 0.1 draft | First full spec from the design review (idea A as shell, B/C/D/E folded in). Decisions: ledger reads through CLI `--json`; mail and Slack send allowed after revision rounds and two approvals; ServiceNow handled from email; standalone public uv tool. |
