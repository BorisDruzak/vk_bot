"""Deterministic, offline timing experiments using the real Worker and State.

Run ``python vk_schedule_simulation.py --congregation``, ``--five-accounts``,
``--six-batches``, ``--incident`` or ``--input scenario.json``.
With no arguments the saved80 plan from29 October is used. Input ``slots`` are
aware ISO timestamps or local HH:MM:SS strings with ``day``. ``stages`` holds
nonnegative duration samples, selected round-robin (default) or with a seeded
RNG via ``sampling: seeded`` and ``seed``. The preparation samples are measured;
confirmation/cleanup is an explicit residual model, not a new measurement.

Only temporary synthetic databases are opened. Refresh, candidate selection and
action transport are replaced; Worker lifecycle, State claims, quotas, spacing,
intent/click deadlines, RetryLedger and stop guards run unchanged. This is not a
browser, identity, singleton deployment or real HTTP transport acceptance test.
The modeled HTTP path includes20s cap,5s pacing,5/10s retry and shared24h budget.
It cannot establish network retry eligibility; ``network`` is a supplied input.
No settings or saved live plans are read or written.
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import closing
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import random
import sqlite3
import sys
import tempfile
from unittest.mock import patch

import vk_batch as engine
import vk_action_retry as retry
import vk_schedule_policy as policy
import vk_scheduler as scheduler
import vk_scheduler_state as ledger


# Transcribed saved starts, not a newly generated schedule or private actor data.
SAVED80 = '''09:00:00 09:09:19 09:19:28 09:29:21 09:38:56 09:48:24
09:58:24 10:07:37 10:17:52 10:27:26 10:37:20 10:47:42 10:56:48
11:06:12 11:16:16 11:26:34 11:35:44 11:44:53 11:55:12 12:04:44
12:14:40 12:24:37 12:34:08 12:43:35 12:53:36 13:02:50 13:13:04
13:23:16 13:32:32 13:42:14 13:52:00 14:01:58 14:11:28 14:21:51
14:30:56 14:40:29 14:50:24 15:00:09 15:09:52 15:19:15 15:29:20
15:39:19 15:48:48 15:57:59 16:08:16 16:17:41 16:27:44 16:37:55
16:47:12 16:56:56 17:07:03 17:16:24 17:26:02 17:35:52 17:45:33
17:55:20 18:04:38 18:14:48 18:24:24 18:34:16 18:44:32 18:53:44
19:03:50 19:13:12 19:23:01 19:32:40 19:43:01 19:52:08 20:01:56
20:11:36 20:21:49 20:31:04 20:40:24 20:50:32 20:59:40 21:10:00
21:19:37 21:29:28 21:38:54 21:48:56'''.split()


def default_scenario():
    return {'day': '2026-10-29', 'slots': list(SAVED80), 'refresh_seconds': 35.376,
            'stages': {'preparation': [47.18, 48.34, 46.70],
                       'cooldown': [20.03, 20.01, 20.01],
                       'confirmation_cleanup': [4.648, 4.516, 4.810]},
            'sampling': 'round_robin', 'seed': 0}


def incident_scenario():
    scenario = default_scenario()
    scenario.update({'first_slot': 3, 'last_slot': 3,
                     'claim_offsets': {'3': 1.571},
                     'previous_action_at': '09:25:38.071', 'previous_action_slot': 2})
    return scenario


# Successful29Oct observations, stripped to paired seconds only. Preparation
# ends at durable reservation; the second number includes everything after it.
# No actor IDs, post IDs, timestamps, DOM, bindings or live files are embedded.
_MEASURED_ACTION_SECONDS = {'community': [[61.567099, 24.793029], [48.184674, 24.671293], [48.077215, 24.979116],
               [47.951713, 24.637279], [47.202781, 24.317147], [48.37832, 24.18608],
               [46.726326, 24.485211], [47.885457, 23.919939], [48.392431, 24.97799],
               [48.499818, 24.771017], [48.027871, 25.012384], [47.913163, 24.628384],
               [47.661749, 24.772415], [47.598839, 23.825269], [46.322162, 23.607256],
               [47.283879, 23.590457], [47.063961, 25.319291], [48.080125, 25.114334],
               [46.919792, 25.03042], [49.661107, 23.976798], [47.385994, 24.425661],
               [47.665133, 23.853958], [45.787562, 23.804767], [49.518993, 24.746008],
               [47.411059, 24.460447], [51.773114, 24.889328], [64.474544, 24.519158],
               [48.122422, 24.916658], [47.112894, 24.445637]],
 'like': [[8.410123, 38.15013], [9.547551, 37.653317], [7.647082, 38.468587],
          [8.201577, 37.719922], [7.532812, 38.86793], [7.827896, 38.505314],
          [7.093521, 38.598959], [9.06487, 37.113924], [7.737242, 37.78253],
          [6.739553, 38.772431], [8.332229, 37.904149], [8.754676, 37.870491],
          [7.822469, 37.426367], [6.977327, 38.41376], [10.867637, 36.273705],
          [7.65227, 38.776306], [8.064804, 38.615027], [8.546548, 37.797196],
          [7.081678, 38.578942], [8.609502, 37.867616], [7.640219, 38.45012],
          [8.250954, 37.778305], [12.129309, 40.347002], [7.570914, 38.498775],
          [7.609727, 43.466228], [7.352112, 38.844908], [8.101987, 36.789583],
          [7.111348, 38.447898], [6.451736, 39.111326], [6.691715, 38.848882],
          [5.891636, 38.720612], [8.618272, 37.818441], [7.639517, 38.756552],
          [8.384143, 39.334164], [6.680494, 38.940366], [8.43989, 38.311682],
          [7.249743, 38.07369], [7.978716, 38.20834], [6.207372, 39.069868],
          [7.908977, 38.618249], [7.576304, 38.381964], [8.69939, 38.068564],
          [10.777668, 36.370645], [6.314536, 39.121786], [6.366133, 39.293921],
          [7.12056, 38.372266], [5.893822, 38.533201], [7.290057, 38.292162],
          [6.640968, 38.502149], [7.342671, 38.438818], [5.829514, 39.045815],
          [7.111193, 38.474584], [6.213033, 39.159898], [6.567454, 39.633486],
          [6.096597, 39.135603], [8.003307, 37.65441], [6.686131, 38.910006],
          [6.184634, 39.071441], [5.954171, 39.178365], [7.808606, 38.632905],
          [7.567047, 38.655809], [7.035281, 39.438963]],
 'personal': [[9.955162, 36.90819], [9.190269, 36.944776], [7.876429, 37.838546],
              [9.31908, 36.694279], [9.395027, 36.522196], [9.944533, 36.892222],
              [7.038413, 40.332384], [11.565252, 41.705818], [9.218906, 36.974737],
              [8.254224, 37.410456], [7.010438, 38.524174], [8.231911, 37.345594],
              [23.236204, 37.34888], [10.208794, 36.86128]]}


def six_batch_scenario(seed=29):
    """Synthetic future54x6 plan, built by the real seeded policy scheduler.

    This scenario starts with empty ledgers and unlimited synthetic candidates.
    Its measured successes are timing inputs, not a prediction of live success.
    """
    accounts = {str(n): {'likes': 50, 'personal_reposts': 10,
                         'community_reposts': 20,
                         'communities': [100 + n * 2, 101 + n * 2]}
                for n in (1, 2, 5, 6)}
    config = policy.validate_config({'start_hour': 9, 'end_hour': 22,
                                     'batch_size': 6, 'accounts': accounts})
    slots = policy.build_schedule(date(2026, 10, 30), config, rng=random.Random(seed))
    return {'day': '2026-10-30', 'policy': config,
            'slots': [_stamp(at) for at in slots], 'seed': seed,
            'sampling': 'seeded', 'refresh_seconds': 120.341770,
            'duration_samples': deepcopy(_MEASURED_ACTION_SECONDS),
            'measurement_basis': {
                'source': '29Oct history-evidence.json successful timing samples',
                'successful_actions': 105,
                'by_kind': {'community': 29, 'like': 62, 'personal': 14},
                'refresh': 'one measured complete refresh120.341770s',
                'after_reservation_model': '20s cooldown plus measured residual; click included in residual',
                'live_four_of_four': 'separate29Oct acceptance observation; not a six-action live test'}}


def five_account_scenario(seed=29):
    """Synthetic future67x6 plan with five equal80-attempt account quotas.

    Reuse the same measured timing model; actor7 and both its destinations are
    synthetic. This factory does not connect, enable or inspect a live account.
    """
    scenario = six_batch_scenario(seed=seed)
    scenario['policy']['accounts']['7'] = {
        'likes': 50, 'personal_reposts': 10, 'community_reposts': 20,
        'communities': [114, 115]}
    config = policy.validate_config(scenario['policy'])
    scenario['policy'] = config
    scenario['slots'] = [_stamp(at) for at in policy.build_schedule(
        date.fromisoformat(scenario['day']), config, rng=random.Random(seed))]
    return scenario


def congregation_scenario(seed=29):
    """Synthetic67-slot plan: four80 quotas plus actor7's50/0/20 quota.

    The390 attempts are allocated across the explicitly retained67 slots;
    deadlines can still leave an allocation partially unused without catchup.
    """
    scenario = five_account_scenario(seed=seed)
    scenario['policy']['accounts']['7']['personal_reposts'] = 0
    scenario['policy']['planned_batches'] = 67
    config = policy.validate_config(scenario['policy'])
    scenario['policy'] = config
    scenario['slots'] = [_stamp(at) for at in policy.build_schedule(
        date.fromisoformat(scenario['day']), config, rng=random.Random(seed))]
    return scenario


def _instant(value, day):
    if not isinstance(value, str):
        raise ValueError('Times must be strings')
    result = datetime.fromisoformat(value if 'T' in value else day + 'T' + value)
    if result.tzinfo is None:
        if 'T' in value:
            raise ValueError('ISO timestamps must include a timezone')
        result = result.replace(tzinfo=policy.LOCAL_TZ)
    return result.astimezone(policy.LOCAL_TZ)


def _seconds(value):
    if type(value) not in (int, float) or not 0 <= value <= 86400:
        raise ValueError('Durations must be finite numbers between0 and86400')
    return float(value)


def _stamp(value):
    return value.astimezone(policy.LOCAL_TZ).isoformat() if value is not None else None


class FakeClock:
    """Advance aware wall time without sleeping; notify a synthetic stop gate."""
    def __init__(self, now, on_advance=None):
        if now.tzinfo is None:
            raise ValueError('FakeClock requires aware time')
        self.now = now
        self.on_advance = on_advance

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += timedelta(seconds=_seconds(seconds))
        if self.on_advance:
            self.on_advance(self.now)

    def advance_to(self, when):
        if when > self.now:
            self.sleep((when - self.now).total_seconds())


class _Samples:
    def __init__(self, scenario):
        self.stages = scenario.get('stages', default_scenario()['stages'])
        if set(self.stages) - {'preparation', 'cooldown', 'confirmation_cleanup', 'click'}:
            raise ValueError('Unknown modeled stage')
        self.mode = scenario.get('sampling', 'round_robin')
        if self.mode not in ('round_robin', 'seeded'):
            raise ValueError('sampling must be round_robin or seeded')
        self.rng = random.Random(scenario.get('seed', 0))
        self.indices = Counter()
        self.duration_samples = scenario.get('duration_samples', {})
        if not isinstance(self.duration_samples, dict) or set(self.duration_samples) - {'like', 'personal', 'community'}:
            raise ValueError('Duration samples must be keyed by action kind')
        for values in self.duration_samples.values():
            if not isinstance(values, list) or not values:
                raise ValueError('Each action kind needs nonempty paired timing samples')
            for pair in values:
                if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                    raise ValueError('Expected preparation and after-reservation seconds')
                _seconds(pair[0]); _seconds(pair[1])
                if pair[1] < 20:
                    raise ValueError('After-reservation sample must preserve20 second cooldown')
        for name, values in self.stages.items():
            if not isinstance(values, list) or not values:
                raise ValueError('Each stage needs a nonempty sample list')
            for value in values:
                _seconds(value)
                if name == 'cooldown' and value < 20:
                    raise ValueError('Modeled cooldown must preserve20 seconds')

    def take(self, name):
        values = self.stages.get(name, [20 if name == 'cooldown' else 0])
        index = self.indices[name] % len(values)
        self.indices[name] += 1
        return float(self.rng.choice(values) if self.mode == 'seeded' else values[index])

    def action(self, kind):
        values = self.duration_samples.get(kind)
        if values:
            index = self.indices[kind] % len(values)
            self.indices[kind] += 1
            preparation, after = self.rng.choice(values) if self.mode == 'seeded' else values[index]
            return {'preparation': preparation, 'cooldown': 20,
                    'confirmation_cleanup': after - 20, 'click': 0}
        return {name: self.take(name) for name in
                ('preparation', 'cooldown', 'confirmation_cleanup', 'click')}


class _SyntheticWork:
    """External-boundary doubles; scheduling decisions remain in Worker/State."""
    def __init__(self, scenario, state, clock):
        self.scenario, self.state, self.clock = scenario, state, clock
        self.samples = _Samples(scenario)
        self.worker = None
        self.batch = None
        self.sequence = 0
        self.read_starts = []

    def refresh(self, *, reserve=False, deadline=None):
        # Worker.prepare_batch_pool still handles ready/error/deadline outcomes.
        deadline = self.worker.reserve_refresh_deadline(self.worker._active_slot)
        report = {'started_at': self.clock().isoformat(), 'requests': 0,
                  'selected_band': 'old', 'band': 'old'}
        model = {'modeled': True, 'attempts': 0, 'retry_wait_seconds': [],
                 'transports': [], 'pacing_wait_seconds': []}
        self.batch['read_model'] = model
        state = 'ready'
        if 'refresh_reads' in self.scenario or 'refresh_pages' in self.scenario:
            pages = self.scenario.get('refresh_pages', [self.scenario.get('refresh_reads', [])])
            for page in pages:
                success = False
                for index, supplied in enumerate(page[:3]):
                    wait = (5, 10)[index - 1] if index else 0
                    if wait:
                        model['retry_wait_seconds'].append(wait)
                        self.clock.sleep(wait)
                    pace = max(0, 5 - (self.clock() - self.read_starts[-1]).total_seconds()) if self.read_starts else 0
                    model['pacing_wait_seconds'].append(pace)
                    if (self.clock() + timedelta(seconds=pace + 20)) >= deadline:
                        state = 'deadline'; break
                    self.clock.sleep(pace)
                    if self.state.get('stop_requested', False):
                        state = 'stopped'; break
                    recent = [at for at in self.read_starts if (self.clock() - at).total_seconds() < 86400]
                    if len(recent) >= self.scenario.get('api_budget', 5000):
                        state = 'budget'; break
                    seconds = min(20, _seconds(supplied.get('seconds', 0)))
                    outcome = supplied.get('outcome', 'ok')
                    if outcome not in ('ok', 'network', '429', 'captcha'):
                        raise ValueError('Unknown modeled HTTP outcome')
                    if supplied.get('seconds', 0) > 20:
                        outcome = 'network'
                    started = self.clock(); self.read_starts.append(started)
                    self.clock.sleep(seconds)
                    model['transports'].append({'started_at': _stamp(started), 'seconds': seconds, 'outcome': outcome})
                    model['attempts'] += 1
                    if outcome == 'ok':
                        success = True; break
                    if outcome in ('429', 'captcha'):
                        self.state.pause('modeled_' + outcome); state = 'stopped'; break
                if state != 'ready': break
                if not success:
                    state = 'error'; break
            report['requests'] = model['attempts']
        else:
            seconds = self.scenario.get('refresh_seconds', 35.376)
            if isinstance(seconds, list):
                seconds = seconds[self.worker._active_slot % len(seconds)]
            seconds = _seconds(seconds)
            self.clock.sleep(min(seconds, max(0, (deadline - self.clock()).total_seconds())))
            if self.clock() >= deadline and seconds:
                state = 'deadline'
            if self.state.get('stop_requested', False):
                state = 'stopped'
        self.batch['refresh_finished_at'] = _stamp(self.clock())
        return {'state': state, 'report': report, 'pool': {(991, n) for n in range(1, 1001)} if state == 'ready' else set()}

    def pick(self, now):
        remaining = self.state.remaining(now)
        choices = [item for item in self.state.categories() if remaining[item[:3]] > 0]
        if not choices or self.scenario.get('empty_pool', False):
            return None
        # Explicit synthetic round robin, not a claim to reproduce live priority.
        account, kind, target, _ = choices[self.sequence % len(choices)]
        self.sequence += 1
        return account, kind, target, {'group_id': 991, 'post_id': self.sequence,
            'published_at': policy.day_bounds(now)[0], 'like_count': 0,
            'repost_count': 0, 'comment_count': 0, 'observed_at': now.isoformat()}

    def execute(self, account, kind, row, *, before_attempt, record_attempt,
                before_click, **options):
        started = self.clock()
        action = {'account': account, 'kind': kind, 'started_at': _stamp(started),
                  'intent_at': None, 'attempt_id': None, 'status': None,
                  'preparation_seconds': 0, 'click_seconds': 0,
                  'cooldown_seconds': 0, 'confirmation_cleanup_seconds': 0,
                  'finished_at': None}
        self.batch['actions'].append(action)
        outcomes = self.scenario.get('outcomes', [])
        index = self.sequence - 1
        outcome = outcomes[index] if index < len(outcomes) else 'verified'
        if outcome not in ('verified', 'already', 'fail_before_intent', 'fail_after_intent'):
            raise ValueError('Unknown synthetic action outcome')
        reserved = False
        durations = self.samples.action(kind)
        try:
            action['preparation_seconds'] = durations['preparation']
            self.clock.sleep(action['preparation_seconds'])
            if outcome == 'fail_before_intent':
                raise RuntimeError('Synthetic failure before intent')
            if outcome == 'already':
                action['status'] = 'already'
                return {'status': 'already', 'attempt_id': None}
            before_attempt(); reserved = True
            action['intent_at'] = _stamp(self.clock())
            with closing(sqlite3.connect(self.state.paths[account])) as db:
                # Disposable modeled ledgers need no physical crash durability.
                # Transactions/constraints and real guard decisions stay active.
                db.execute('PRAGMA synchronous=OFF')
                attempt = db.execute('INSERT INTO action_attempts(group_id,post_id,action,attempted_at) VALUES(?,?,?,?)',
                    (row['group_id'], row['post_id'], 'like' if kind == 'like' else 'repost',
                     self.clock().astimezone(timezone.utc).isoformat(timespec='seconds'))).lastrowid
                if kind == 'community':
                    run = db.execute('INSERT INTO community_runs(community_id) VALUES(?)', (row['community_id'],)).lastrowid
                    db.execute('INSERT INTO community_run_attempts VALUES(?,?)', (run, attempt))
                db.commit()
            record_attempt(attempt); action['attempt_id'] = attempt
            timeout = before_click()
            click_seconds = durations['click']
            action['click_seconds'] = min(click_seconds, timeout / 1000)
            self.clock.sleep(min(click_seconds, timeout / 1000))
            if click_seconds * 1000 > timeout:
                raise RuntimeError('Synthetic click exceeded real click deadline')
            if outcome == 'fail_after_intent':
                raise RuntimeError('Synthetic failure after durable intent')
        except Exception:
            action['status'] = 'uncertain' if reserved else 'failed'
            raise
        finally:
            if reserved:
                action['cooldown_seconds'] = durations['cooldown']
                self.clock.sleep(action['cooldown_seconds'])
            if action['status'] is not None:
                action['finished_at'] = _stamp(self.clock())
        action['confirmation_cleanup_seconds'] = durations['confirmation_cleanup']
        self.clock.sleep(action['confirmation_cleanup_seconds'])
        action['status'] = outcome
        action['finished_at'] = _stamp(self.clock())
        return {'status': outcome, 'attempt_id': attempt}


def _config(scenario):
    # All IDs are synthetic and are never used in a browser or network request.
    accounts = {str(n): {'likes': 50, 'personal_reposts': 10,
                        'community_reposts': 20, 'communities': [100 + n * 2, 101 + n * 2]}
                for n in (1, 2, 5, 6)}
    return policy.validate_config({'end_hour': 22, 'accounts': accounts,
                                   **scenario.get('policy', {})})


def simulate(scenario=None):
    """Return a JSON-safe report; no caller records or live runtime are changed."""
    scenario = deepcopy(default_scenario() if scenario is None else scenario)
    if not isinstance(scenario, dict):
        raise ValueError('Scenario must be a JSON object')
    refresh_seconds = scenario.get('refresh_seconds', 35.376)
    if isinstance(refresh_seconds, list):
        if not refresh_seconds:
            raise ValueError('Refresh samples must not be empty')
        for value in refresh_seconds:
            _seconds(value)
    else:
        _seconds(refresh_seconds)
    if 'refresh_reads' in scenario and 'refresh_pages' in scenario:
        raise ValueError('Use refresh_reads or refresh_pages, not both')
    if 'refresh_reads' in scenario or 'refresh_pages' in scenario:
        pages = scenario.get('refresh_pages', [scenario.get('refresh_reads')])
        if not isinstance(pages, list) or not pages:
            raise ValueError('Modeled refresh needs a nonempty page list')
        for page in pages:
            if not isinstance(page, list) or not page:
                raise ValueError('Each modeled page needs a list of transport outcomes')
            for supplied in page:
                if not isinstance(supplied, dict):
                    raise ValueError('Modeled transport outcomes must be objects')
                _seconds(supplied.get('seconds', 0))
                if supplied.get('outcome', 'ok') not in ('ok', 'network', '429', 'captcha'):
                    raise ValueError('Unknown modeled HTTP outcome')
    day = scenario.get('day', '2026-10-29')
    cfg = _config(scenario)
    slots = [_instant(value, day) for value in scenario.get('slots', SAVED80)]
    policy._validate_schedule(slots, cfg)
    attempt_limits = [policy.batch_attempt_limit(cfg, slot) for slot in range(len(slots))]
    first, last = scenario.get('first_slot', 0), scenario.get('last_slot', len(slots) - 1)
    if type(first) is not int or type(last) is not int or not 0 <= first <= last < len(slots):
        raise ValueError('Invalid simulation slot range')
    api_budget = scenario.get('api_budget', 5000)
    if type(api_budget) is not int or not 1 <= api_budget <= 5000:
        raise ValueError('Modeled shared API budget must be between1 and5000')
    clock = FakeClock(slots[first])
    batches = []
    with tempfile.TemporaryDirectory(prefix='vk-schedule-offline-') as directory:
        root = Path(directory)
        paths = {int(n): root / ('actor_' + n + '.sqlite3') for n in cfg['accounts']}
        for path in paths.values():
            with closing(engine.db_open(path)) as db:
                db.executescript('CREATE TABLE community_runs(id INTEGER PRIMARY KEY,community_id INTEGER);'
                                 'CREATE TABLE community_run_attempts(run_id INTEGER,attempt_id INTEGER);')
        with closing(ledger.State(root / 'scheduler.sqlite3', paths, cfg)) as state:
            state.db.execute('PRAGMA synchronous=OFF')
            state.db.execute('INSERT INTO schedule_days VALUES(?,?,?)',
                (day, json.dumps(cfg, sort_keys=True), json.dumps([t.isoformat() for t in slots])))
            state.db.commit()
            state.set('action_retry_policy', 1)
            if scenario.get('previous_action_at'):
                state.set('last_action_batch', {'day': day, 'slot': scenario.get('previous_action_slot', first - 1),
                          'at': _instant(scenario['previous_action_at'], day).isoformat()})
            stop_at = _instant(scenario['stop_at'], day) if scenario.get('stop_at') else None
            def stop_gate(now):
                if stop_at is not None and now >= stop_at:
                    state.set('stop_requested', True)
            clock.on_advance = stop_gate
            work = _SyntheticWork(scenario, state, clock)
            worker = scheduler.Worker(state, root / 'public.sqlite3', {'groups': []}, clock=clock)
            work.worker = worker
            worker.sleeper = clock.sleep
            worker.refresh_batch = work.refresh
            worker.pick = work.pick
            # Suppress only the external diagnostics sink; State's temp SQLite
            # diagnostic records and real lifecycle remain active.
            with patch.object(scheduler.actions, 'execute_action', work.execute), \
                    patch.object(scheduler.diagnostics, 'write', lambda record: None):
                for slot in range(first, last + 1):
                    planned = slots[slot]
                    end = policy.work_bounds(planned, cfg)[1]
                    boundary = min(end, slots[slot + 1]) if slot + 1 < len(slots) else end
                    offset = _seconds(scenario.get('claim_offsets', {}).get(str(slot), 0))
                    clock.advance_to(planned + timedelta(seconds=offset))
                    # Model the daemon's next eligible claim wake after the prior
                    # batch, preserving both real claim guards and no catchup.
                    previous = state.db.execute('SELECT started_at,finished_at FROM scheduler_batches ORDER BY started_at DESC LIMIT 1').fetchone()
                    if previous:
                        clock.advance_to(datetime.fromisoformat(previous['started_at']) + timedelta(seconds=540))
                        if previous['finished_at']:
                            clock.advance_to(datetime.fromisoformat(previous['finished_at']) + timedelta(seconds=20))
                    batch = {'slot': slot, 'attempt_limit': attempt_limits[slot],
                             'planned_at': _stamp(planned), 'boundary_at': _stamp(boundary),
                             'intent_deadline': _stamp(boundary - timedelta(seconds=60)),
                             'refresh_deadline': _stamp(boundary - timedelta(seconds=120)),
                             'started_at': None, 'finished_at': None, 'refresh_finished_at': None,
                             'spacing_ready_at': None, 'actions': [], 'read_model': None}
                    work.batch = batch
                    saved = state.get('last_action_batch')
                    if saved and (saved['day'], saved['slot']) != (day, slot):
                        batch['spacing_ready_at'] = _stamp(datetime.fromisoformat(saved['at']) + timedelta(seconds=540))
                    if clock() > planned + timedelta(seconds=120):
                        phase, reason = 'missed', 'claim_grace_expired'
                    elif state.get('stop_requested', False):
                        phase, reason = 'batch_stop_requested', 'stop_requested'
                    elif state.blocked():
                        phase, reason = 'attention', 'paused_or_uncertain'
                    elif clock().timestamp() < state.get('technical_backoff_until', 0):
                        phase, reason = 'technical_error_backoff', 'technical_error_backoff'
                    else:
                        result = worker._run_batch(slot)
                        phase = reason = result['phase']
                    row = state.db.execute('SELECT * FROM scheduler_batches WHERE day=? AND slot=?', (day, slot)).fetchone()
                    if row:
                        batch['started_at'] = _stamp(datetime.fromisoformat(row['started_at']))
                        batch['finished_at'] = _stamp(datetime.fromisoformat(row['finished_at']))
                    jobs = [dict(row) for row in state.db.execute('SELECT * FROM scheduler_jobs WHERE day=? AND slot=?', (day, slot))]
                    attempts = sum(a['attempt_id'] is not None for a in batch['actions'])
                    batch.update({'phase': phase, 'reason': reason, 'attempts': attempts,
                        'first_intent_at': next((a['intent_at'] for a in batch['actions'] if a['intent_at']), None),
                        'first_action_at': next((a['intent_at'] for a in batch['actions'] if a['intent_at']), None),
                        'first_preparation_at': batch['actions'][0]['started_at'] if batch['actions'] else None,
                        'verified': sum(j['status'] == 'verified' for j in jobs),
                        'already': sum(j['status'] == 'already' for j in jobs),
                        'uncertain': sum(j['status'] == 'uncertain' for j in jobs),
                        'failed_without_attempt': sum(j['status'] == 'failed' and j['attempt_id'] is None for j in jobs),
                        'classification': ('missed' if not row else 'empty' if not attempts else
                                           'complete' if attempts == attempt_limits[slot] else 'partial'),
                        'next_slot_offset_seconds': round((clock() - slots[slot + 1]).total_seconds(), 6) if slot + 1 < len(slots) else None})
                    batches.append(batch)
            actor_attempts = {}
            for account, path in paths.items():
                with closing(sqlite3.connect(path)) as db:
                    actor_attempts[str(account)] = db.execute('SELECT COUNT(*) FROM action_attempts').fetchone()[0]
            saved_slots = json.loads(state.db.execute('SELECT slots FROM schedule_days WHERE day=?', (day,)).fetchone()[0])
            if saved_slots != [t.isoformat() for t in slots]:
                raise AssertionError('Simulation unexpectedly changed saved slots')
            saved_config = json.loads(state.db.execute('SELECT config FROM schedule_days WHERE day=?', (day,)).fetchone()[0])
            if saved_config != cfg:
                raise AssertionError('Simulation unexpectedly changed saved configuration')
            used = state.usage(slots[-1])
            actor_kinds = {str(account): {kind: sum(value for (a, k, _), value in used.items()
                                                  if a == account and k == kind)
                                         for kind in ('like', 'personal', 'community')}
                           for account in paths}
            community_targets = {str(account): {str(target): used[(account, 'community', target)]
                                                for target in cfg['accounts'][str(account)]['communities']}
                                 for account in paths}
            retry_summary = retry.RetryLedger(state).summary()
            retry_summary['active'] = state.db.execute(
                "SELECT COUNT(*) FROM scheduler_action_recovery WHERE state IN ('running','pending')").fetchone()[0]
    gaps = [(b - a).total_seconds() for a, b in zip(slots, slots[1:])]
    counts = Counter(b['classification'] for b in batches)
    return {'offline': True, 'model': 'real Worker/State; synthetic refresh, selection and actions',
        'sampling': scenario.get('sampling', 'round_robin'), 'seed': scenario.get('seed', 0),
        'measurement_basis': scenario.get('measurement_basis'),
        'retry': retry_summary,
        'limitations': ['No real network, browser, identity or VM acceptance.',
            'Temporary ledgers skip physical fsync; this is not crash-durability proof.',
            'Historical model measures preparation/cooldown; per-kind model measures preparation and aggregate after-reservation.',
            'Synthetic candidate round robin does not reproduce live source priority.',
            'Paired per-kind observations model20s cooldown plus measured residual; internal stage order is simplified.',
            'Unlimited synthetic candidates and successful outcomes do not forecast live quota completion.',
            'HTTP outcomes/eligibility are supplied model inputs, not transport proof.',
            'Historical action usage is empty unless produced during this simulation.'],
        'plan': {'slots': [_stamp(t) for t in slots], 'unchanged': True, 'batch_size': cfg['batch_size'],
            **({'attempt_limits': attempt_limits} if 'planned_batches' in cfg else {}),
            'min_gap_seconds': min(gaps) if gaps else None, 'max_gap_seconds': max(gaps) if gaps else None,
            'unique_gaps': len(set(gaps))},
        'summary': {'theoretical_attempt_capacity': policy.daily_total(cfg),
            'simulated_slot_capacity': len(batches) * cfg['batch_size'],
            'planned_attempt_capacity': sum(batch['attempt_limit'] for batch in batches),
            'attempts': sum(b['attempts'] for b in batches), 'verified': sum(b['verified'] for b in batches),
            'already': sum(b['already'] for b in batches), 'uncertain': sum(b['uncertain'] for b in batches),
            'complete': counts['complete'], 'partial': counts['partial'], 'empty': counts['empty'],
            'missed': counts['missed'], 'actor_attempts': actor_attempts,
            'actor_kind_attempts': actor_kinds, 'community_target_attempts': community_targets,
            'modeled_http_attempts': len(work.read_starts)}, 'batches': batches}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--input', help='Scenario JSON file; - reads stdin')
    group.add_argument('--incident', action='store_true', help='Reproduce09:29 three-of-four example')
    group.add_argument('--six-batches', action='store_true', help='Model future54x6 with unchanged320 quotas')
    group.add_argument('--five-accounts', action='store_true', help='Model future67x6 with five equal80-attempt quotas')
    group.add_argument('--congregation', action='store_true', help='Model390 attempts across67 slots; fifth account50/0/20')
    args = parser.parse_args(argv)
    try:
        if args.input:
            scenario = json.load(sys.stdin) if args.input == '-' else json.loads(Path(args.input).read_text(encoding='utf-8'))
        else:
            scenario = (congregation_scenario() if args.congregation else
                        five_account_scenario() if args.five_accounts else
                        six_batch_scenario() if args.six_batches else
                        incident_scenario() if args.incident else default_scenario())
        print(json.dumps(simulate(scenario), ensure_ascii=False, indent=2))
        return 0
    except (ValueError, TypeError, KeyError) as exc:
        parser.error(str(exc))


if __name__ == '__main__':
    raise SystemExit(main())
