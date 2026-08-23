"""Pure domain policies for monitoring, quota, scheduling, and notifications.

This module intentionally has no Streamlit, database, or network dependencies.
It accepts plain dictionaries at its boundaries so the UI and persistence layers
can adopt the policies incrementally without breaking legacy monitor records.
"""
from __future__ import annotations

import calendar
import math
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from enum import Enum
from typing import Any, Iterable, Mapping, Optional, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

TAIPEI_TIMEZONE = "Asia/Taipei"


class QuotaMode(str, Enum):
    """Maximum provider requests used by one monitor run."""

    LEAN = "lean"
    STANDARD = "standard"
    FULL = "full"

    @property
    def requests_per_run(self) -> int:
        return {
            QuotaMode.LEAN: 1,
            QuotaMode.STANDARD: 2,
            QuotaMode.FULL: 5,
        }[self]


@dataclass(frozen=True)
class QuotaGuardResult:
    allowed: bool
    requested: int
    remaining: Optional[int]
    remaining_after: Optional[int]
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "requested": self.requested,
            "remaining": self.remaining,
            "remaining_after": self.remaining_after,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class QuotaForecast:
    daily_requests: int
    monthly_requests: int
    remaining: Optional[int]
    projected_remaining: Optional[int]
    fits_monthly_limit: Optional[bool]

    def to_dict(self) -> dict[str, Any]:
        return {
            "daily_requests": self.daily_requests,
            "monthly_requests": self.monthly_requests,
            "remaining": self.remaining,
            "projected_remaining": self.projected_remaining,
            "fits_monthly_limit": self.fits_monthly_limit,
        }


@dataclass(frozen=True)
class QuotaPolicy:
    """Request-budget policy.

    ``FULL`` is the compatibility default because the current scheduled report
    may fetch the top itinerary plus three Taiwanese-carrier return options.
    A limit of zero means the provider limit is unknown, not that all calls are
    blocked.
    """

    mode: QuotaMode = QuotaMode.FULL
    monthly_limit: int = 250
    reserve_requests: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "mode", _enum_value(QuotaMode, self.mode, QuotaMode.FULL))
        if self.monthly_limit < 0:
            raise ValueError("monthly_limit cannot be negative")
        if self.reserve_requests < 0:
            raise ValueError("reserve_requests cannot be negative")

    @property
    def max_requests_per_run(self) -> int:
        return self.mode.requests_per_run

    def estimate_daily(self, runs_per_day: float, monitor_count: int = 1) -> int:
        if runs_per_day < 0 or monitor_count < 0:
            raise ValueError("runs_per_day and monitor_count cannot be negative")
        return math.ceil(runs_per_day * monitor_count * self.max_requests_per_run)

    def estimate_monthly(
        self,
        runs_per_day: float,
        monitor_count: int = 1,
        *,
        days: Optional[int] = None,
        now: Optional[datetime] = None,
    ) -> int:
        if days is None:
            current = now or datetime.now()
            days = calendar.monthrange(current.year, current.month)[1]
        if days < 0:
            raise ValueError("days cannot be negative")
        return math.ceil(
            runs_per_day * monitor_count * self.max_requests_per_run * days
        )

    def remaining(self, used_requests: int) -> Optional[int]:
        if used_requests < 0:
            raise ValueError("used_requests cannot be negative")
        if self.monthly_limit == 0:
            return None
        return max(0, self.monthly_limit - used_requests)

    def guard(self, used_requests: int, requested: Optional[int] = None) -> QuotaGuardResult:
        """Decide whether a request batch can run without spending the reserve."""
        if used_requests < 0:
            raise ValueError("used_requests cannot be negative")
        requested_count = self.max_requests_per_run if requested is None else requested
        if requested_count < 0:
            raise ValueError("requested cannot be negative")
        if self.monthly_limit == 0:
            return QuotaGuardResult(True, requested_count, None, None, "unknown_limit")

        remaining = self.remaining(used_requests)
        assert remaining is not None
        spendable = max(0, remaining - self.reserve_requests)
        allowed = requested_count <= spendable
        if allowed:
            reason = "allowed"
        elif remaining == 0:
            reason = "monthly_quota_exhausted"
        else:
            reason = "quota_reserve"
        return QuotaGuardResult(
            allowed=allowed,
            requested=requested_count,
            remaining=remaining,
            remaining_after=max(0, remaining - requested_count) if allowed else remaining,
            reason=reason,
        )

    def forecast(
        self,
        runs_per_day: float,
        monitor_count: int,
        used_requests: int = 0,
        *,
        days: Optional[int] = None,
        now: Optional[datetime] = None,
    ) -> QuotaForecast:
        daily = self.estimate_daily(runs_per_day, monitor_count)
        monthly = self.estimate_monthly(
            runs_per_day, monitor_count, days=days, now=now
        )
        remaining = self.remaining(used_requests)
        if remaining is None:
            projected_remaining = None
            fits = None
        else:
            projected_remaining = max(0, remaining - monthly)
            fits = monthly <= max(0, remaining - self.reserve_requests)
        return QuotaForecast(daily, monthly, remaining, projected_remaining, fits)

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode.value,
            "monthly_limit": self.monthly_limit,
            "reserve_requests": self.reserve_requests,
        }

    @classmethod
    def from_dict(cls, value: Optional[Mapping[str, Any]]) -> "QuotaPolicy":
        data = dict(value or {})
        return cls(
            mode=data.get("mode", data.get("quota_mode", QuotaMode.FULL.value)),
            monthly_limit=_as_int(data.get("monthly_limit", 250), 250),
            reserve_requests=_as_int(data.get("reserve_requests", data.get("reserve", 0)), 0),
        )


