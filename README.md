# Jobsearch-AI

An agent that searches for jobs matching your preferences, writes the cover
letter or statement of purpose each one needs, and submits the application —
after you approve it.

You give it your CV, your details, and what you are looking for. Every morning
it searches, scores what it finds against your profile, drafts the documents
for the matches, and emails you a link. You open the dashboard, read what it
wrote, edit anything you want, and click Approve. Nothing is ever sent under
your name without that click.

```
   profile.yaml + CV
           │
           ▼
    ┌─────────────┐   company boards + aggregators
    │  discover   │◀──────────────────────────────
    └──────┬──────┘
           ▼
    ┌─────────────┐   your hard preferences — every rejection keeps a reason
    │   filter    │
    └──────┬──────┘
           ▼
    ┌─────────────┐   Claude scores fit 0-100 against your actual CV
    │    score    │
    └──────┬──────┘
           ▼
    ┌─────────────┐   cover letter / SOP / screening answers
    │    write    │
    └──────┬──────┘
           ▼
    ┌─────────────┐   ◀── you: read, edit, approve or skip
    │   YOUR CALL │
    └──────┬──────┘
           ▼
    ┌─────────────┐   ATS submit where possible, ready-to-paste packet otherwise
    │   submit    │
    └─────────────┘
```

---

## Quick start

```bash
git clone <this repo> && cd Jobsearch-AI

cp .env.example .env          # add your ANTHROPIC_API_KEY
docker compose run --rm jobsearch jobsearch init

# 1. put your CV in data/cv/         (PDF, DOCX, TXT or MD)
# 2. fill in data/profile.yaml       (identity, experience, preferences, sources)

docker compose run --rm jobsearch jobsearch doctor   # checks everything
docker compose up -d                                 # dashboard + daily schedule
```

Then open **http://localhost:8765**.

To run a search immediately instead of waiting for the schedule:

```bash
docker compose exec jobsearch jobsearch discover
```

