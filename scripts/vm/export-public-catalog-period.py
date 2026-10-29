"""Read-only export of public API IDs/dates, never actor states or attempts."""
from contextlib import closing
from pathlib import Path
import json,sqlite3,sys

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from vk_analytics import stamp
path=ROOT/'vk_batch.sqlite3'
with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=3)) as db:
    db.row_factory=sqlite3.Row
    fields={r['name'] for r in db.execute('PRAGMA table_info(api_seen_posts)')}
    if not {'group_id','post_id','published_at','seen_at','valid'}<=fields:raise RuntimeError('Public catalog schema missing')
    rows=[dict(r) for r in db.execute('''SELECT group_id,post_id,published_at,seen_at,valid
              FROM api_seen_posts WHERE published_at>=? AND published_at<? ORDER BY group_id,post_id''',
              (stamp('2026-10-08'),stamp('2026-10-15')))]
print(json.dumps({'source':'api_seen_posts read-only','posts':rows}))
