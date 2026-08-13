import unittest
from datetime import datetime, time
from zoneinfo import ZoneInfo

from src.policies import (
    AirlineCode,
    AirlineRule,
    AirlineRuleSet,
    Itinerary,
    NotificationPolicy,
    NotificationStrategy,
    PriceBasis,
    QuotaMode,
    QuotaPolicy,
    ScheduleMode,
    SchedulePolicy,
    TimeWindow,
    normalize_airline,
)


TAIPEI = ZoneInfo("Asia/Taipei")


class QuotaPolicyTests(unittest.TestCase):
    def test_modes_have_explicit_request_caps(self):
        self.assertEqual(QuotaMode.LEAN.requests_per_run, 1)
        self.assertEqual(QuotaMode.STANDARD.requests_per_run, 2)
        self.assertEqual(QuotaMode.FULL.requests_per_run, 5)

    def test_daily_monthly_forecast_and_remaining(self):
        policy = QuotaPolicy(mode="standard", monthly_limit=250)
        self.assertEqual(policy.estimate_daily(2, monitor_count=3), 12)
        self.assertEqual(policy.estimate_monthly(2, monitor_count=3, days=30), 360)
        forecast = policy.forecast(2, 3, used_requests=20, days=30)
        self.assertEqual(forecast.daily_requests, 12)
        self.assertEqual(forecast.monthly_requests, 360)
        self.assertEqual(forecast.remaining, 230)
        self.assertEqual(forecast.projected_remaining, 0)
        self.assertFalse(forecast.fits_monthly_limit)

    def test_full_mode_exposes_current_worst_case(self):
        policy = QuotaPolicy()
        self.assertEqual(policy.mode, QuotaMode.FULL)
        self.assertEqual(policy.estimate_monthly(2, 1, days=31), 310)

    def test_guard_preserves_reserve_and_handles_unknown_limit(self):
        policy = QuotaPolicy(mode="full", monthly_limit=250, reserve_requests=10)
        allowed = policy.guard(used_requests=230, requested=5)
        blocked = policy.guard(used_requests=238, requested=5)
        exhausted = policy.guard(used_requests=250, requested=1)
        self.assertTrue(allowed.allowed)
        self.assertEqual(allowed.remaining_after, 15)
        self.assertFalse(blocked.allowed)
        self.assertEqual(blocked.reason, "quota_reserve")
        self.assertEqual(exhausted.reason, "monthly_quota_exhausted")

        unknown = QuotaPolicy(monthly_limit=0).guard(99)
        self.assertTrue(unknown.allowed)
        self.assertIsNone(unknown.remaining)
        self.assertEqual(unknown.reason, "unknown_limit")

    def test_quota_policy_dict_compatibility(self):
        legacy = QuotaPolicy.from_dict({})
        self.assertEqual(legacy.mode, QuotaMode.FULL)
        loaded = QuotaPolicy.from_dict(
            {"quota_mode": "lean", "monthly_limit": "100", "reserve": "5"}
        )
        self.assertEqual(loaded.to_dict(), {
            "mode": "lean",
            "monthly_limit": 100,
            "reserve_requests": 5,
        })


