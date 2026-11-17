"""Regression tests for public collector status; no production database access."""
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from app.main import collector_status


class ScalarResult:
    def __init__(self, value):
        self.value = value

    def scalar(self):
        return self.value

    def scalar_one(self):
        return self.value


class RowsResult:
    def __init__(self, rows):
        self.rows = rows

    def mappings(self):
        return self

    def all(self):
        return self.rows


class FakeConnection:
    def __init__(self, rows, latest_success, failures=0, initialized=True):
        self.rows = rows
        self.latest_success = latest_success
        self.failures = failures
        self.initialized = initialized

    def execute(self, statement):
        sql = str(statement)
        if "to_regclass" in sql:
            return ScalarResult(self.initialized)
        if "DISTINCT ON" in sql:
            return RowsResult(self.rows)
        if "COUNT(*)" in sql:
            return ScalarResult(self.failures)
        if "MAX(finished_at)" in sql:
            return ScalarResult(self.latest_success)
        raise AssertionError("Unexpected query: " + sql)


class FakeEngine:
    def __init__(self, connection):
        self.connection = connection

    @contextmanager
    def connect(self):
        yield self.connection


class CollectorStatusTests(unittest.TestCase):
    def check(self, status, age_hours=1, failures=0, initialized=True):
        now = datetime.now(timezone.utc)
        last = now - timedelta(hours=age_hours) if initialized else None
        rows = [{"job_name": "fixtures", "status": status, "result_count": 14,
                 "finished_at": now - timedelta(minutes=5)}] if initialized else []
        fake = FakeConnection(rows, last, failures, initialized)
        with patch("app.main.engine", FakeEngine(fake)):
            return collector_status()

    def test_normal(self):
        result = self.check("success")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["unresolved_failures"], 0)

    def test_failed_latest(self):
        result = self.check("failed")
        self.assertEqual(result["status"], "degraded")
        self.assertEqual(result["unresolved_failures"], 1)

    def test_skipped_latest(self):
        self.assertEqual(self.check("skipped")["status"], "degraded")

    def test_recovered_failure_history(self):
        result = self.check("success", failures=3)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["failed_last_24h"], 3)

    def test_stale_fixtures(self):
        result = self.check("success", age_hours=16)
        self.assertTrue(result["fixtures_stale"])
        self.assertEqual(result["status"], "degraded")

    def test_uninitialized(self):
        result = self.check("success", initialized=False)
        self.assertEqual(result["status"], "not_initialized")


if __name__ == "__main__":
    unittest.main()