class AirlineCode(str, Enum):
    EVA = "EVA"
    CHINA_AIRLINES = "CI"
    STARLUX = "JX"


class PriceBasis(str, Enum):
    TOTAL = "total"
    PER_PERSON = "per_person"


@dataclass(frozen=True)
class TimeWindow:
    """A local wall-clock window; overnight windows are supported."""

    start: time
    end: time

    def __post_init__(self) -> None:
        object.__setattr__(self, "start", _clock(self.start))
        object.__setattr__(self, "end", _clock(self.end))

    def contains(self, value: Any) -> bool:
        current = _clock_or_none(value)
        if current is None:
            return False
        if self.start == self.end:
            return True
        if self.start < self.end:
            return self.start <= current < self.end
        return current >= self.start or current < self.end

    def to_dict(self) -> dict[str, str]:
        return {"start": _format_clock(self.start), "end": _format_clock(self.end)}

    @classmethod
    def from_dict(cls, value: Any) -> Optional["TimeWindow"]:
        if value is None or value == "":
            return None
        if isinstance(value, TimeWindow):
            return value
        if isinstance(value, Mapping):
            return cls(_clock(value.get("start")), _clock(value.get("end")))
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes)) and len(value) == 2:
            return cls(_clock(value[0]), _clock(value[1]))
        raise ValueError("time window must contain start and end")


