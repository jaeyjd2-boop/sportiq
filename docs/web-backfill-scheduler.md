# Optional FastAPI lifespan backfill scheduler

Web backfill is **disabled by default**. The existing Render background worker and its forecast collection/evaluation jobs are unchanged.

The FastAPI lifespan registers two jobs only when `WEB_BACKFILL_ENABLED=true` on the **web service**:
- Every 6 hours: last 7 days, one 7-day window
- 04:00 Asia/Seoul daily: last 30 days, 7-day windows

Uses existing `app.backfill.run_backfill` with `write=True`, football-data.org competition code from `WEB_BACKFILL_COMPETITION` (default `PL`), and the existing `FOOTBALL_DATA_TOKEN` / PostgreSQL connection. There is no public or administrator trigger endpoint in this change.

The job executes in a thread, keeping synchronous HTTP/SQL work off the FastAPI event loop. A dedicated PostgreSQL connection holds a **session-level advisory lock** across the job, preventing another enabled web process using the same lock ID from running another web backfill. No transaction is held during API calls. This does **not** coordinate with the original worker's fixture collection or other independent jobs. Failed windows are reported as failed; the original backfill may still commit successful windows. Job results are written to `collector_runs` when available.

**Deployment:** Keep the flag false initially. Check Render deploy and health, provider quota, and monitoring before explicitly changing the web service flag to true. Do not stop the existing Worker merely because the web backfill is enabled: forecast snapshot/evaluation and other collection tasks still run there. With multiple web replicas the scheduler is instantiated per replica, but the advisory lock prevents simultaneous web backfills, not duplicate sequential schedules. Web restarts and shutdowns may interrupt running jobs. No tests or production execution were performed as part of this code change.
