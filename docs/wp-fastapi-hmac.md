# WordPress ↔ FastAPI HMAC (opt-in)

This change adds `app/security/wp_hmac.py` without changing any existing public API or WordPress plugin. No secrets are committed.

## Signature contract
- Headers: `X-API-Key`, `X-Timestamp` (Unix seconds), `X-Signature` (lowercase hex SHA-256)
- Signed bytes: `METHOD + "\\n" + PATH + "\\n" + TIMESTAMP + "\\n"` (UTF-8) followed by the **exact request body bytes**
- Query strings are not included: use only on endpoints where the query is not security-sensitive, or extend the protocol to include canonical query bytes on both sides.
- Maximum body: 64 KiB. Timestamp skew: ±300 seconds.
- Render API secrets: `SPORTIQ_WP_API_KEY`, `SPORTIQ_WP_HMAC_SECRET`; configure only when enabling private routes. Keep both out of browser JavaScript and source control.
- Add `Depends(verify_wp_hmac)` to **new private POST routes only** after WP client implementation. Existing GET endpoints stay public.
- Example WordPress PHP signer:
```php
$timestamp = (string) time();
$body = wp_json_encode(array('fixture_id' => 123));
$path = '/v1/internal/hmac-check';
$message = "POST\n" . $path . "\n" . $timestamp . "\n" . $body;
$signature = hash_hmac('sha256', $message, getenv('SPORTIQ_WP_HMAC_SECRET'));
```
- WordPress secrets must be stored server-side in secure configuration, not frontend code or plugin source.
- **Limitation:** timestamps alone do not stop replay within 5 minutes. Before enabling sensitive/mutating operations, add a nonce and shared atomic replay store (e.g. Redis or PostgreSQL) and sign the nonce.
- Never use this demo module as a substitute for authorization or input validation.
- Test: `python -m unittest tests.test_wp_hmac`; test requires project dependencies.