@dataclass(frozen=True)
class FlightLeg:
    airline: str
    departure_time: Optional[time] = None
    arrival_time: Optional[time] = None
    stops: int = 0
    flight_number: str = ""

    def __post_init__(self) -> None:
        if self.stops < 0:
            raise ValueError("stops cannot be negative")
        object.__setattr__(self, "departure_time", _clock_or_none(self.departure_time))
        object.__setattr__(self, "arrival_time", _clock_or_none(self.arrival_time))

    @property
    def airline_code(self) -> Optional[AirlineCode]:
        return normalize_airline(self.airline, self.flight_number)

    def to_dict(self) -> dict[str, Any]:
        return {
            "airline": self.airline,
            "departure_time": _format_clock(self.departure_time) if self.departure_time else None,
            "arrival_time": _format_clock(self.arrival_time) if self.arrival_time else None,
            "stops": self.stops,
            "flight_number": self.flight_number,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "FlightLeg":
        departure = value.get("departure_time")
        arrival = value.get("arrival_time")
        departure_airport = value.get("departure_airport")
        arrival_airport = value.get("arrival_airport")
        if departure is None and isinstance(departure_airport, Mapping):
            departure = departure_airport.get("time")
        if arrival is None and isinstance(arrival_airport, Mapping):
            arrival = arrival_airport.get("time")
        return cls(
            airline=str(value.get("airline") or value.get("airline_name") or ""),
            departure_time=_clock_or_none(departure),
            arrival_time=_clock_or_none(arrival),
            stops=max(0, _as_int(value.get("stops", value.get("transfers", 0)), 0)),
            flight_number=str(value.get("flight_number") or value.get("number") or ""),
        )


@dataclass(frozen=True)
class Itinerary:
    total_price: float
    outbound: FlightLeg
    returning: Optional[FlightLeg] = None
    adults: int = 1
    currency: str = "TWD"

    def __post_init__(self) -> None:
        if self.total_price < 0:
            raise ValueError("total_price cannot be negative")
        if self.adults < 1:
            raise ValueError("adults must be at least one")

    @property
    def price_per_person(self) -> float:
        return self.total_price / self.adults

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_price": self.total_price,
            "outbound": self.outbound.to_dict(),
            "returning": self.returning.to_dict() if self.returning else None,
            "adults": self.adults,
            "currency": self.currency,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "Itinerary":
        outbound_raw = value.get("outbound") or value.get("outbound_flight") or value
        returning_raw = (
            value.get("returning")
            or value.get("return_flight")
            or value.get("return")
        )
        if not isinstance(outbound_raw, Mapping):
            raise ValueError("itinerary outbound leg is required")
        if returning_raw is not None and not isinstance(returning_raw, Mapping):
            raise ValueError("itinerary return leg must be a mapping")
        raw_price = value.get("total_price", value.get("price"))
        if raw_price is None:
            raise ValueError("itinerary price is required")
        return cls(
            total_price=float(raw_price),
            outbound=FlightLeg.from_dict(outbound_raw),
            returning=FlightLeg.from_dict(returning_raw) if returning_raw else None,
            adults=max(1, _as_int(value.get("adults", 1), 1)),
            currency=str(value.get("currency") or "TWD").upper(),
        )


@dataclass(frozen=True)
class AirlineMatch:
    airline: AirlineCode
    eligible: bool
    target_hit: bool
    evaluated_price: float
    reasons: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "airline": self.airline.value,
            "eligible": self.eligible,
            "target_hit": self.target_hit,
            "evaluated_price": self.evaluated_price,
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True)
class AirlineRule:
    airline: AirlineCode
    target_price: Optional[float] = None
    direct_only: bool = False
    max_stops: Optional[int] = None
    outbound_window: Optional[TimeWindow] = None
    return_window: Optional[TimeWindow] = None
    price_basis: PriceBasis = PriceBasis.TOTAL
    require_same_airline_roundtrip: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "airline", normalize_airline_required(self.airline))
        object.__setattr__(
            self, "price_basis", _enum_value(PriceBasis, self.price_basis, PriceBasis.TOTAL)
        )
        object.__setattr__(self, "outbound_window", TimeWindow.from_dict(self.outbound_window))
        object.__setattr__(self, "return_window", TimeWindow.from_dict(self.return_window))
        if self.target_price is not None and self.target_price <= 0:
            raise ValueError("target_price must be positive")
        if self.max_stops is not None and self.max_stops < 0:
            raise ValueError("max_stops cannot be negative")

    def evaluate(self, itinerary: Itinerary | Mapping[str, Any]) -> AirlineMatch:
        trip = itinerary if isinstance(itinerary, Itinerary) else Itinerary.from_dict(itinerary)
        reasons: list[str] = []
        if trip.outbound.airline_code != self.airline:
            reasons.append("outbound_airline")
        if (
            self.require_same_airline_roundtrip
            and (
                trip.returning is None
                or trip.returning.airline_code != self.airline
            )
        ):
            reasons.append("return_airline")

        legs = [trip.outbound] + ([trip.returning] if trip.returning else [])
        if self.direct_only and any(leg.stops != 0 for leg in legs):
            reasons.append("direct_only")
        if self.max_stops is not None and any(leg.stops > self.max_stops for leg in legs):
            reasons.append("max_stops")
        if self.outbound_window and not self.outbound_window.contains(trip.outbound.departure_time):
            reasons.append("outbound_window")
        if self.return_window:
            if trip.returning is None or not self.return_window.contains(
                trip.returning.departure_time
            ):
                reasons.append("return_window")

        evaluated_price = (
            trip.price_per_person
            if self.price_basis == PriceBasis.PER_PERSON
            else trip.total_price
        )
        eligible = not reasons
        target_hit = bool(
            eligible
            and self.target_price is not None
            and evaluated_price <= self.target_price
        )
        return AirlineMatch(
            airline=self.airline,
            eligible=eligible,
            target_hit=target_hit,
            evaluated_price=evaluated_price,
            reasons=tuple(reasons),
        )

    def matches(self, itinerary: Itinerary | Mapping[str, Any]) -> bool:
        """Return whether the itinerary satisfies structural constraints."""
        return self.evaluate(itinerary).eligible

    def is_target_hit(self, itinerary: Itinerary | Mapping[str, Any]) -> bool:
        return self.evaluate(itinerary).target_hit

    def to_dict(self) -> dict[str, Any]:
        return {
            "airline": self.airline.value,
            "target_price": self.target_price,
            "direct_only": self.direct_only,
            "max_stops": self.max_stops,
            "outbound_window": self.outbound_window.to_dict() if self.outbound_window else None,
            "return_window": self.return_window.to_dict() if self.return_window else None,
            "price_basis": self.price_basis.value,
            "require_same_airline_roundtrip": self.require_same_airline_roundtrip,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any], *, airline: Any = None) -> "AirlineRule":
        return cls(
            airline=airline or value.get("airline") or value.get("carrier"),
            target_price=_optional_float(value.get("target_price", value.get("target"))),
            direct_only=bool(value.get("direct_only", value.get("only_direct", False))),
            max_stops=_optional_int(value.get("max_stops", value.get("max_transfers"))),
            outbound_window=TimeWindow.from_dict(
                value.get("outbound_window", value.get("departure_window"))
            ),
            return_window=TimeWindow.from_dict(
                value.get("return_window", value.get("return_departure_window"))
            ),
            price_basis=value.get("price_basis", value.get("price_type", PriceBasis.TOTAL.value)),
            require_same_airline_roundtrip=bool(
                value.get("require_same_airline_roundtrip", True)
            ),
        )


