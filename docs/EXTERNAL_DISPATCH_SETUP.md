# External dispatcher for trade scheduling

Docs only — nothing in this repo calls the Worker. The operator creates the Cloudflare
account resources and the GitHub token; this file is the whole procedure.

## Why

GitHub's `schedule` trigger is best-effort, and for this repo it is late by hours. On
2026-10-05 scheduled trade runs landed **3–8 h after their cron time** with essentially
**zero queue wait** once started — the delay is in *schedule delivery*, not in runner or
concurrency-group queueing (2026-10-05 pipeline review §1; re-verify any time with
`gh run list --workflow trade.yml --json createdAt,startedAt,event`: `createdAt` far from
the cron time and `startedAt` ≈ `createdAt` means delivery delay; a long `createdAt →
startedAt` gap would mean queueing and this fix would NOT help).

The cost is real: 7 of 14 PM-approved buys since 9/02 were decided after the entry gate
closed, and 38% of runs ended post-close. A `workflow_dispatch` API call starts a run
within seconds, so an external clock calling it at the intended time removes the delay.

Design (already in the repo, WS-A):

- `trade.yml` accepts a `slot` input — the intended UTC time, `YYYY-MM-DDTHH:MM`.
- `main.py` exits 0 if the journal already holds a completed run for that slot, so the
  external dispatch and the **GH cron backup (kept as-is)** can both fire for a slot
  without double-trading. First arrival wins; the second is a no-op.
- The GH cron computes its slot with `scripts/compute_slot.py` from the cron line, i.e.
  `<scheduled day>T14:05` / `T17:00`. **The Worker's slot string must match that format
  and those times exactly**, or the two paths will not dedupe and the day trades twice.
- `heartbeat.yml` checks the same slots (`--slots 14:05,17:00`) — keep all three in sync.

## What you need

1. A Cloudflare account (free plan is enough: cron triggers are free, 1 invocation per slot).
2. A GitHub **fine-grained personal access token**:
   - Resource owner: `nick-tan01`; **Only select repositories** → `Pocket-Hedge-Fund`.
   - Repository permissions: **Actions: Read and write**. (Metadata: Read-only is added
     automatically.) Nothing else — no Contents, no Secrets, no Administration.
   - Expiration: set one (e.g. 90 days) and put the renewal date in your calendar. An
     expired token fails silently from the repo's point of view — the GH cron backup then
     takes over, which is exactly the degraded mode you had before.
   - Blast radius if leaked: someone could dispatch/cancel/delete workflow runs in this
     one repo. They cannot push code or read secrets.
3. `npm` and `wrangler` (`npm i -g wrangler`, then `wrangler login`).

## Worker

`dispatcher/wrangler.toml` (create the folder anywhere outside this repo, or in it — your call):

```toml
name = "phf-trade-dispatcher"
main = "src/index.js"
compatibility_date = "2026-10-01"

[triggers]
# UTC, same instants as the trade.yml crons ("5 14" and "0 17", Mon-Fri).
# Named weekdays on purpose: Cloudflare's numeric day-of-week differs from Linux cron.
crons = ["5 14 * * MON-FRI", "0 17 * * MON-FRI"]

[vars]
GH_REPO = "nick-tan01/Pocket-Hedge-Fund"
GH_REF = "main"
GH_WORKFLOW = "trade.yml"
DRY_RUN = "0"
```

`dispatcher/src/index.js`:

```js
// Cron-only Worker: no fetch handler, so there is no public URL that can trigger a trade.
export default {
  async scheduled(event, env, ctx) {
    ctx.waitUntil(dispatch(event, env));
  },
};

// event.scheduledTime is the INTENDED fire time (ms epoch), even if Cloudflare is late,
// so the slot always equals the cron instant -> matches scripts/compute_slot.py.
function slotFor(event) {
  return new Date(event.scheduledTime).toISOString().slice(0, 16); // YYYY-MM-DDTHH:MM (UTC)
}

async function dispatch(event, env) {
  const when = new Date(event.scheduledTime);
  const dow = when.getUTCDay();
  if (dow === 0 || dow === 6) return; // belt and braces with the MON-FRI cron

  const slot = slotFor(event);
  const url = `https://api.github.com/repos/${env.GH_REPO}/actions/workflows/${env.GH_WORKFLOW}/dispatches`;
  const body = JSON.stringify({
    ref: env.GH_REF,
    inputs: { slot, reason: "external_dispatch" },
  });

  if (env.DRY_RUN === "1") {
    console.log(`DRY_RUN would POST ${url} ${body}`);
    return;
  }

  // Retry only transport errors and 5xx. A 4xx (bad token, wrong repo/workflow, bad input)
  // will not fix itself — fail loudly. A retry after a lost 204 can create a second run;
  // that is safe: the repo's slot idempotency makes the later run exit 0.
  let lastErr;
  for (let attempt = 1; attempt <= 3; attempt++) {
    try {
      const res = await fetch(url, {
        method: "POST",
        headers: {
          Authorization: `Bearer ${env.GH_TOKEN}`,
          Accept: "application/vnd.github+json",
          "X-GitHub-Api-Version": "2022-11-28",
          "User-Agent": "phf-trade-dispatcher",
          "Content-Type": "application/json",
        },
        body,
      });
      if (res.status === 204) {
        console.log(`dispatched slot ${slot}`);
        if (env.HC_PING_URL) await fetch(env.HC_PING_URL); // optional dead-man's switch
        return;
      }
      const text = await res.text();
      lastErr = new Error(`GitHub ${res.status}: ${text.slice(0, 300)}`);
      if (res.status < 500) break;
    } catch (e) {
      lastErr = e;
    }
    await new Promise((r) => setTimeout(r, attempt * 2000));
  }
  if (env.HC_PING_URL) await fetch(env.HC_PING_URL + "/fail").catch(() => {});
  throw lastErr; // surfaces in Worker logs / Cloudflare cron error metrics
}
```

## Step by step

1. **Create the token** (GitHub → Settings → Developer settings → Fine-grained tokens)
   with the scope above. Copy it once.
2. `cd dispatcher && wrangler secret put GH_TOKEN` and paste the token. (A secret, not a
   `[vars]` entry — vars are visible in the dashboard and in `wrangler.toml`.)
3. **Dry run first.** Set `DRY_RUN = "1"` in `wrangler.toml`, then
   `wrangler dev --test-scheduled` and in another shell
   `curl "http://localhost:8787/__scheduled?cron=5+14+*+*+MON-FRI"`. The log should print the
   POST URL and a body whose `slot` is `YYYY-MM-DDTHH:MM`. Compare it with
   `python3 scripts/compute_slot.py --cron "5 14 * * 1-5" --now <same instant>` — they must
   be the same string for the 14:05 slot (and `0 17 …` for 17:00).
4. Set `DRY_RUN = "0"` and `wrangler deploy`. The cron triggers appear under the Worker →
   Settings → Triggers.
5. **First live check** (next 14:05 / 17:00 UTC weekday): the Actions tab shows a
   `workflow_dispatch` run started within seconds of the slot; the journal run record has
   `slot` set and `run_meta.started_at` ≈ `scheduled_for`; `gh run list --workflow trade.yml
   --json createdAt,startedAt,event,displayTitle` shows the dispatch run *and later* a GH-cron
   run for the same slot that exits 0 without trading. That second run is the idempotency
   working, not a bug.
6. **Success criteria** (from the 2026-10-05 review's R1a): over 20 trading days ≥95% of slots start
   within 10 minutes of schedule; share of PM buys lost to the market gate < 10%; zero
   post-close debate runs.
7. *(Optional, recommended)* Add a healthchecks.io check with a ~25 h period / 90 min grace,
   set its ping URL as the `HC_PING_URL` secret (`wrangler secret put HC_PING_URL`). It is
   the only monitor that does not depend on GitHub's scheduler.

## Operating notes

- **Rollback:** `wrangler delete`, or remove the `crons` lines and redeploy. The GH cron
  backup keeps running unchanged, so rollback restores the pre-change behaviour with no repo edit.
- **Changing run times:** edit all three together — `trade.yml` crons, the Worker `crons`,
  and `heartbeat.yml --slots`. A Worker time that differs from the GH cron time is a
  *different slot* and will not dedupe.
- **DST:** both schedules are fixed UTC. 14:05 UTC is 10:05 EDT / 09:05 EST — after the
  30-minute open buffer in both seasons.
- **Holidays:** the Worker fires on market holidays like the GH cron does; the run's entry
  gate (`market_closed`) makes it a no-op review-only run.
- **Not verified here:** this file was written without access to a Cloudflare account or to
  GitHub's API, so the Worker has not been executed end to end. Steps 3 and 5 are the
  verification; do not skip the dry run.