class AirlinePolicyTests(unittest.TestCase):
    @staticmethod
    def eva_round_trip(**overrides):
        value = {
            "price": 18000,
            "adults": 2,
            "currency": "twd",
            "airline": "EVA Air",
            "flight_number": "BR 130",
            "departure_time": "2026-09-01 08:30",
            "arrival_time": "2026-09-01 12:00",
            "stops": 0,
            "return_flight": {
                "airline": "長榮航空",
                "flight_number": "BR 129",
                "departure_time": "2026-09-08 16:10",
                "arrival_time": "2026-09-08 18:20",
                "stops": 0,
            },
        }
        value.update(overrides)
        return Itinerary.from_dict(value)

    def test_airline_aliases_cover_eva_ci_and_jx(self):
        self.assertEqual(normalize_airline("長榮航空"), AirlineCode.EVA)
        self.assertEqual(normalize_airline("華航"), AirlineCode.CHINA_AIRLINES)
        self.assertEqual(normalize_airline("STARLUX Airlines"), AirlineCode.STARLUX)
        self.assertEqual(normalize_airline("", "BR130"), AirlineCode.EVA)

    def test_rule_matches_round_trip_windows_and_per_person_target(self):
        rule = AirlineRule(
            airline="EVA",
            target_price=9500,
            direct_only=True,
            max_stops=0,
            outbound_window=TimeWindow(time(6), time(12)),
            return_window={"start": "14:00", "end": "20:00"},
            price_basis="per_person",
        )
        result = rule.evaluate(self.eva_round_trip())
        self.assertTrue(result.eligible)
        self.assertTrue(result.target_hit)
        self.assertEqual(result.evaluated_price, 9000)

    def test_rule_reports_structural_mismatch_reasons(self):
        trip = self.eva_round_trip(
            stops=1,
            return_flight={
                "airline": "China Airlines",
                "departure_time": "2026-09-08 23:10",
                "stops": 2,
            },
        )
        rule = AirlineRule.from_dict(
            {
                "airline": "EVA",
                "target_price": 20000,
                "direct_only": True,
                "max_stops": 1,
                "return_window": {"start": "12:00", "end": "20:00"},
            }
        )
        result = rule.evaluate(trip)
        self.assertFalse(result.eligible)
        self.assertFalse(result.target_hit)
        self.assertIn("return_airline", result.reasons)
        self.assertIn("direct_only", result.reasons)
        self.assertIn("max_stops", result.reasons)
        self.assertIn("return_window", result.reasons)

    def test_same_airline_roundtrip_rule_rejects_missing_return_leg(self):
        trip = Itinerary.from_dict(
            {
                "price": 12000,
                "airline": "EVA Air",
                "flight_number": "BR130",
                "stops": 0,
            }
        )
        result = AirlineRule(
            airline="EVA",
            target_price=15000,
            require_same_airline_roundtrip=True,
        ).evaluate(trip)
        self.assertFalse(result.eligible)
        self.assertFalse(result.target_hit)
        self.assertIn("return_airline", result.reasons)

    def test_time_window_supports_overnight_ranges(self):
        window = TimeWindow.from_dict(["22:00", "06:00"])
        self.assertTrue(window.contains("23:30"))
        self.assertTrue(window.contains("05:59"))
        self.assertFalse(window.contains("12:00"))

    def test_quiet_hours_returns_exact_next_allowed_time(self):
        policy = NotificationPolicy.from_dict(
            {"quiet_hours": {"start": "22:00", "end": "07:00"}}
        )
        zone = ZoneInfo("Asia/Taipei")
        late = datetime(2026, 8, 12, 23, 0, tzinfo=zone)
        early = datetime(2026, 8, 13, 6, 0, tzinfo=zone)
        self.assertEqual(
            policy.next_allowed_time(late),
            datetime(2026, 8, 13, 7, 0, tzinfo=zone),
        )
        self.assertEqual(
            policy.next_allowed_time(early),
            datetime(2026, 8, 13, 7, 0, tzinfo=zone),
        )

    def test_rule_set_accepts_carrier_keyed_legacy_mapping(self):
        rules = AirlineRuleSet.from_dict(
            {
                "EVA": {"target": 17000},
                "CI": {"target_price": 18000, "price_type": "total"},
                "JX": 19000,
            }
        )
        self.assertEqual(len(rules.rules), 3)
        self.assertEqual(
            {rule.airline for rule in rules.rules},
            {AirlineCode.EVA, AirlineCode.CHINA_AIRLINES, AirlineCode.STARLUX},
        )
        round_trip = AirlineRuleSet.from_dict(rules.to_dict())
        self.assertEqual(round_trip.to_dict(), rules.to_dict())

    def test_itinerary_and_rule_dict_round_trip(self):
        itinerary = self.eva_round_trip()
        self.assertEqual(Itinerary.from_dict(itinerary.to_dict()), itinerary)
        rule = AirlineRule(
            AirlineCode.EVA,
            20000,
            max_stops=1,
            outbound_window=TimeWindow(time(7), time(13)),
            price_basis=PriceBasis.TOTAL,
        )
        self.assertEqual(AirlineRule.from_dict(rule.to_dict()), rule)