@dataclass(frozen=True)
class AirlineRuleSet:
    rules: tuple[AirlineRule, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "rules",
            tuple(rule if isinstance(rule, AirlineRule) else AirlineRule.from_dict(rule) for rule in self.rules),
        )

    def evaluate(self, itinerary: Itinerary | Mapping[str, Any]) -> tuple[AirlineMatch, ...]:
        return tuple(rule.evaluate(itinerary) for rule in self.rules)

    def target_hits(
        self, itineraries: Iterable[Itinerary | Mapping[str, Any]]
    ) -> tuple[AirlineMatch, ...]:
        hits: list[AirlineMatch] = []
        for itinerary in itineraries:
            for rule in self.rules:
                result = rule.evaluate(itinerary)
                if result.target_hit:
                    hits.append(result)
        return tuple(hits)

    def to_dict(self) -> dict[str, Any]:
        return {"rules": [rule.to_dict() for rule in self.rules]}

    @classmethod
    def from_dict(cls, value: Any) -> "AirlineRuleSet":
        if not value:
            return cls()
        raw = value.get("rules", value) if isinstance(value, Mapping) else value
        rules: list[AirlineRule] = []
        if isinstance(raw, Mapping):
            for airline, policy in raw.items():
                if policy is None:
                    continue
                if not isinstance(policy, Mapping):
                    policy = {"target_price": policy}
                rules.append(AirlineRule.from_dict(policy, airline=airline))
        elif isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
            rules = [AirlineRule.from_dict(item) for item in raw]
        else:
            raise ValueError("airline rules must be a mapping or sequence")
        return cls(tuple(rules))


