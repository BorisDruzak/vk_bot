"""Adaptive forecasts and immutable claims layered on the existing action ledger."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3

import vk_schedule_policy as policy
from vk_scheduler_state import State
from vk_adaptive_metrics import estimate_costs
from vk_adaptive_planner import plan_day


class AdaptiveState(State):
    def __init__(self, path, account_paths, config=None):
        super().__init__(path, account_paths, config)
        if self.config.get('schedule_mode') != 'adaptive':
            raise ValueError('Adaptive state requires adaptive policy')
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS scheduler_adaptive_days(
            day TEXT PRIMARY KEY,activated_at TEXT NOT NULL,base_slot INTEGER NOT NULL);
          CREATE TABLE IF NOT EXISTS scheduler_adaptive_forecasts(
            id INTEGER PRIMARY KEY,day TEXT NOT NULL,at TEXT NOT NULL,
            config TEXT NOT NULL,remaining TEXT NOT NULL,forecast TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS scheduler_adaptive_batches(
            day TEXT NOT NULL,slot INTEGER NOT NULL,revision INTEGER NOT NULL,
            target_attempts INTEGER NOT NULL,config TEXT NOT NULL,estimated_end TEXT,
            enabled_accounts TEXT NOT NULL,
            PRIMARY KEY(day,slot));
          CREATE TABLE IF NOT EXISTS scheduler_adaptive_recoveries(
            day TEXT NOT NULL,slot INTEGER NOT NULL,qualified_at TEXT NOT NULL,
            PRIMARY KEY(day,slot));
        ''')
        self.db.commit()

    def active_row(self, now):
        if not self.table(self.db, 'scheduler_adaptive_recoveries'):
            return self.db.execute('SELECT * FROM scheduler_batches WHERE finished_at IS NULL ORDER BY started_at DESC LIMIT 1').fetchone()
        return self.db.execute('''SELECT b.* FROM scheduler_batches b WHERE b.finished_at IS NULL
          AND NOT EXISTS(SELECT 1 FROM scheduler_adaptive_recoveries r WHERE r.day=b.day AND r.slot=b.slot)
          ORDER BY started_at DESC LIMIT 1''').fetchone()

    def refresh_policy(self, now):
        paths_provider = getattr(self, 'paths_provider', None)
        if paths_provider:
            self.paths.update({int(k): Path(v) for k, v in paths_provider().items()})
        active = self.active_row(now)
        if active and self.table(self.db, 'scheduler_adaptive_batches'):
            saved = self.db.execute('SELECT config FROM scheduler_adaptive_batches WHERE day=? AND slot=?',
                                    (active['day'], active['slot'])).fetchone()
            if saved:
                self.config = policy.validate_config(json.loads(saved[0]))
                return
        provider = getattr(self, 'policy_provider', None)
        if provider:
            self.config = policy.validate_config(provider())

    def account_enabled(self, account):
        if not super().account_enabled(account):
            return False
        if self.table(self.db, 'scheduler_adaptive_batches'):
            row = self.db.execute('''SELECT a.enabled_accounts FROM scheduler_adaptive_batches a
              JOIN scheduler_batches b ON b.day=a.day AND b.slot=a.slot
              WHERE b.finished_at IS NULL AND NOT EXISTS(
                SELECT 1 FROM scheduler_adaptive_recoveries r WHERE r.day=b.day AND r.slot=b.slot)
              ORDER BY b.started_at DESC LIMIT 1''').fetchone()
            if row is not None:
                return int(account) in json.loads(row[0])
        return True

    def calendar_end(self, now):
        return policy.work_bounds(now, self.config)[1]

    def ensure_day(self, now):
        day = str(policy.local_day(now))
        if self.db.execute('SELECT 1 FROM scheduler_adaptive_days WHERE day=?', (day,)).fetchone():
            return
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            saved = self.db.execute('SELECT slots FROM schedule_days WHERE day=?', (day,)).fetchone()
            count = len(json.loads(saved[0])) if saved else 0
            highest = self.db.execute('SELECT MAX(slot) FROM scheduler_batches WHERE day=?', (day,)).fetchone()[0]
            base = max(count, highest + 1 if highest is not None else 0)
            self.db.execute('INSERT OR IGNORE INTO schedule_days VALUES(?,?,?)',
                            (day, json.dumps(self.config, sort_keys=True), '[]'))
            self.db.execute('INSERT OR IGNORE INTO scheduler_adaptive_days VALUES(?,?,?)',
                            (day, now.isoformat(), base))

    def latest_forecast(self, now):
        if not self.table(self.db, 'scheduler_adaptive_forecasts'):
            return None
        return self.db.execute('SELECT * FROM scheduler_adaptive_forecasts WHERE day=? ORDER BY id DESC LIMIT 1',
                               (str(policy.local_day(now)),)).fetchone()

    def day_plan(self, now):
        row = self.latest_forecast(now)
        return [datetime.fromisoformat(b['start']) for b in json.loads(row['forecast'])['batches']] if row else []

    def last_finished_at(self):
        values = [datetime.fromisoformat(r[0]) for r in self.db.execute(
            'SELECT finished_at FROM scheduler_batches WHERE finished_at IS NOT NULL')]
        if self.table(self.db, 'scheduler_adaptive_recoveries'):
            values.extend(datetime.fromisoformat(r[0]) for r in self.db.execute(
                'SELECT qualified_at FROM scheduler_adaptive_recoveries'))
        return max(values) if values else None

    def rest_ready_at(self, now):
        finished = self.last_finished_at()
        ready = finished + timedelta(seconds=self.config['batch_rest_seconds']) if finished else now
        gate = self.get('resume_not_before', 0)
        if gate:
            ready = max(ready, datetime.fromtimestamp(gate, timezone.utc))
        return ready

    def next_slot(self, now):
        day = str(policy.local_day(now))
        row = self.db.execute('SELECT base_slot FROM scheduler_adaptive_days WHERE day=?', (day,)).fetchone()
        highest = self.db.execute('SELECT MAX(slot) FROM scheduler_batches WHERE day=?', (day,)).fetchone()[0]
        return max(row[0] if row else 0, highest + 1 if highest is not None else 0)

    def _api_attempts(self, now):
        path = getattr(self, 'source_path', self.paths.get(1))
        if path is None:
            raise RuntimeError('Shared API ledger path is unavailable')
        db = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)
        try:
            if not self.table(db, 'api_read_attempts'):
                return []
            cutoff = (now - timedelta(days=1)).isoformat()
            return [r[0] for r in db.execute('SELECT attempted_at FROM api_read_attempts WHERE attempted_at>=? ORDER BY attempted_at', (cutoff,))]
        finally:
            db.close()

    def update_forecast(self, now, reason='tick', force=False):
        self.refresh_policy(now)
        self.ensure_day(now)
        row = self.latest_forecast(now)
        if self.active_row(now):
            return json.loads(row['forecast']) if row else None
        remaining = self.remaining(now)
        serialized_remaining = json.dumps({f'{a}:{k}:{c}': n for (a, k, c), n in remaining.items()}, sort_keys=True)
        serialized_config = json.dumps(self.config, sort_keys=True)
        # Keep a due forecast stable until claim. Waiting API forecasts are refreshed
        # periodically for other readers and exact rolling-budget expiry.
        if row and not force and row['config'] == serialized_config and row['remaining'] == serialized_remaining:
            forecast = json.loads(row['forecast'])
            age = (now - datetime.fromisoformat(row['at'])).total_seconds()
            if age < 30:
                return forecast
        attempts = self._api_attempts(now)
        clock = getattr(self, 'planning_clock', None)
        fresh = clock() if callable(clock) else now
        if policy.local_day(fresh) != policy.local_day(now):
            return self.update_forecast(fresh, reason=reason, force=True)
        now = max(now, fresh)
        costs = estimate_costs(self.db, now, groups_count=getattr(self, 'groups_count', 64))
        local = now.astimezone(policy.LOCAL_TZ)
        reserve = getattr(self, 'groups_count', 64) * sum(local.hour < hour for hour in (8, 20))
        end = max(now, self.calendar_end(now) - timedelta(seconds=120))
        # A gate after midnight cannot grant work on this day.
        forecast = plan_day(now=now, end=end, remaining=remaining,
            action_seconds=costs['action_seconds'], refresh_seconds=costs['refresh_seconds'],
            refresh_requests=costs['refresh_requests'], api_attempts=attempts,
            last_finished_at=self.last_finished_at(), not_before=self.rest_ready_at(now),
            rest_seconds=self.config['batch_rest_seconds'], reserved_reads=reserve)
        forecast['assumptions'] = {**forecast.get('assumptions', {}), 'cost_model': costs,
                                   'day_tail_reserve_seconds': 120, 'analytics_reserved_reads': reserve}
        forecast['reason_trigger'] = reason
        with self.db:
            cur = self.db.execute('INSERT INTO scheduler_adaptive_forecasts(day,at,config,remaining,forecast) VALUES(?,?,?,?,?)',
                (str(policy.local_day(now)), now.isoformat(), serialized_config, serialized_remaining, json.dumps(forecast)))
        forecast.update(revision=cur.lastrowid, generated_at=now.isoformat())
        return forecast

    def claim_batch(self, now, slot):
        self.refresh_policy(now)
        day = str(policy.local_day(now))
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            now = self.fresh_time(now)
            if (str(policy.local_day(now)) != day or self.active_row(now) or self.action_time_closed(now)
                    or not policy.in_work_window(now, self.config)):
                return False
            if self.blocked() or self.get('stop_requested', False) or now < self.rest_ready_at(now):
                return False
            if self.db.execute("SELECT 1 FROM scheduler_jobs WHERE status IN ('dispatching','attempting')").fetchone():
                return False
            if self.db.execute("SELECT 1 FROM scheduler_action_recovery WHERE state='running'").fetchone():
                return False
            if slot != self.next_slot(now):
                return False
            row = self.latest_forecast(now)
            if not row or row['config'] != json.dumps(self.config, sort_keys=True):
                return False
            forecast = json.loads(row['forecast'])
            if not forecast['next_start'] or forecast['next_target'] <= 0 or now < datetime.fromisoformat(forecast['next_start']):
                return False
            remaining = {f'{a}:{k}:{c}': n for (a, k, c), n in self.remaining(now).items()}
            if row['remaining'] != json.dumps(remaining, sort_keys=True):
                return False
            self.db.execute("INSERT INTO scheduler_batches VALUES(?,?,?,NULL,'running')", (day, slot, now.isoformat()))
            members = [int(a) for a in self.config['accounts'] if self.account_enabled(int(a))]
            self.db.execute('INSERT INTO scheduler_adaptive_batches VALUES(?,?,?,?,?,?,?)',
                (day, slot, row['id'], forecast['next_target'], row['config'], forecast['batches'][0]['end'], json.dumps(members)))
        return True

    def target_for(self, now, slot):
        row = self.db.execute('SELECT target_attempts FROM scheduler_adaptive_batches WHERE day=? AND slot=?',
                              (str(policy.local_day(now)), slot)).fetchone()
        if not row:
            raise RuntimeError('No durable adaptive claim')
        return row[0]

    def finish_batch(self, now, slot, status, *, day=None):
        super().finish_batch(now, slot, status, day=day)
        # Persist the actual finish before calculating another prediction. A
        # calculation failure cannot lose the finish or authorize early work.
        self.update_forecast(now, reason='batch_finished', force=True)

    def action_time_closed(self, now):
        boundary = getattr(self, '_batch_boundary', None) or self.calendar_end(now)
        return now >= boundary - timedelta(seconds=120)

    def batch_spacing_ready(self, now, slot):
        active = self.active_row(now)
        if not active or (active['day'], active['slot']) != (str(policy.local_day(now)), slot):
            return False
        return self.fresh_time(now) >= self.rest_ready_at(now)

    def recover(self):
        super().recover()
        now = self.fresh_time(datetime.now(timezone.utc))
        with self.db:
            self.db.execute('''INSERT OR IGNORE INTO scheduler_adaptive_recoveries
              SELECT day,slot,? FROM scheduler_batches WHERE finished_at IS NULL''', (now.isoformat(),))

    def resume_candidate(self, now):
        if not self.table(self.db, 'scheduler_adaptive_days'):
            return None
        if not self.db.execute('SELECT 1 FROM scheduler_adaptive_days WHERE day=?', (str(policy.local_day(now)),)).fetchone():
            return None
        gate = max(now + timedelta(seconds=61), self.rest_ready_at(now))
        return (self.next_slot(now), gate) if gate < self.calendar_end(now) - timedelta(seconds=120) else None

    def adaptive_view(self, now):
        row = self.latest_forecast(now)
        forecast = json.loads(row['forecast']) if row else {}
        if row:
            forecast.update(revision=row['id'], generated_at=row['at'])
        active = self.active_row(now)
        active_view = None
        if active and self.table(self.db, 'scheduler_adaptive_batches'):
            claimed = self.db.execute('SELECT * FROM scheduler_adaptive_batches WHERE day=? AND slot=?', (active['day'], active['slot'])).fetchone()
            if claimed:
                active_view = {'slot': active['slot'], 'target_attempts': claimed['target_attempts'],
                    'started_at': active['started_at'], 'estimated_end': claimed['estimated_end']}
        ready = self.rest_ready_at(now)
        mode = ('working' if active else 'resting' if now < ready else
                'day_complete' if forecast.get('remaining_attempts') == 0 else
                'infeasible' if forecast.get('feasible') is False else
                'waiting_api' if forecast.get('next_start') and now < datetime.fromisoformat(forecast['next_start']) else 'ready')
        activated = None
        if self.table(self.db, 'scheduler_adaptive_days'):
            saved = self.db.execute('SELECT activated_at FROM scheduler_adaptive_days WHERE day=?', (str(policy.local_day(now)),)).fetchone()
            activated = saved[0] if saved else None
        return {'day': str(policy.local_day(now)), 'state': mode, 'rest_seconds': self.config['batch_rest_seconds'],
            'rest_until': ready.isoformat() if now < ready else None, 'active_batch': active_view,
            'forecast': forecast, 'historical_cutover_at': activated}
