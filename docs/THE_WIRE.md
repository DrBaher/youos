# The Wire — newsletter digest setup

The Wire turns the day's newsletters across your accounts into **one** Gmail-safe
HTML email — every story extracted, deduplicated across sources, and grouped
into themed sections — then archives the originals. Each issue carries a
sequential edition number.

It runs on a schedule (default **weekdays at 19:00**) once it's enabled, and you
can also trigger it by hand. This guide takes you from nothing to a working
daily Wire.

> Implementation: `app/agent/wire_digest.py`. It reuses the digest engine's
> safety machinery, so a real send is gated exactly like every other outbound
> path (see [AGENT_SAFETY_MODEL.md](AGENT_SAFETY_MODEL.md)).

## Prerequisites

- YouOS installed and running, with **at least one Gmail account authenticated**
  via the default `gog` backend (`gog auth login`) and ingested by YouOS. The
  Wire reads from whatever accounts YouOS already ingests — no separate account
  wiring.
- For the rich themed summary, the cloud summarizer (`summary_model: cloud`,
  the default) uses the Claude CLI. `local` uses the on-device model instead
  (simpler result, **no egress** — see [Things to know](#things-to-know)).

## 1. Preview first (read-only — no setup, no gating)

This proves collection + summarization work before you enable anything. It
sends and archives nothing and works even with every switch off:

```bash
youos wire run --preview            # build + print today's digest HTML
youos wire run --preview --days 7   # try a wider collection window
```

It writes the rendered HTML to a temp file you can open in a browser.

## 2. Add the `agent.wire` block to your instance `youos_config.yaml`

Minimal configuration — everything else has sane defaults:

```yaml
agent:
  wire:
    enabled: true          # the Wire's master switch
    hour: 19               # local time-of-day to run
    weekdays_only: true    # Mon–Fri (set false for every day)
    days_back: 2           # how far back to collect each run
    summary_model: cloud   # 'cloud' (Claude, rich) | 'local' (on-device, no egress)
```

## 3. Open the send frontier

The Wire **emails you and archives mail**, so it crosses the never-send
frontier. Three gates must **all** be open, and — for security — they can only
be set **out-of-band** (the `youos config` CLI or a direct file edit), never the
web UI (a network token must not be able to arm outbound):

```bash
youos config set agent.send.enabled true        # shared outbound frontier
youos config set agent.outbound_kill_switch false
youos config set agent.wire.enabled true         # the Wire's own switch
```

If any gate is closed, a real run records `blocked` and sends nothing.

## 4. First run + verify

```bash
youos wire status                  # enabled/schedule state + next edition number
youos wire run                     # send today's edition now
youos wire run --days 7 --force    # one-time backfill of the past week
```

After this, the **running server** fires the Wire automatically on schedule. The
server must be up for scheduled runs; manual `youos wire run` works any time.

## 5. Optional tuning

Scalars can be set with `youos config set agent.wire.<key> <value>`; the list
fields are easiest to edit directly in `youos_config.yaml` under `agent.wire`:

| Key | Default | Purpose |
| --- | --- | --- |
| `from_account` | first account | which account sends the digest |
| `deliver_to` | own inbox | where the digest is delivered |
| `max_emails` | 600 | cap on newsletters collected per run (paginates past Gmail's 500/page) |
| `work_accounts` | none | accounts that contribute **only** categorized promotions/updates, not personal mail |
| `skip_from` | generic list | senders never worth a digest entry — **replaces** the default |
| `skip_subject` | generic list | subject substrings to skip (receipts, calendar invites, …) |
| `promo_from` | brand list | senders routed to a 1-line **Promotions** section |
| `archive_exclusions` | none | senders never archived after a digest (stay in your inbox) |
| `seed_edition` | 0 | start numbering at N+1 (e.g. migrating from another tool) |

```yaml
agent:
  wire:
    work_accounts: [you@company.com]
    skip_from: [paypal, stripe, no-reply@accounts.google.com]
    archive_exclusions: [some-author.example]
    seed_edition: 0
```

## Things to know

- **Cloud egress.** With `summary_model: cloud`, newsletter *bodies* are sent to
  the Claude CLI to be summarized (newsletters are bulk mail; the digest is
  read-only). Use `local` for zero egress.
- **Volume & timeout.** A heavy day or a multi-day `--days` backfill is
  summarized in **batches** so it never degrades to a flat list. A very large
  edition (hundreds of stories) can exceed Gmail's ~102 KB inline limit and show
  "[Message clipped]" — the full digest still arrives.
- **At-most-once per day + dedup.** Each day's edition is claimed once and every
  message is recorded, so re-running the same day is a safe no-op. Use `--force`
  for an explicit ad-hoc rebuild that ignores both guards.
- **Editions** are stored per-instance at `var/wire_edition.json`.

## CLI reference

```text
youos wire status                       # enabled/schedule + next edition
youos wire run                          # send one edition (gated)
youos wire run --preview                # build + print only; no send/archive
youos wire run --days N                 # override the collection window
youos wire run --force                  # bypass once-per-day + dedup (manual rebuild)
```