class ScheduleMode(str, Enum):
    FIXED_TIMES = "fixed_times"
    DAILY = "daily"
    INTERVAL = "interval"


@dataclass(frozen=True)
class SchedulePolicy:
    """Per-monitor schedule policy using an IANA timezone."""

    mode: ScheduleMode = ScheduleMode.FIXED_TIMES
    fixed_times: tuple[time, ...] = (time(0, 0), time(12, 0))
    daily_time: time = time(0, 0)
    interval_minutes: int = 720
    timezone: str = TAIPEI_TIMEZONE

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "mode", _enum_value(ScheduleMode, self.mode, ScheduleMode.FIXED_TIMES)
        )
        normalized = tuple(sorted({_clock(item) for item in self.fixed_times}))
        if self.mode == ScheduleMode.FIXED_TIMES and not normalized:
            raise ValueError("fixed_times schedule requires at least one time")
        object.__setattr__(self, "fixed_times", normalized)
        object.__setattr__(self, "daily_time", _clock(self.daily_time))
        if self.interval_minutes < 5:
            raise ValueError("interval_minutes must be at least five")
        try:
            ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"unknown timezone: {self.timezone}") from exc

    @property
    def timezone_info(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def runs_per_day(self) -> float:
        if self.mode == ScheduleMode.FIXED_TIMES:
            return float(len(self.fixed_times))
        if self.mode == ScheduleMode.DAILY:
            return 1.0
        return 1440.0 / self.interval_minutes

    def next_run(
        self,
        now: Optional[datetime] = None,
        *,
        last_run: Optional[datetime] = None,
    ) -> datetime:
        zone = self.timezone_info
        current = _zoned(now or datetime.now(zone), zone)
        if self.mode in (ScheduleMode.FIXED_TIMES, ScheduleMode.DAILY):
            clocks = self.fixed_times if self.mode == ScheduleMode.FIXED_TIMES else (self.daily_time,)
            candidates = [
                datetime.combine(current.date() + timedelta(days=offset), clock, tzinfo=zone)
                for offset in (0, 1)
                for clock in clocks
            ]
            return min(candidate for candidate in candidates if candidate > current)

        interval = timedelta(minutes=self.interval_minutes)
        if last_run is None:
            return current + interval
        anchor = _zoned(last_run, zone)
        candidate = anchor + interval
        if candidate > current:
            return candidate
        elapsed = current - anchor
        steps = math.floor(elapsed / interval) + 1
        return anchor + interval * steps

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode.value,
            "fixed_times": [_format_clock(item) for item in self.fixed_times],
            "daily_time": _format_clock(self.daily_time),
            "interval_minutes": self.interval_minutes,
            "timezone": self.timezone,
        }

    @classmethod
    def from_dict(cls, value: Optional[Mapping[str, Any]]) -> "SchedulePolicy":
        data = dict(value or {})
        raw_times = data.get("fixed_times", data.get("times", ("00:00", "12:00")))
        if isinstance(raw_times, str):
            raw_times = [item.strip() for item in raw_times.split(",") if item.strip()]
        return cls(
            mode=data.get("mode", data.get("schedule_type", ScheduleMode.FIXED_TIMES.value)),
            fixed_times=tuple(_clock(item) for item in raw_times),
            daily_time=_clock(data.get("daily_time", data.get("time", "00:00"))),
            interval_minutes=_as_int(
                data.get("interval_minutes", data.get("minutes", 720)), 720
            ),
            timezone=str(data.get("timezone") or TAIPEI_TIMEZONE),
        )


