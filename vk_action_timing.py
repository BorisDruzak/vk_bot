"""Buffered, non-authorizing action spans; no identifiers or exception text."""
from __future__ import annotations

from contextlib import contextmanager, nullcontext
import math
import statistics
import time

import vk_diagnostics as diagnostics


STAGES=('browser_open','source_navigation','source_guard','community_prepare',
        'click','confirmation','cooldown')


class StageTimings:
    def __init__(self, *, monotonic=None):
        self._clock=monotonic if monotonic is not None else time.perf_counter
        self._samples=[]

    @property
    def samples(self):
        return tuple(dict(sample) for sample in self._samples)

    @contextmanager
    def measure(self, stage):
        # An observer must never affect execution, including broken clocks.
        try:started=self._clock() if stage in STAGES else None
        except Exception:started=None
        outcome='failed'
        try:
            yield
            outcome='complete'
        finally:
            try:
                if started is not None:
                    elapsed=self._clock()-started
                    if math.isfinite(elapsed) and elapsed>=0:
                        self._samples.append(dict(timing_stage=stage,
                            duration_ms=round(elapsed*1000),timing_outcome=outcome))
            except Exception:pass

    def flush(self, callback):
        samples,self._samples=self._samples,[]
        for sample in samples:
            try:diagnostics.emit(callback,'stage_timing',**sample)
            except Exception:pass


def measure(timings, stage):
    """Opt in only for a real timer, never a dynamically manufactured mock."""
    return timings.measure(stage) if isinstance(timings,StageTimings) else nullcontext()


def summarize(samples):
    """Completed span distribution (nearest-rank p95); failures stay separate."""
    values={stage:[] for stage in STAGES};failed=dict.fromkeys(STAGES,0)
    for sample in samples:
        if not isinstance(sample,dict):continue
        stage=sample.get('timing_stage');duration=sample.get('duration_ms')
        if type(stage) is not str or stage not in values or type(duration) is not int or duration<0:continue
        if sample.get('timing_outcome')=='complete':values[stage].append(duration)
        elif sample.get('timing_outcome')=='failed':failed[stage]+=1
    return {stage:dict(samples=len(ordered),failed_samples=failed[stage],
        median_ms=statistics.median(ordered) if ordered else None,
        p95_ms=ordered[math.ceil(len(ordered)*.95)-1] if ordered else None)
        for stage in STAGES for ordered in [sorted(values[stage])]}