<details>
<summary>Without Docker</summary>

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
jobsearch init
jobsearch doctor
jobsearch serve
```
</details>

---

## The two files you fill in

### `data/profile.yaml`

Created by `jobsearch init`, fully commented. The parts that matter most:

| Section | What it drives |
|---|---|
| `identity`, `work_authorization` | The fields every application form asks for |
| `experience`, `education`, `skills` | Evidence the writer is allowed to draw on |
| `cv_file` | The CV attached to applications *and* used as source material |
| `preferences` | The search: titles, locations, remote/hybrid, seniority, exclusions |
| `sources` | Which boards to search |
| `answers` | Your standard answers to recurring screening questions |
| `writing` | Tone, length, language, phrases to avoid |

The more of `answers` you fill in, the more applications complete without
coming back to you.

### `.env`

Your API key and the operational settings — score threshold, daily cap,
schedule, submission mode, SMTP. All documented inline in `.env.example`.

---

## Where it searches

| Source | Needs | Notes |
|---|---|---|
| **Greenhouse** | board token | `job-boards.greenhouse.io/`**`stripe`** |
| **Lever** | company slug | `jobs.lever.co/`**`netflix`** |
| **Ashby** | org name | `jobs.ashbyhq.com/`**`ramp`** |
| **Workable** | account | `apply.workable.com/`**`acme`** |
| **Remotive** | — | remote roles, full descriptions |
| **Arbeitnow** | — | Europe, strong German coverage |
| **RemoteOK** | — | off by default; requires attribution |
| **Adzuna** | free API key | broad aggregate search, truncated descriptions |

Company boards give by far the best results: full descriptions, real screening
questions, and a direct apply route. Add the companies you actually want to
work at. `jobsearch doctor` tells you whether each one responds and how many
postings it returned.

When the same role appears on several sources it is collapsed into one entry,
and the copy that can be applied to directly wins.

---

## How selective it is

A posting reaches your queue only if it passes **both** gates:

1. **Hard filters** — title, location, remote mode, employment type,
   seniority, excluded keywords and companies, posting age, and sponsorship
   requirements. These are cheap, run first, and cost nothing. Every rejection
   records *why*, visible at `/jobs?show=filtered` — that page is the fastest
   way to work out why your queue is empty or full of noise.
2. **Fit score** — Claude reads the posting against your CV and scores it
   0-100 with a rationale, strengths, gaps and red flags. Anything below
   `MIN_SCORE` (default 70), or with a red flag such as an eligibility
   requirement you cannot meet, is kept but not queued.

`MAX_NEW_APPLICATIONS_PER_DAY` caps how many documents get written per day, and
the cap is spent on the highest-scoring matches first.

---

## What it writes

Claude picks what the posting actually calls for — a cover letter for most
roles, a statement of purpose for academic and fellowship applications, a
motivation letter where that is the convention — and writes it from your CV
and profile.

The writing prompts forbid inventing employers, degrees, metrics or years of
experience, and forbid placeholders. Known gaps are handed to the writer
explicitly with instructions to address them honestly or leave them out.

Screening questions are answered from your `answers` block and your CV. **When
the writer has to guess — a salary expectation, a notice period, anything
legally consequential that is not in your profile — it flags the answer and
submission is blocked until you confirm it.**

Everything is editable in the dashboard, and you can ask for a rewrite with an
instruction like *"lead with the payments work, keep it under 250 words"*.

---

## How it applies

Approving an application submits it. What that means depends on the ATS:

| ATS | Route |
|---|---|
| Greenhouse, Lever | Direct form submission (see the verification note below) |
| Ashby, Workable | Manual packet — these accept applications only through their own front end or with an employer API key that applicants do not have |
| Anything else | Manual packet |

A **manual packet** is a folder under `data/output/` containing every document
as PDF, DOCX and plain text, a copy of your CV, and an `APPLY.txt` listing
every field value in the order the form asks for them. Finishing by hand takes
about a minute. Manual is a normal outcome, not a failure — and any automated
submission that stalls produces one too, so you are never left at a dead end.

### Verifying the submission recipes before going live

**`SUBMIT_MODE` defaults to `dry_run`, and the shipped Greenhouse and Lever
recipes are marked unverified.** Applicant-facing form endpoints are not
published API contracts, and they were not confirmed against a live board when
this code was written — the build environment had no network route to any job
board. In `live` mode an unverified recipe deliberately falls back to a manual
packet rather than firing a request that might quietly fail.

To turn one on:

```bash
jobsearch submit <application-id> --dry-run --show-request
```

That prints the exact request that would be sent — URL, every field name and
value, the attached file. Compare it against a real submission in your
browser's network tab. If it matches, set `verified = True` for that recipe in
`jobsearch/apply/recipes.py` and switch `SUBMIT_MODE=live`. If it does not,
the recipe is plain data — fix the endpoint or the field names in that same
file, no Python required.

This is deliberately a decision you make with evidence in front of you, not a
default you inherit.

---

## Deploying it somewhere that stays on

The daily search fires from an in-process cron, so **the box has to be awake at
`DIGEST_CRON` time**. A laptop that sleeps at night will silently skip runs.
What it needs:

- an always-on process (not serverless, not scale-to-zero)
- a persistent disk for `data/` — SQLite, your CV, and generated packets
- 512 MB RAM is enough, 1 GB comfortable; CPU is near-idle between runs
- outbound HTTPS to the job boards and the Claude API

| Where | Cost | Notes |
|---|---|---|
| **Home server / Raspberry Pi** | free | Best privacy — your CV never leaves the house. A Pi 4 handles it easily. |
| **Small VPS** (Hetzner, DigitalOcean, Vultr) | ~€4–6/mo | The usual choice. `docker compose up -d` and it's done. Secure it as below. |
| **Fly.io / Railway / Render** | ~$5/mo | Works, but attach a **persistent volume** or SQLite is wiped on redeploy, and **disable scale-to-zero** or the cron never fires (Render's free tier sleeps; Fly needs `auto_stop_machines = false`). |
| **Always-on desktop** | free | Fine. Just confirm it doesn't sleep — set the cron to a time you know it's awake. |

### Raspberry Pi / home server, step by step

The best option if you have the hardware: your CV never leaves the house, and
there is no monthly bill.

**Use 64-bit Raspberry Pi OS.** On 64-bit every dependency installs from a
prebuilt wheel in a couple of minutes. On 32-bit, `uvloop`, `httptools`,
`markupsafe` and `sqlalchemy` have no wheels and compile from source — slow,
and a 1 GB Pi can run out of memory doing it. Check with `uname -m`: `aarch64`
is good, `armv7l` is the 32-bit build.

A Pi 4 (2 GB+) is comfortable. A Pi 3 or Zero 2 W works but is slow to install.

```bash
sudo apt update && sudo apt install -y python3-venv git

git clone -b claude/job-application-ai-agent-gqk916 \
    https://github.com/vurghunimran/Jobsearch-AI ~/Jobsearch-AI
cd ~/Jobsearch-AI

python3 -m venv .venv
.venv/bin/pip install -e .

cp .env.example .env        # add ANTHROPIC_API_KEY and your SMTP details
.venv/bin/jobsearch init    # writes data/profile.yaml
# put your CV in data/cv/, then fill in data/profile.yaml
.venv/bin/jobsearch doctor
```

Set the timezone in `.env` — `DIGEST_CRON` is interpreted in it, and a Pi often
defaults to UTC:

```bash
TIMEZONE=Europe/Tallinn      # yours
DIGEST_CRON=0 8 * * *        # 08:00 local
DASHBOARD_HOST=0.0.0.0       # so you can reach it from your phone
DASHBOARD_TOKEN=             # see the note below before setting this
```

Run it as a service so it survives reboots and power cuts:

```bash
sudo cp deploy/jobsearch.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now jobsearch

systemctl status jobsearch
journalctl -u jobsearch -f      # watch the first search run
```

The unit assumes `/home/pi/Jobsearch-AI` and user `pi`; edit the paths if
yours differ. Docker works on the Pi too (`docker compose up -d`), but native
saves ~100 MB of RAM and boots faster — worth it on a small Pi.

**Reaching it from your phone.** Install
[Tailscale](https://tailscale.com/download) on the Pi and your phone, then set
`PUBLIC_BASE_URL=http://<pi-tailscale-name>:8765`. The dashboard is reachable
from anywhere without opening a single port on your router, and you can leave
`DASHBOARD_TOKEN` empty on a private tailnet. **Do not port-forward 8765 from
your router** — that puts your CV and personal details on the public internet.
If you must, set a token and put Caddy in front for HTTPS.

