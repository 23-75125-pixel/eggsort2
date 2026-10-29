"""Dashboard routes with fake query data; no production database or hardware."""
import unittest
from collections import Counter
from datetime import datetime, timezone
from types import SimpleNamespace as NS
from unittest.mock import patch

from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from auth_test_support import auth as backend, app, db


class Column:
    def __init__(self, name): self.name = name
    def __ge__(self, value): return lambda row: utc(getattr(row, self.name)) >= value
    def __lt__(self, value): return lambda row: utc(getattr(row, self.name)) < value
    def notin_(self, values): return lambda row: getattr(row, self.name) not in values
    def desc(self): return self.name, True
    def asc(self): return self.name, False


def utc(value):
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


class Query:
    def __init__(self, rows=(), group=None, minimum=False):
        self.rows, self.group, self.minimum = list(rows), group, minimum
    def filter(self, *predicates):
        return Query([r for r in self.rows if all(p(r) for p in predicates)], self.group, self.minimum)
    def filter_by(self, **values):
        return Query([r for r in self.rows if all(getattr(r, k) == v for k, v in values.items())], self.group)
    def order_by(self, ordering):
        name, reverse = ordering
        return Query(sorted(self.rows, key=lambda r: getattr(r, name), reverse=reverse), self.group)
    def group_by(self, column): return Query(self.rows, column.name)
    def limit(self, count): return Query(self.rows[:count], self.group)
    def count(self): return len(self.rows)
    def all(self): return list(Counter(getattr(r, self.group) for r in self.rows).items()) if self.group else self.rows
    def first(self): return self.rows[0] if self.rows else None
    def scalar(self): return min((utc(r.sorted_at) for r in self.rows), default=None)
    def delete(self, **kwargs): raise AssertionError("Audit deletion is forbidden")


