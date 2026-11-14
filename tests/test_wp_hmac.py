"""No external service or database required: python -m unittest tests.test_wp_hmac"""
import asyncio
import os
import time
import unittest
from unittest.mock import patch
from fastapi import HTTPException
from starlette.requests import Request
from app.security.wp_hmac import signature_for, verify_wp_hmac


def request_for(body=b'{"fixture_id":1}', method="POST", path="/v1/internal/hmac-check"):
    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}
    return Request({"type": "http", "method": method, "path": path, "headers": []}, receive)


class HMACTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"SPORTIQ_WP_API_KEY": "test-key", "SPORTIQ_WP_HMAC_SECRET": "test-secret"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.ts = str(int(time.time()))
        self.body = b'{"fixture_id":1}'
        self.sig = signature_for("test-secret", "POST", "/v1/internal/hmac-check", self.ts, self.body)

    def check(self, **kwargs):
        params = {"request": request_for(self.body), "x_api_key": "test-key", "x_timestamp": self.ts, "x_signature": self.sig}
        params.update(kwargs)
        return asyncio.run(verify_wp_hmac(**params))

    def test_valid(self):
        self.assertIsNone(self.check())

    def test_bad_key(self):
        with self.assertRaises(HTTPException) as e: self.check(x_api_key="wrong")
        self.assertEqual(e.exception.status_code, 401)

    def test_expired(self):
        with self.assertRaises(HTTPException) as e: self.check(x_timestamp=str(int(self.ts)-301))
        self.assertEqual(e.exception.status_code, 401)

    def test_bad_timestamp(self):
        with self.assertRaises(HTTPException) as e: self.check(x_timestamp="not-a-number")
        self.assertEqual(e.exception.status_code, 400)

    def test_body_tampering(self):
        with self.assertRaises(HTTPException) as e: self.check(request=request_for(b'{"fixture_id":2}'))
        self.assertEqual(e.exception.status_code, 403)

    def test_path_tampering(self):
        with self.assertRaises(HTTPException) as e: self.check(request=request_for(self.body, path="/v1/internal/other"))
        self.assertEqual(e.exception.status_code, 403)

    def test_unconfigured(self):
        with patch.dict(os.environ, {"SPORTIQ_WP_HMAC_SECRET": ""}):
            with self.assertRaises(HTTPException) as e: self.check()
        self.assertEqual(e.exception.status_code, 503)


if __name__ == "__main__":
    unittest.main()
