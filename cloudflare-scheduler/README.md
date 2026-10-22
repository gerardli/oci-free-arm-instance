# Cloudflare Cron → existing OCI GitHub Actions

**Purpose:** Cloudflare provides the 15-minute clock. GitHub remains the **only** OCI execution environment and keeps all OCI keys. This package does not modify the production OCI workflow or create any OCI instance by itself.

- Target: `gerardli/oci-free-arm-instance` / `.github/workflows/hunt-free-vm.yml`, branch `main`.
- UTC schedule: minutes 08, 23, 38, 53, every hour (96 triggers/day expected). Cron scheduling and GitHub dispatch are not hard real-time guarantees.
- Default is **OFF**: `DISPATCH_ENABLED=false`. Deployment with this default must never dispatch.
- GitHub's existing `concurrency.group: oracle-free-vm-hunt` serializes queued jobs. It is **not** a distributed exactly-once lock: overlap can still cause a second attempt. Remove the original GitHub cron **only after** proving the Cloudflare path works.
- The old workflow's heartbeat explicitly requires `GITHUB_EVENT_NAME == schedule`; after final cutover, adjust this condition to also include `workflow_dispatch`, otherwise Telegram heartbeat stops. Do not change the OCI launch or safety logic.

## Set up once (from the repository root)

1. GitHub: create a **Fine-grained personal access token** at https://github.com/settings/personal-access-tokens/new, restricted to repository `gerardli/oci-free-arm-instance`, **Actions: Read and write** (and default Metadata read). Select a finite expiration. Do **not** paste or commit the PAT into code or chat.
2. In a terminal: `cd cloudflare-scheduler && npx wrangler@latest login && npx wrangler@latest deploy`. This deploys an *inert* Worker with a Cron Trigger because the feature flag defaults to `false`. It has no public HTTP endpoint.
3. In the same directory: `npx wrangler@latest secret put GITHUB_DISPATCH_TOKEN`. Enter the fine-grained PAT at the private CLI prompt, not in a source file. Never put it in the `vars` section.
4. Run tests locally from this directory: `node --test test/worker.test.mjs`.
5. To perform a live acceptance run, change `DISPATCH_ENABLED` in `wrangler.jsonc` to `"true"` and run `npx wrangler@latest deploy`. Watch the next scheduled invocation in Cloudflare Worker Observability (result `accepted`), and confirm a **new** GitHub Actions run with event `workflow_dispatch` and a real OCI result. GitHub API acceptance does not prove execution: **verify both sides**.

## Cut over only after the first confirmed live run

6. On GitHub's production `.github/workflows/hunt-free-vm.yml`, remove **only** the `on.schedule` cron stanza while preserving `workflow_dispatch`. Change the OCI heartbeat guard from `env.get('GITHUB_EVENT_NAME') == 'schedule'` to `env.get('GITHUB_EVENT_NAME') in ('schedule', 'workflow_dispatch')`. Do not otherwise modify OCI SDK logic. This avoids double scheduling while preserving the roughly six-hour heartbeat (heartbeat delivery still depends on GitHub executing inside the UTC time window).
7. Observe several successive 15-minute slots. Compare Cloudflare logs with GitHub Actions timestamps, and confirm there are no unexpected gaps, duplicate successful creates or billing issues.

## Rollback

If Cloudflare stops dispatching, first set `DISPATCH_ENABLED` back to `"false"` and redeploy, then reinstate the GitHub `on.schedule` stanza on the default branch. This keeps GitHub the single active scheduler again. Note that an already accepted GitHub run may still execute; inspect existing OCI instances and storage before retrying.

## Security and limitations

- The Cloudflare token only has GitHub Actions permissions for this one repository; **OCI credentials never leave GitHub Secrets**.
- Missing/invalid token or GitHub HTTP error fails the scheduled invocation visibly. The worker never retries a failed POST automatically, preventing unintended double requests.
- The Worker has no public request trigger (`workers_dev: false`, HTTP handler returns 404). Do not add one without authentication.
- A GitHub PAT expiry stops dispatches. Set a calendar reminder to rotate the token.
- Cloudflare Cron improves schedule delivery relative to the observed GitHub schedule, but neither platform promises exactly 96 executed GitHub runs per day.
- Keep `DISPATCH_ENABLED` disabled until token, budget, and workflow behavior have been verified. If OCI PAYG is enabled later, confirm the entire tenancy's eligible free quotas before changing VM shape.
