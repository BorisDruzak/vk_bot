"""Read-only compact acceptance facts; no identity, token or DOM output."""
import json
import hashlib
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
import vk_accounts as accounts
import vk_control as control
import vk_unified as app

snapshot=control.snapshot()
with control.readonly(accounts.ROOT/'vk_scheduler.sqlite3') as db:
    inflight=db.execute("SELECT COUNT(*) FROM scheduler_jobs WHERE status IN ('dispatching','attempting')").fetchone()[0]
counts={}
history={}
for account in accounts.account_numbers():
    with control.readonly(accounts.database_for_account(account)) as db:
        counts[str(account)]=db.execute('SELECT COUNT(*) FROM action_attempts').fetchone()[0]
        rows=db.execute("SELECT * FROM posts WHERE like_status!='pending' OR repost_status!='pending' ORDER BY group_id,post_id").fetchall()
        history[str(account)]={'rows':len(rows),'sha256':hashlib.sha256(json.dumps(rows,ensure_ascii=True).encode()).hexdigest()}
groups=snapshot['groups']
job=snapshot['statistics_refresh']
scheduler=snapshot['scheduler']
print(json.dumps({'version':app.VERSION,'inflight':inflight,'attempts':counts,'reaction_history':history,
    'accounts':[{'account':a['account'],'enabled':a['enabled'],
                 'login':a['login']['state'],'authentication':a['authentication']['state'],
                 'fresh':a['authentication']['fresh']} for a in snapshot['accounts']],
    'groups':len(groups),'sample_posts':sum(g['posts'] for g in groups),
    'metrics':{metric:{'known_posts':sum(g.get(metric+'_known_posts',0) for g in groups),
                       'groups_with_data':sum(g.get(metric+'_known_posts',0)>0 for g in groups)}
               for metric in ('like_count','comment_count','repost_count','view_count')},
    'statistics_refresh':{k:job.get(k) for k in ('state','groups_done','total','requests','failures','rejected_posts','reason')},
    'scheduler':{k:scheduler.get(k) for k in ('paused','active_uncertain','isolated_posts')},
    'heartbeat':{k:scheduler.get('heartbeat',{}).get(k) for k in ('phase','mode','pid','at')}
},ensure_ascii=True))