class NotificationStrategy(str, Enum):
    TARGET_ONLY = "target_only"
    PRICE_DROP = "price_drop"
    ALWAYS = "always"
    DIGEST = "digest"


@dataclass(frozen=True)
class NotificationDecision:
    send_now: bool
    queue_digest: bool
    reason: str
    quiet: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "send_now": self.send_now,
            "queue_digest": self.queue_digest,
            "reason": self.reason,
            "quiet": self.quiet,
        }


@dataclass(frozen=True)
class NotificationPolicy:
    """Notification event policy with optional local quiet hours."""

    strategy: NotificationStrategy = NotificationStrategy.ALWAYS
    quiet_hours: Optional[TimeWindow] = None
    timezone: str = TAIPEI_TIMEZONE

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "strategy",
            _enum_value(NotificationStrategy, self.strategy, NotificationStrategy.ALWAYS),
        )
        object.__setattr__(self, "quiet_hours", TimeWindow.from_dict(self.quiet_hours))
        try:
            ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"unknown timezone: {self.timezone}") from exc

    def is_quiet(self, now: Optional[datetime] = None) -> bool:
        if self.quiet_hours is None or self.quiet_hours.start == self.quiet_hours.end:
            return False
        zone = ZoneInfo(self.timezone)
        current = _zoned(now or datetime.now(zone), zone)
        return self.quiet_hours.contains(current.time())

    def next_allowed_time(self, now: Optional[datetime] = None) -> datetime:
        """Return the first local instant at which delivery is not quiet."""
        zone = ZoneInfo(self.timezone)
        current = _zoned(now or datetime.now(zone), zone)
        window = self.quiet_hours
        if window is None or window.start == window.end or not window.contains(current.time()):
            return current
        if window.start < window.end:
            return datetime.combine(current.date(), window.end, tzinfo=zone)
        if current.time() >= window.start:
            return datetime.combine(current.date() + timedelta(days=1), window.end, tzinfo=zone)
        return datetime.combine(current.date(), window.end, tzinfo=zone)

    def evaluate(
        self,
        *,
        current_price: Optional[float],
        target_price: Optional[float] = None,
        previous_price: Optional[float] = None,
        now: Optional[datetime] = None,
        digest_due: bool = False,
        target_hit: Optional[bool] = None,
    ) -> NotificationDecision:
        if current_price is None:
            return NotificationDecision(False, False, "no_price")

        if self.strategy == NotificationStrategy.TARGET_ONLY:
            qualifies = (
                bool(target_hit)
                if target_hit is not None
                else target_price is not None and current_price <= target_price
            )
            reason = "target_hit" if qualifies else "target_not_hit"
        elif self.strategy == NotificationStrategy.PRICE_DROP:
            qualifies = previous_price is not None and current_price < previous_price
            reason = "price_drop" if qualifies else "no_price_drop"
        elif self.strategy == NotificationStrategy.DIGEST:
            qualifies = True
            reason = "digest_due" if digest_due else "digest_queue"
        else:
            qualifies = True
            reason = "always"

        if not qualifies:
            return NotificationDecision(False, False, reason)
        quiet = self.is_quiet(now)
        if quiet:
            return NotificationDecision(False, True, "quiet_hours", quiet=True)
        if self.strategy == NotificationStrategy.DIGEST and not digest_due:
            return NotificationDecision(False, True, reason)
        return NotificationDecision(True, False, reason)

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy.value,
            "quiet_hours": self.quiet_hours.to_dict() if self.quiet_hours else None,
            "timezone": self.timezone,
        }

    @classmethod
    def from_dict(cls, value: Optional[Mapping[str, Any]]) -> "NotificationPolicy":
        data = dict(value or {})
        quiet = data.get("quiet_hours")
        if quiet is None and (data.get("quiet_start") is not None or data.get("quiet_end") is not None):
            quiet = {"start": data.get("quiet_start"), "end": data.get("quiet_end")}
        return cls(
            strategy=data.get(
                "strategy", data.get("notify_strategy", NotificationStrategy.ALWAYS.value)
            ),
            quiet_hours=TimeWindow.from_dict(quiet),
            timezone=str(data.get("timezone") or TAIPEI_TIMEZONE),
        )


