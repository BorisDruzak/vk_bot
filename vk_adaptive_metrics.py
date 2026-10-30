"""Read-only, anonymous cost estimates from complete durable observations.

Pass elapsed time includes UI preparation, confirmation and mandatory cooldown.
Nested stage diagnostics are deliberately not added to that elapsed time.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone


KINDS = ('like', 'personal', 'community')
FALLBACK_SECONDS = {'like': 60.0, 'personal': 75.0, 'community': 100.0}
SAFETY_FACTOR = 1.15
LOOKBACK_DAYS = 7
MAX_SAMPLES = 20000


def _moment(value):
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        return None
    return value.astimezone(timezone.utc)


def _object(value):
    try:
        value = json.loads(value)
    except (ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _p95(values):
    return sorted(values)[math.ceil(len(values) * .95) - 1] if values else None


def _rows(db, sql, args=()):
    cursor = db.execute(sql, args)
    names = [column[0] for column in cursor.description]
    return [dict(zip(names, row)) for row in cursor]


def estimate_costs(scheduler_db, now, *, groups_count=64):
    """Accept an existing SQLite connection; never initialize state or write.

    The seven-day nearest-rank p95 receives a 15% margin. With fewer than five
    samples the conservative fallback remains a lower bound. All cost estimates
    are forecasts; live guards, deadlines and the API ledger remain authoritative.
    """
    now = _moment(now)
    if now is None:
        raise ValueError('An aware timestamp is required')
    if type(groups_count) is not int or not 0 <= groups_count <= 10000:
        raise ValueError('Invalid groups count')
    lower = now - timedelta(days=LOOKBACK_DAYS)
    tables = {r[0] for r in scheduler_db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    durations = {kind: [] for kind in KINDS}
    truncated = False
    if {'scheduler_jobs', 'scheduler_action_passes'} <= tables:
        rows = _rows(scheduler_db, '''SELECT j.kind,p.started_at,p.finished_at
            FROM scheduler_action_passes p JOIN scheduler_jobs j ON j.id=p.job_id
            WHERE p.attempt_id IS NOT NULL AND p.finished_at IS NOT NULL
            AND julianday(p.finished_at)>=julianday(?) AND julianday(p.finished_at)<=julianday(?)
            ORDER BY julianday(p.finished_at) DESC LIMIT ?''',
            (lower.isoformat(), now.isoformat(), MAX_SAMPLES + 1))
        truncated = len(rows) > MAX_SAMPLES
        for row in rows[:MAX_SAMPLES]:
            start, end = _moment(row['started_at']), _moment(row['finished_at'])
            if row['kind'] not in durations or start is None or end is None:
                continue
            elapsed = (end - start).total_seconds()
            if lower <= start < end <= now and 0 < elapsed <= 86400:
                durations[row['kind']].append(elapsed)

    events = []
    if 'scheduler_diagnostics' in tables:
        rows = _rows(scheduler_db, '''SELECT at,event,snapshot FROM scheduler_diagnostics
            WHERE event IN ('batch_timing','counter_progress')
            AND julianday(at)>=julianday(?) AND julianday(at)<=julianday(?)
            ORDER BY id DESC LIMIT ?''', (lower.isoformat(), now.isoformat(), MAX_SAMPLES + 1))
        truncated |= len(rows) > MAX_SAMPLES
        for row in rows[:MAX_SAMPLES]:
            stamp = _moment(row['at'])
            if stamp is not None:
                events.append((stamp, row['event'], _object(row['snapshot'])))

    refreshes = {}
    for _, event, data in events:
        if event != 'batch_timing':
            continue
        window = data.get('effective_window')
        if not isinstance(window, dict):
            continue
        start, finish = _moment(window.get('claimed_at')), _moment(window.get('refresh_finished_at'))
        if start is not None and finish is not None and lower <= start < finish <= now:
            refreshes[(start, finish)] = []

    metadata = []
    if 'scheduler_meta' in tables:
        for row in _rows(scheduler_db, "SELECT key,value FROM scheduler_meta WHERE key IN ('last_counter_refresh','heartbeat')"):
            value = _object(row['value'])
            if row['key'] == 'heartbeat':
                value = value.get('counter_refresh', {})
            if isinstance(value, dict):
                metadata.append(value)

    for data in metadata:
        start, finish = _moment(data.get('started_at')), _moment(data.get('finished_at'))
        requests = data.get('requests')
        if start is None or finish is None or not lower <= start < finish <= now:
            continue
        # Metadata's refresh begins a few milliseconds after claim; merge the
        # same observation rather than treating it as an additional sample.
        key = next((key for key in refreshes if key[0] <= start <= finish <= key[1] + timedelta(seconds=1)), None)
        if key is None:
            key = (start, finish)
            refreshes[key] = []
        if type(requests) is int and 0 < requests <= 5000:
            refreshes[key].append(requests)

    for (start, finish), counts in refreshes.items():
        for at, event, data in events:
            requests = data.get('requests')
            if event == 'counter_progress' and start <= at <= finish and type(requests) is int and 0 < requests <= 5000:
                counts.append(requests)

    refresh_durations = [(finish-start).total_seconds() for start, finish in refreshes]
    request_counts = [max(counts) for counts in refreshes.values() if counts]
    refresh_requests = max(groups_count, math.ceil(_p95(request_counts)*1.1)) if request_counts else max(1, groups_count*2)
    pacing_floor = 5.0 * max(0, refresh_requests-1)
    refresh_seconds = max(pacing_floor, (_p95(refresh_durations)*SAFETY_FACTOR if refresh_durations else refresh_requests*5.0+20))
    action_seconds = {}
    for kind, values in durations.items():
        observed = _p95(values)
        action_seconds[kind] = round(max(20.0, (observed*SAFETY_FACTOR if observed is not None else 0),
                                        FALLBACK_SECONDS[kind] if len(values)<5 else 0), 3)
    fallback = [kind for kind in KINDS if len(durations[kind])<5]
    reason = 'completed_pass_p95_and_refresh_p95'
    if fallback or not refresh_durations or not request_counts:
        reason += '_with_conservative_fallback'
    return {
        'action_seconds': action_seconds,
        'refresh_seconds': round(refresh_seconds, 3),
        'refresh_requests': refresh_requests,
        'sample_counts': {'actions': {kind: len(values) for kind, values in durations.items()},
                          'refreshes': len(refresh_durations), 'refresh_requests': len(request_counts)},
        'observed_p95_seconds': {kind: _p95(values) for kind, values in durations.items()},
        'model_reason': reason, 'safety_factor': SAFETY_FACTOR,
        'lookback_days': LOOKBACK_DAYS, 'truncated': truncated,
        'assumptions': ['elapsed_pass_includes_cooldown_and_confirmation',
                        'nested_stages_not_summed', 'refresh_request_margin_10_percent',
                        'forecast_not_verified_live_guarantee'],
    }