def record(identifier, stamp, quality="Good", size="Small"):
    row = NS(id=identifier, sorted_at=datetime.fromisoformat(stamp), quality=quality, size=size)
    row.to_dict = lambda: {"id": identifier, "weight_grams": 50 if quality == "Good" else None,
                           "size": size, "sorted_at": row.sorted_at.isoformat()}
    return row


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.context = app.app_context(); self.context.push(); self.addCleanup(self.context.pop)
        self.client = app.test_client()
        self.user = NS(id=10, username="operator", email="operator@example.invalid", display_name="Operator",
                       role="staff", avatar_url=None, is_active=True, password_set=True)
        self.records, self.trays, self.audits = [], [], []
        self.egg = NS(**{name: Column(name) for name in ("id", "size", "quality", "sorted_at")})
        self.egg.query = Query()
        self.tray = NS(egg_size=Column("egg_size"), query=Query())
        self.audit = NS(id=Column("id"), event_type=Column("event_type"), query=Query())
        self.alert = NS(query=Query())
        patches = [patch.object(backend, "EggRecord", self.egg),
                   patch.object(backend, "TraySizeProgress", self.tray),
                   patch.object(backend, "AuditLog", self.audit),
                   patch.object(backend, "TraySizeAlert", self.alert),
                   patch.object(backend, "func", NS(count=lambda c: None, min=lambda c: ("min", c))),
                   patch.object(db.session, "get", side_effect=lambda *args: self.user),
                   patch.object(db.session, "query", side_effect=lambda *args: Query(self.records)),
                   patch.object(backend.ESP32_BRIDGE, "status", return_value={}),
                   patch.object(Engine, "connect", side_effect=AssertionError("Database connections forbidden")),
                   patch.object(db.session, "commit", side_effect=AssertionError("Database writes forbidden")),
                   patch("socket.socket.connect", side_effect=AssertionError("Network forbidden"))]
        for p in patches: p.start(); self.addCleanup(p.stop)
        self.authenticate()

    def authenticate(self, role="staff"):
        self.user.role = role
        with self.client.session_transaction() as session:
            session["user_id"] = self.user.id
            session["_csrf_token"] = "dashboard-test-token"

    def stats(self, **dates):
        self.egg.query = Query(self.records)
        self.tray.query = Query(self.trays)
        self.audit.query = Query(self.audits)
        return self.client.get("/api/dashboard/stats", query_string=dates or {
            "start_date": "2026-10-19", "end_date": "2026-10-29"})

    def test_clear_disabled_preserves_capture_markers_and_all_audits(self):
        self.authenticate("admin")
        marker = NS(event_key="egg-capture:session:42", event_type="egg_sorted")
        self.audits.append(marker); self.audit.query = Query(self.audits)
        response = self.client.post("/api/dashboard/audit-logs/clear", headers={"X-CSRF-Token": "dashboard-test-token"})
        self.assertEqual(response.status_code, 409)
        self.assertIn("No audit records were deleted", response.get_json()["error"])
        self.assertIs(self.audits[0], marker)
        self.assertEqual(marker.event_key, "egg-capture:session:42")
        db.session.commit.assert_not_called()

    def test_clear_admin_authorization_and_csrf_remain(self):
        self.assertEqual(self.client.post("/api/dashboard/audit-logs/clear", headers={"X-CSRF-Token": "dashboard-test-token"}).status_code, 403)
        self.authenticate("admin")
        self.assertEqual(self.client.post("/api/dashboard/audit-logs/clear").status_code, 400)
        with self.client.session_transaction() as session: session.clear()
        self.assertEqual(self.client.post("/api/dashboard/audit-logs/clear").status_code, 401)

    def test_multiple_weeks_have_distinct_actual_dates(self):
        self.records.extend([record(1, "2026-10-19T03:00:00+00:00"), record(2, "2026-10-26T03:00:00+00:00")])
        data = self.stats().get_json()
        self.assertEqual(len(data["daily_trend"]), 11)
        nonzero = {r["date"]: r["count"] for r in data["daily_trend"] if r["count"]}
        self.assertEqual(nonzero, {"2026-10-19": 1, "2026-10-26": 1})

    def test_manila_midnight_and_exclusive_next_day(self):
        self.records.extend([record(i, value) for i, value in enumerate([
            "2026-10-28T15:59:59+00:00", "2026-10-28T16:00:00+00:00",
            "2026-10-29T15:59:59+00:00", "2026-10-29T16:00:00+00:00"], 1)])
        data = self.stats(start_date="2026-10-29", end_date="2026-10-29").get_json()
        self.assertEqual(data["total_sorted"], 2)
        self.assertEqual(data["daily_trend"], [{"date": "2026-10-29", "label": "Thu", "count": 2}])

    def test_naive_historical_timestamps_are_utc(self):
        self.records.append(record(1, "2026-10-28T16:00:00"))
        self.assertEqual(self.stats(date="2026-10-29").get_json()["daily_trend"][0]["count"], 1)

    def test_empty_records_zero_counts_and_zero_days(self):
        data = self.stats().get_json()
        for key in ("total_sorted", "good_count", "non_good_count", "quality_rate", "trays_completed"):
            self.assertEqual(data[key], 0)
        self.assertIsNone(data["latest_record"])
        self.assertTrue(all(row["count"] == 0 for row in data["daily_trend"]))

    def test_rejected_and_mixed_quality_counts_preserve_rules(self):
        self.records.extend([record(1, "2026-10-29T00:00:00+00:00", "Crack", "N/A"),
                             record(2, "2026-10-29T00:00:01+00:00", "Rotten", "N/A")])
        data = self.stats().get_json()
        self.assertEqual((data["total_sorted"], data["quality_rate"], data["non_good_count"]), (2, 0, 2))
        self.assertEqual(data["size_counts"]["N/A"], 2)
        self.records.append(record(3, "2026-10-29T00:00:02+00:00", "Good", "Jumbo"))
        data = self.stats().get_json()
        self.assertEqual(data["quality_rate"], 33.3)
        self.assertEqual(data["quality_counts"], {"Good": 1, "Crack": 1, "Rotten": 1})
        self.assertEqual(sum(data["size_counts"].values()), data["total_sorted"])
        self.assertEqual(sum(r["count"] for r in data["daily_trend"]), data["total_sorted"])

    def test_cumulative_trays_are_not_filtered_or_mutated(self):
        tray = NS(egg_size="Small", completed_milestones=8, to_dict=lambda: {"egg_size": "Small", "completed_milestones": 8})
        self.trays.append(tray)
        data = self.stats().get_json()
        self.assertEqual((data["total_sorted"], data["trays_completed"]), (0, 8))
        self.assertEqual(tray.completed_milestones, 8)

    def test_invalid_extreme_and_reversed_dates_are_400(self):
        cases = [{"date": d} for d in ("bad", "2026-02-30", "2026-2-01", "0001-01-01", "9999-12-31")]
        cases += [{"start_date": "2026-10-30", "end_date": "2026-10-29"},
                  {"end_date": "9999-12-31"}]
        for dates in cases:
            with self.subTest(dates=dates):
                response = self.stats(**dates)
                self.assertEqual(response.status_code, 400)
                self.assertIn("error", response.get_json())

    def test_supported_date_boundaries_do_not_overflow(self):
        for date in ("0002-01-01", "9998-12-31"):
            self.assertEqual(self.stats(date=date).status_code, 200)

    def test_extreme_earliest_record_does_not_overflow_open_range(self):
        self.records.append(record(1, "9999-12-31T23:59:00+00:00"))
        self.assertEqual(self.stats(all_dates="true").status_code, 400)
        self.assertEqual(self.stats(end_date="2026-10-29").status_code, 400)

    def test_long_ranges_are_transparently_sparse(self):
        self.records.append(record(1, "2026-10-29T00:00:00+00:00"))
        data = self.stats(start_date="2000-01-01", end_date="2026-10-29").get_json()
        self.assertTrue(data["daily_trend_sparse"])
        self.assertEqual(len(data["daily_trend"]), 1)

    def test_open_ranges_and_default_last_seven_days(self):
        self.records.append(record(1, "2026-10-26T00:00:00+00:00"))
        for dates in ({"all_dates": "true"}, {"end_date": "2026-10-29"}, {"start_date": "2026-10-26"}):
            self.assertEqual(self.stats(**dates).status_code, 200)
        response = self.client.get("/api/dashboard/stats")
        self.assertEqual(len(response.get_json()["daily_trend"]), 7)

    def test_staff_and_admin_labels_and_audit_visibility(self):
        def audit(identifier, event):
            return NS(id=identifier, event_type=event, to_dict=lambda: {"event_type": event})
        self.audits.extend([audit(1, "login"), audit(2, "egg_sorted"), audit(3, "egg_rejected")])
        for role in ("staff", "admin"):
            self.authenticate(role)
            html = self.client.get("/dashboard").get_data(as_text=True)
            self.assertNotIn("Operations Portal / Admin", html)
            self.assertIn("Operations Portal", html)
            self.assertEqual('id="clear-audit-logs"' in html, role == "admin")
            data = self.stats().get_json()
            self.assertEqual("audit_logs" in data, role == "admin")
            if role == "admin":
                self.assertEqual(data["audit_logs"], [{"event_type": "login"}])
                self.assertRegex(html, r'id="clear-audit-logs"\s+type="button"\s+disabled')

    def test_database_failure_is_generic_503(self):
        with patch.object(Query, "count", side_effect=SQLAlchemyError("sensitive test detail")), patch.object(app.logger, "exception"):
            response = self.stats()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.get_json()["error"], "The database is temporarily unavailable. Please try again.")

    def test_unauthenticated_stats_rejected(self):
        with self.client.session_transaction() as session: session.clear()
        self.assertEqual(self.stats().status_code, 401)


if __name__ == "__main__": unittest.main()