def normalize_airline(airline: Any, flight_number: Any = "") -> Optional[AirlineCode]:
    """Normalize English/Chinese airline labels and BR/CI/JX flight numbers."""
    if isinstance(airline, AirlineCode):
        return airline
    text = f"{airline or ''} {flight_number or ''}".strip().upper()
    compact = "".join(character for character in text if character.isalnum())
    aliases = {
        AirlineCode.EVA: ("EVA", "EVAAIR", "長榮", "BR"),
        AirlineCode.CHINA_AIRLINES: ("CHINAAIRLINES", "中華航空", "華航", "CI"),
        AirlineCode.STARLUX: ("STARLUX", "星宇", "JX"),
    }
    for code, names in aliases.items():
        if any(name.upper() in text or name.upper() in compact for name in names[:-1]):
            return code
        flight_prefix = names[-1]
        number = str(flight_number or "").strip().upper().replace(" ", "")
        if number.startswith(flight_prefix):
            return code
        if compact == code.value:
            return code
    return None


def normalize_airline_required(value: Any) -> AirlineCode:
    result = normalize_airline(value)
    if result is None:
        allowed = ", ".join(item.value for item in AirlineCode)
        raise ValueError(f"unsupported airline; expected one of {allowed}")
    return result


def _enum_value(enum_type: type[Enum], value: Any, default: Enum) -> Any:
    if isinstance(value, enum_type):
        return value
    if value is None or value == "":
        return default
    text = str(value).strip().lower()
    for member in enum_type:
        if text in (str(member.value).lower(), member.name.lower()):
            return member
    raise ValueError(f"unsupported {enum_type.__name__}: {value}")


def _clock(value: Any) -> time:
    parsed = _clock_or_none(value)
    if parsed is None:
        raise ValueError("time value is required")
    return parsed


def _clock_or_none(value: Any) -> Optional[time]:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.time().replace(tzinfo=None, second=0, microsecond=0)
    if isinstance(value, time):
        return value.replace(tzinfo=None, second=0, microsecond=0)
    text = str(value).strip()
    try:
        if "T" in text or " " in text:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).time().replace(
                tzinfo=None, second=0, microsecond=0
            )
        parts = text.split(":")
        if len(parts) not in (2, 3):
            raise ValueError
        return time(int(parts[0]), int(parts[1]))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid time: {value}") from exc


def _format_clock(value: time) -> str:
    return value.strftime("%H:%M")


def _zoned(value: datetime, zone: ZoneInfo) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=zone)
    return value.astimezone(zone)


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _optional_int(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    return int(value)


def _optional_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    return float(value)


__all__ = [
    "AirlineCode",
    "AirlineMatch",
    "AirlineRule",
    "AirlineRuleSet",
    "FlightLeg",
    "Itinerary",
    "NotificationDecision",
    "NotificationPolicy",
    "NotificationStrategy",
    "PriceBasis",
    "QuotaForecast",
    "QuotaGuardResult",
    "QuotaMode",
    "QuotaPolicy",
    "ScheduleMode",
    "SchedulePolicy",
    "TAIPEI_TIMEZONE",
    "TimeWindow",
    "normalize_airline",
]
