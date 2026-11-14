# SportIQ HMAC Bridge deployment status

The following source files were committed directly to `main` without running tests, per user request:
- `app/security/wp_bridge_router.py`: POST `/v1/internal/hmac-check`, requires `verify_wp_hmac`, accepts positive `fixture_id`; no database changes or prediction execution.
- `app/main.py`: registers the new router.
- `wordpress/sportiq-secure-bridge/sportiq-secure-bridge.php`: server-side HMAC client class. No public shortcode or frontend endpoint is registered.

## Required secrets (not committed)
- Render web service: `SPORTIQ_WP_API_KEY`, `SPORTIQ_WP_HMAC_SECRET`.
- WordPress server-side configuration: `SPORTIQ_WP_API_KEY`, `SPORTIQ_WP_HMAC_SECRET` with matching values. Do not put secrets in browser JS, pages, GitHub, or chat.
- WordPress bridge plugin must be installed and activated separately; source commit does not install it.

## Status and limits
- Source code committed. No test execution or live request was performed.
- Render auto-deploy may run after a main-branch commit; this has not been verified.
- With no Render secrets the new endpoint returns 503; existing public endpoints are unchanged.
- The endpoint is only an authentication connectivity check, not a prediction endpoint.
- Replay protection is only a 300-second timestamp window; do not use for sensitive mutations until nonce/replay store is added.
