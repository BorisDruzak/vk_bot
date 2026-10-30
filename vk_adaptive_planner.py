"""Pure, estimated adaptive-day forecasts. No files, network, or action authority.

Costs are conservative whole-attempt seconds by kind. The remaining mix supplies
its weighted mean: batch targets are counts, never an execution ordering. Real
quota, identity, date, cleanup, pacing and rolling-ledger guards remain mandatory.
"""
from collections import deque
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
import math

_UTC = timezone.utc
_YEKT = timezone(timedelta(hours=5))
_DAY_SECONDS = 86400


def _utc(value, name):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be an aware UTC datetime")
    return value.timestamp()


def _integer(value, name, minimum=0):
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")
    return value


def _seconds(value, name, minimum=0):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number >= {minimum}")
    try:
        number = float(value)
    except OverflowError as exc:
        raise ValueError(f"{name} is too large") from exc
    if not math.isfinite(number) or number < minimum:
        raise ValueError(f"{name} must be a finite number >= {minimum}")
    return number


def _iso(value):
    return datetime.fromtimestamp(value, _UTC).isoformat() if value is not None else None


def plan_day(now, end, remaining, action_seconds, refresh_seconds, refresh_requests,
             api_attempts, last_finished_at=None, rest_seconds=540, api_limit=5000,
             api_pause_seconds=5, identity_reads_per_attempt=1, reserved_reads=0, not_before=None):
    """Return a JSON-safe estimate, preserving the full requested remainder.

    api_attempts contains aware UTC datetimes or aware UTC ISO strings of actual
    shared-ledger reads. Reads expire exactly 24 hours later. reserved_reads is
    capacity held throughout this forecast for known analytics/other readers.
    api_available is capacity free *now*, before this reservation; api_required
    includes planned reads and the reservation, not already-spent ledger reads.

    Pick the most balanced batches that fit the complete weighted mix. If that
    is impossible, report the shortfall explicitly and find the largest safe
    same-mix estimate, again using the most batches. No remainder crosses days.
    not_before may delay admission without changing the actual now/rolling ledger.
    The one-day time/pacing bounds also bound work for huge input quotas.
    """
    now_s, end_s = _utc(now, "now"), _utc(end, "end")
    tomorrow = now.astimezone(_YEKT).date() + timedelta(days=1)
    midnight = datetime.combine(tomorrow, datetime.min.time(), _YEKT).timestamp()
    if end_s < now_s or end_s > midnight:
        raise ValueError("end must be between now and the next YEKT midnight")
    rest = _seconds(rest_seconds, "rest_seconds", 540)
    pause = _seconds(api_pause_seconds, "api_pause_seconds", 5)
    limit = _integer(api_limit, "api_limit", 1)
    if limit > 5000:
        raise ValueError("api_limit cannot exceed the shared hard limit 5000")
    reserve = _integer(reserved_reads, "reserved_reads")
    requests = _integer(refresh_requests, "refresh_requests")
    identities = _integer(identity_reads_per_attempt, "identity_reads_per_attempt")
    refresh = max(_seconds(refresh_seconds, "refresh_seconds"), max(0, requests - 1) * pause)
    if not math.isfinite(refresh):
        raise ValueError("refresh duration is too large")
    if not isinstance(remaining, Mapping) or not isinstance(action_seconds, Mapping):
        raise ValueError("remaining and action_seconds must be mappings")
    if any(kind not in ("like", "personal", "community") for kind in action_seconds):
        raise ValueError("unknown action cost kind")
    costs = {kind: _seconds(value, f"action_seconds[{kind}]", 20)
             for kind, value in action_seconds.items()}
    total = 0
    weighted = 0.0
    for key, count in remaining.items():
        if not isinstance(key, tuple) or len(key) != 3:
            raise ValueError("remaining keys must be (actor, kind, community)")
        actor, kind, community = key
        if isinstance(actor, bool) or not isinstance(actor, (int, str)) or not actor:
            raise ValueError("actor must be a nonempty string or positive integer")
        if isinstance(actor, int) and actor < 1:
            raise ValueError("actor must be positive")
        if kind not in ("like", "personal", "community"):
            raise ValueError("unknown action kind")
        minimum_community = 1 if kind == "community" else 0
        if community is not None and (isinstance(community, bool) or not isinstance(community, int) or community < minimum_community):
            raise ValueError("community must be positive; non-community accepts None/0")
        if kind == "community" and community is None:
            raise ValueError("community action requires an explicit destination")
        count = _integer(count, "remaining count")
        if count and kind not in costs:
            raise ValueError(f"missing action cost for {kind}")
        total += count
        try:
            weighted += count * costs.get(kind, 20)
        except OverflowError as exc:
            raise ValueError("remaining weighted cost is too large") from exc
    if not math.isfinite(weighted):
        raise ValueError("remaining weighted cost must be finite")
    average = weighted / total if total else 20.0
    earliest = max(now_s, _utc(not_before, "not_before")) if not_before is not None else now_s
    if last_finished_at is not None:
        finished = _utc(last_finished_at, "last_finished_at")
        if finished > now_s:
            raise ValueError("last_finished_at cannot be in the future")
        earliest = max(earliest, finished + rest)
    existing = []
    for value in api_attempts:
        if isinstance(value, str):
            try:
                value = datetime.fromisoformat(value)
            except ValueError as exc:
                raise ValueError("invalid api_attempts timestamp") from exc
        stamp = _utc(value, "api_attempts timestamp")
        if stamp > now_s:
            raise ValueError("api_attempts cannot be in the future")
        if stamp + _DAY_SECONDS > now_s:
            existing.append(stamp)
    existing.sort()
    available = max(0.0, end_s - now_s)
    effective_limit = limit - reserve

    # Relative seconds prevent epoch-sized float rounding accumulating per action.
    horizon = end_s - now_s
    first_offset = earliest - now_s

    def simulate(attempt_count, batch_count):
        if attempt_count == 0:
            return []
        if batch_count < 1 or earliest >= end_s:
            return None
        read_count = attempt_count * identities + batch_count * requests
        # Even an empty ledger cannot admit these reads within the time window.
        if read_count and (effective_limit < 1 or (read_count - 1) * pause > end_s - earliest):
            return None
        expirations = deque(stamp - now_s + _DAY_SECONDS for stamp in existing)
        last_read = existing[-1] - now_s if existing else -math.inf
        cursor = first_offset
        batches = []

        def ready_at(candidate):
            candidate = max(candidate, last_read + pause)
            while True:
                while expirations and expirations[0] <= candidate:
                    expirations.popleft()
                if len(expirations) < effective_limit:
                    return candidate
                candidate = max(candidate, expirations[0])

        def read(candidate):
            nonlocal last_read
            candidate = ready_at(candidate)
            if candidate > horizon:
                return None
            expirations.append(candidate + _DAY_SECONDS)
            last_read = candidate
            return candidate

        base, extra = divmod(attempt_count, batch_count)
        for index in range(batch_count):
            target = base + (index < extra)
            if requests or identities:
                cursor = ready_at(cursor)
            start = cursor
            if cursor >= horizon:
                return None
            for _ in range(requests):
                cursor = read(cursor)
                if cursor is None:
                    return None
            cursor = max(cursor, start + refresh)
            if identities:
                for _ in range(target):
                    for _ in range(identities):
                        cursor = read(cursor)
                        if cursor is None:
                            return None
                    cursor += average
                    if cursor > horizon + 0.000001:
                        return None
            else:
                cursor += target * average
            if cursor > horizon + 0.000001:
                return None
            batches.append({"start": _iso(now_s + start), "end": _iso(now_s + min(cursor, horizon)), "target": target})
            cursor += rest
        return batches

    def most_batches(attempt_count):
        if not attempt_count:
            return []
        room = end_s - earliest - attempt_count * average
        if room < 0:
            return None
        upper = min(attempt_count, max(1, math.floor((room + rest) / (refresh + rest) + 1e-9)))
        for batch_count in range(upper, 0, -1):
            batches = simulate(attempt_count, batch_count)
            if batches is not None:
                return batches
        return None

    # One batch has least refresh/rest overhead, so maximizes possible work.
    bound = min(total, max(0, math.floor(max(0, end_s - earliest - refresh) / average + 1e-9)))
    full = most_batches(total) if total <= bound else None
    if total == 0:
        full = []
    if full is not None:
        planned, batches = total, full
    else:
        low, high = 0, bound
        while low < high:
            middle = (low + high + 1) // 2
            if simulate(middle, 1) is not None:
                low = middle
            else:
                high = middle - 1
        planned = low
        batches = most_batches(planned) or []
    feasible = planned == total
    completion = batches[-1]["end"] if batches else None
    return {
        "feasible": feasible,
        "reason": "empty" if not total else "fits" if feasible else "insufficient_time_or_api_budget",
        "remaining_attempts": total,
        "planned_attempts": planned,
        "shortfall": total - planned,
        "batch_count": len(batches),
        "next_target": batches[0]["target"] if batches else 0,
        "next_start": batches[0]["start"] if batches else None,
        "completion_at": completion,
        "projected_seconds": datetime.fromisoformat(completion).timestamp() - now_s if completion else 0.0,
        "available_seconds": available,
        "api_required": planned * identities + len(batches) * requests + reserve,
        "api_available": max(0, limit - len(existing)),
        "batches": batches,
        "maximum_attempts_fit": planned,
        "assumptions": {
            "estimated": True,
            "weighted_action_seconds": average,
            "action_seconds": costs,
            "refresh_seconds": refresh,
            "refresh_requests": requests,
            "rest_seconds": rest,
            "api_pause_seconds": pause,
            "api_limit": limit,
            "identity_reads_per_attempt": identities,
            "reserved_reads": reserve,
            "api_available_scope": "free now before reservations; exact +24h expiry modeled",
            "cost_scope": "whole-attempt weighted mix; extra pacing waits added conservatively",
            "live_guards_required": True,
            "cross_day_backlog": False,
            "not_before": _iso(_utc(not_before, "not_before")) if not_before is not None else None,
        },
    }
