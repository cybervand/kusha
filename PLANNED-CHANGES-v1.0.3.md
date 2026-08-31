# Kusha v1.0.3 — Planned Changes (handoff briefing)

Written 2026-08-31 by a Claude session working in the **Lava** repo, after a
live debugging session against the production pair. Nothing here is
implemented yet — this doc is the spec for whoever (human or Claude) works
in this directory next.

## Context you don't have

- Kusha (this repo, FastAPI + pyserial, `main.py`) is the SMS gateway twin
  of **Lava** (the SMS web app, sibling repo `../lava`). Kusha talks to an
  AES iGate Prime GSM device via a serial modem; Lava talks to Kusha over
  HTTPS on port 6969.
- Production: both run as Docker containers on host `justasimcaard`
  (192.168.180.38, SSH as `ikt`, key auth, ikt is in the docker group).
  Kusha's deployed compose lives at `~/Documents/kusha-1.0.2/` on that host.
  Current image: `kusha:1.0.2`.
- **Kusha has multiple customers, but only one inbox reader.** Verified
  2026-08-31: the reception apps on nygaard (`/opt/reception` and the
  `sens-besok-app` sms-bridge) use Kusha **send-only**
  (`POST /sms/messages`); neither touches `/sms/inbox`. As of Lava
  v7.2.0, **Lava's server is the single reader** of `GET /sms/inbox`
  (mutexed drain + 15s interval — `lava/lib/inbox-poller.js`); browser
  tabs no longer hit Kusha directly. Keep it that way: with
  delete-after-serve, a second inbox reader steals messages. If another
  app ever needs incoming SMS, that's a design change (per-consumer
  cursors or sender-based routing) — not another poller.

## What the live debugging found (2026-08-31)

A 10-chunk `*21#` sync reply lost 2 chunks and duplicated 2 when two
consumers polled `/sms/inbox` concurrently. Root causes, all in `main.py`:

1. **Read race in `get_inbox()`** (~line 658): concurrent calls both run
   `SELECT ... WHERE unread = 1` before either runs the
   `UPDATE ... SET unread = 0` — both receive the same rows (duplicates),
   and later pollers get nothing (lost chunks). Lava-side single-consumer
   removes the trigger, but the endpoint is still racy by construction.
2. **`InboxMessage` exposes only `{number, text}`** (~line 194). The DB has
   `id` (autoincrement) and `created_at` for every row — they're just
   stripped. Downstream, Lava cannot deduplicate or order reliably without
   them.
3. **Messages are retained forever, contrary to the security intent.**
   Peter's design goal: a compromised Kusha should hold no message history.
   In reality `/sms/inbox` only flags rows read; `GET /sms/messages` serves
   the complete history to anyone with the API key, and rows accumulate
   until Lava's phonebook sync happens to call `DELETE /sms/messages`.
   (The modem/SIM side IS drained correctly — `fetch_unread_messages()`
   deletes from SIM after import. The DB is the leak.)
4. **Zombie process leak**: uvicorn (PID 1 in the container) accumulates
   ~160 zombie children over time and never reaps them (observed on the
   host via `ps`). No `init` in the container.
5. **API key is optional**: with no `SMS_API_KEY` env set, every endpoint
   is open to the LAN (send SMS + read history). Verify the deployed
   compose actually sets it (Lava sends `X-API-Key` when its config has
   `apiKey` set — check both sides match).

## The changes for v1.0.3

### 1. Expose `id` and `timestamp` in `/sms/inbox`

```python
class InboxMessage(BaseModel):
    id: int
    number: str
    text: str
    timestamp: str   # ISO 8601
```

**Field names matter — this is the contract with Lava.** Lava's ingestion
(`lava/lib/messages-sqlite.js addReceived`) reads, in order:
- upstream id: `msg.id || msg.messageId || msg.smsId` → becomes stable
  dedup key `upstream-<id>`
- receive time: `msg.timestamp || msg.date || msg.receivedAt`

So expose exactly `id` and `timestamp` and Lava needs **zero changes**.
`created_at` as a field name would NOT be picked up.

### 2. True delete-after-serve in `get_inbox()`

Replace the `unread`-flag flip with an atomic claim-and-delete so the
security intent becomes real (safe now: Lava persists everything it
receives before Kusha forgets it — Kusha serves each message exactly once
and then drops it). Make the SELECT+DELETE atomic (single transaction /
`DELETE ... RETURNING` on modern sqlite3) so the old read race cannot
duplicate rows even with concurrent callers.

