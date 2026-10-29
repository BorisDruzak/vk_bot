"""Protected history hashes only. Does not change worker, locks or databases."""
from contextlib import closing
from datetime import datetime,timezone
import hashlib,json,sqlite3
from pathlib import Path

root=Path(__file__).resolve().parents[2]
result={};meta_hashes={}
for path in [root/'vk_batch.sqlite3',*root.glob('vk_batch_account_*.sqlite3'),root/'vk_scheduler.sqlite3']:
    if not path.is_file():continue
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=3)) as db:
        tables={}
        for (table,) in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
            if table.startswith(('sqlite_','api_')) or table in ('read_navigation_pacing','scheduler_diagnostics'):continue
            name='"'+table.replace('"','""')+'"'
            columns=db.execute('PRAGMA table_info('+name+')').fetchall()
            order=','.join(str(i+1) for i in range(len(columns)))
            digest=hashlib.sha256();count=0
            for row in db.execute('SELECT * FROM '+name+' ORDER BY '+order):
                digest.update(json.dumps(row,ensure_ascii=True,default=str).encode());count+=1
            tables[table]=dict(rows=count,sha256=digest.hexdigest())
            if table=='scheduler_meta':
                meta_hashes={str(row[0]):hashlib.sha256(json.dumps(row,ensure_ascii=True,default=str).encode()).hexdigest()
                             for row in db.execute('SELECT * FROM '+name)}
        result[path.name]=tables
print(json.dumps(dict(at=datetime.now(timezone.utc).isoformat(),protected=result,meta_key_hashes=meta_hashes)))