**Two Pi-specific habits.** SD cards wear out and do fail: run from a USB SSD
if you can, and back up the database, which holds your whole application
history —

```bash
sqlite3 data/jobsearch.db ".backup '/mnt/usb/jobsearch-$(date +%F).db'"
```

A weekly `cron` entry for that line is enough.


### Securing a remote deployment

The compose file binds to `127.0.0.1` deliberately. Don't change that to
`0.0.0.0` and call it done — the dashboard holds your CV, your personal
details and your drafted applications. Two good options:

**Tailscale (recommended).** Install it on the VPS and your phone. The
dashboard stays completely off the public internet, and you reach it from
anywhere as if it were local:

```bash
# .env
PUBLIC_BASE_URL=http://<your-tailscale-hostname>:8765
DASHBOARD_TOKEN=            # optional on a tailnet
```

**Public with TLS.** Put Caddy or nginx in front for HTTPS and set a token:

```bash
# .env
PUBLIC_BASE_URL=https://jobs.yourdomain.com
DASHBOARD_TOKEN=$(openssl rand -hex 24)
```

With a token set, the digest email's links carry it and the dashboard swaps it
for a cookie on arrival — so tapping a link on your phone signs you in once and
every link after that just works. Every route is gated except `/healthz` and
`/static`.

### Getting the notifications

The digest is plain SMTP, so any provider works. For personal use a Gmail
[app password](https://myaccount.google.com/apppasswords) is the fastest route
(a normal account password will not work with 2FA enabled):

```bash
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USERNAME=you@gmail.com
SMTP_PASSWORD=<16-character app password>
SMTP_FROM=you@gmail.com
DIGEST_TO=you@gmail.com
```

Resend, Brevo, Mailgun and Postmark all have free tiers and work the same way.
Test it without waiting for tomorrow's run:

```bash
docker compose exec jobsearch jobsearch discover   # find and draft
docker compose exec jobsearch jobsearch digest     # send the email now
```

If you'd rather get a phone push than an email, `jobsearch/notify/email.py` is
the only place notification lives — a Telegram or ntfy sender slots in beside
`send_digest` without touching anything else.

### First week

Run with `SUBMIT_MODE=dry_run` for a few days. Read what it queues and what it
writes, check `/jobs?show=filtered` to see what it threw away and why, and tune
`preferences` until the queue is mostly roles you'd genuinely apply to. Only
then verify a submission recipe and switch to `live`.

---

## Commands

| Command | What it does |
|---|---|
| `jobsearch init` | Create `data/profile.yaml` and the folders |
| `jobsearch doctor` | Check config, profile, CV, every source, every recipe |
| `jobsearch discover` | Run one full search now |
| `jobsearch serve` | Dashboard + daily scheduler |
| `jobsearch queue` | List what is awaiting review |
| `jobsearch submit <id> --dry-run --show-request` | Preview the exact request |
| `jobsearch approve <id>` | Approve and submit from the terminal |
| `jobsearch digest` | Re-send the digest email |

---

## Your data

Everything — CV, profile, database, generated documents — stays in `data/`,
which is git-ignored and, under Docker, a local volume. The dashboard binds to
`127.0.0.1` only. Set `DASHBOARD_TOKEN` if the port is reachable by anyone
else.

Your CV and profile are sent to the Claude API to score jobs and write
documents, and to an employer only when you approve that specific application.
The dashboard and the digest email load no third-party assets.

---

## A word on automated applying

Some job boards' terms of service prohibit automated submission. This tool
applies to postings on employers' own ATS-hosted forms and keeps a human
approval step in front of every send, but checking the terms of the boards you
target is on you. Volume is not the point either: fifteen well-matched
applications with letters that mention something real about the company beat
three hundred generic ones, and `MAX_NEW_APPLICATIONS_PER_DAY` exists to keep
you on the right side of that.

---

## Development

```bash
pip install -e ".[dev]"
pytest              # 149 tests, no network and no API key needed
ruff check jobsearch tests
ruff format jobsearch tests
```

The test suite stubs Claude and mocks HTTP, so it runs offline. Connector tests
assert against realistic API payloads, which is what catches a source changing
its response shape.

```
jobsearch/
├── config.py          settings
├── logging_setup.py   log config (uvicorn only configures its own)
├── models.py          database tables
├── llm.py             Claude wrapper
├── pipeline.py        the daily run
├── scheduler.py       cron
├── cli.py             commands
├── profile/           your YAML + CV text extraction
├── sources/           one module per board, plus dedup
├── matching/          hard filters, normalisation, fit scoring
├── documents/         prompts, generation, PDF/DOCX rendering
├── apply/             recipes, submission engine, manual packets
├── notify/            digest email
└── web/               dashboard
```