Decide what to do about outgoing rows and `GET /sms/messages` (the
list-everything endpoint): with the no-history goal, that endpoint should
probably go, or serve only the transient unserved backlog. Note the
outgoing rows include the RECEPTION apps' sent messages (visitor
notifications — names/phone numbers, i.e. PII), which strengthens the
case for not retaining. Also note `DELETE /sms/messages` (which Lava's
phonebook sync calls) currently wipes reception's sent rows too — shared
blast radius. Ask Peter.

### 3. Capture the modem's real receive time

`fetch_unread_messages()` already parses the CMGL header
(`+CMGL: 2,"REC READ","+47...",,"25/11/20,01:11:57+04"`) and throws away
the SCTS timestamp (`parts[4]`/`parts[5]` — date and time are separate
comma-split parts; recombine and parse, mind the quirky `yy/MM/dd` format
and the quarter-hour zone suffix). Store it as the row's timestamp so
ordering reflects when the SMS actually arrived, falling back to
`CURRENT_TIMESTAMP` when parsing fails.

### 4. Reap zombies

Add `init: true` to the service in `docker-compose.yml` (tini becomes
PID 1 and reaps). Cheap and sufficient; finding the actual leaking
subprocess in uvicorn/pyserial is optional archaeology.

### 5. Verify `SMS_API_KEY` is set in the deployed compose

If it isn't, generate one, set it in Kusha's env, and set the same value
as `apiKey` in Lava's settings (Settings tab, or its encrypted config).

## Version & deploy notes

- Bump `VERSION` in `main.py` and any spots listed in
  `VERSION_LOCATIONS.md`; suggested tag `kusha:1.0.3`.
- Deploying restarts the SMS gateway — a brief window where device replies
  can sit on the SIM (the modem buffers; kusha imports on startup, so
  nothing is lost — but avoid restarting mid-phonebook-sync).
  **The restart also briefly breaks SMS sending for the reception apps**
  (visitor notifications) — pick a quiet moment.
- Update the deployed compose dir on justasimcaard
  (`~/Documents/kusha-1.0.2/` — consider renaming the dir or just leaving
  it), `docker build`, `docker compose up -d`.
- After deploy, from the Lava side, verify: send `*20#` from Lava's
  Messages tab; the reply should appear once, with a realistic timestamp,
  and `lava/data` ingestion logs should show `upstream-<id>` dedup keys.
  Then run a phonebook "Sync Permanent" — every chunk should arrive
  exactly once.

## Status: IMPLEMENTED & DEPLOYED 2026-08-31

All changes above are implemented and `kusha:1.0.3` is live on
justasimcaard (deployed from `~/Documents/kusha-1.0.3/`). Decisions made
by Peter during implementation:

- **Retention:** full no-history. Outgoing rows are never stored;
  `/sms/inbox` claim-and-deletes atomically (`BEGIN IMMEDIATE`);
  `GET /sms/messages` kept but only ever shows the unserved backlog;
  `init_db()` purges pre-1.0.3 leftovers on startup (verified: history
  endpoint returned `[]` right after deploy).
- **Item 5 (API key): verified NOT set, and deliberately left unset.**
  Enabling it would also gate `POST /sms/messages`, and nobody has
  verified whether the reception apps on nygaard send `X-API-Key` —
  enabling would silently break visitor notifications. Rollout plan:
  check/update the nygaard apps first, then set the same key in Kusha's
  `.env`, Lava's settings, and reception in one coordinated change.
- Zombie cause found: the healthcheck's busybox wget spawns an
  `ssl_client` child every 30s that PID-1 uvicorn never reaped (~8850
  zombies after 3 days). `init: true` fixed it (0 zombies post-deploy,
  `docker-init` is PID 1).
- Post-deploy: container healthy, Lava's 15s drain polling with 200s.
  The `*20#`/sync end-to-end check still needs a human with the Lava UI.
- Unrelated observation in Lava's logs: Microsoft token refresh failing
  with AADSTS700016 (app id not found in tenant) — calendar/service
  account auth is broken and worth a look.

## Explicitly out of scope

- Don't make `/sms/inbox` non-destructive "to be safe" — the
  forget-after-serve behavior is Peter's deliberate security design.
- Don't add more consumers of `/sms/inbox`; Lava's server owns it.