class SchedulePolicyTests(unittest.TestCase):
    def test_fixed_times_next_run_uses_taipei_timezone(self):
        policy = SchedulePolicy.from_dict({})
        morning = datetime(2026, 8, 12, 10, 30, tzinfo=TAIPEI)
        evening = datetime(2026, 8, 12, 13, 0, tzinfo=TAIPEI)
        self.assertEqual(
            policy.next_run(morning), datetime(2026, 8, 12, 12, 0, tzinfo=TAIPEI)
        )
        self.assertEqual(
            policy.next_run(evening), datetime(2026, 8, 13, 0, 0, tzinfo=TAIPEI)
        )
        self.assertEqual(policy.runs_per_day, 2.0)

    def test_daily_and_interval_next_run(self):
        daily = SchedulePolicy(mode="daily", daily_time=time(9))
        current = datetime(2026, 8, 12, 10, 0, tzinfo=TAIPEI)
        self.assertEqual(
            daily.next_run(current), datetime(2026, 8, 13, 9, 0, tzinfo=TAIPEI)
        )

        interval = SchedulePolicy(mode="interval", interval_minutes=15)
        last = datetime(2026, 8, 12, 10, 0, tzinfo=TAIPEI)
        now = datetime(2026, 8, 12, 10, 31, tzinfo=TAIPEI)
        self.assertEqual(
            interval.next_run(now, last_run=last),
            datetime(2026, 8, 12, 10, 45, tzinfo=TAIPEI),
        )
        self.assertEqual(interval.runs_per_day, 96.0)

    def test_schedule_dict_aliases_and_round_trip(self):
        policy = SchedulePolicy.from_dict(
            {"schedule_type": "fixed_times", "times": "06:30,18:30"}
        )
        self.assertEqual(policy.fixed_times, (time(6, 30), time(18, 30)))
        self.assertEqual(SchedulePolicy.from_dict(policy.to_dict()), policy)


class NotificationPolicyTests(unittest.TestCase):
    def test_backward_default_is_always(self):
        policy = NotificationPolicy.from_dict({})
        decision = policy.evaluate(current_price=20000, target_price=10000)
        self.assertEqual(policy.strategy, NotificationStrategy.ALWAYS)
        self.assertTrue(decision.send_now)

    def test_target_only_and_price_drop(self):
        target = NotificationPolicy(strategy="target_only")
        self.assertFalse(
            target.evaluate(current_price=12000, target_price=10000).send_now
        )
        self.assertTrue(
            target.evaluate(current_price=9000, target_price=10000).send_now
        )
        self.assertTrue(
            target.evaluate(
                current_price=12000,
                target_price=10000,
                target_hit=True,
            ).send_now
        )

        drop = NotificationPolicy(strategy="price_drop")
        self.assertFalse(drop.evaluate(current_price=9000).send_now)
        self.assertTrue(
            drop.evaluate(current_price=9000, previous_price=10000).send_now
        )

    def test_digest_queues_until_due(self):
        policy = NotificationPolicy(strategy="digest")
        queued = policy.evaluate(current_price=10000)
        due = policy.evaluate(current_price=10000, digest_due=True)
        self.assertFalse(queued.send_now)
        self.assertTrue(queued.queue_digest)
        self.assertTrue(due.send_now)
        self.assertFalse(due.queue_digest)

    def test_quiet_hours_defer_events_across_midnight(self):
        policy = NotificationPolicy.from_dict(
            {
                "notify_strategy": "always",
                "quiet_start": "22:00",
                "quiet_end": "07:00",
            }
        )
        late = datetime(2026, 8, 12, 23, 0, tzinfo=TAIPEI)
        morning = datetime(2026, 8, 13, 8, 0, tzinfo=TAIPEI)
        deferred = policy.evaluate(current_price=10000, now=late)
        sent = policy.evaluate(current_price=10000, now=morning)
        self.assertTrue(deferred.quiet)
        self.assertTrue(deferred.queue_digest)
        self.assertFalse(deferred.send_now)
        self.assertTrue(sent.send_now)
        self.assertEqual(NotificationPolicy.from_dict(policy.to_dict()), policy)


if __name__ == "__main__":
    unittest.main()
