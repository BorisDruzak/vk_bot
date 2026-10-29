"""Explicit synthetic schedules for isolated orchestration tests."""
import json
from datetime import timedelta
import vk_schedule_policy as policy


def save_plan(state,now,*,slot=1,minutes=20):
    slots=[now+timedelta(seconds=(i-slot)*minutes*60-(i-slot)*(i-slot-1)//2)
           for i in range(slot+3)]
    state.db.execute('INSERT OR REPLACE INTO schedule_days VALUES(?,?,?)',
        (str(policy.local_day(now)),json.dumps(state.config,sort_keys=True),json.dumps([t.isoformat() for t in slots])))
    state.db.commit()
