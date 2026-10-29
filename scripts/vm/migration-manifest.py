"""Read-only logical SQLite digest: only table names, counts and hashes emitted."""
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

root=Path(sys.argv[1]) if len(sys.argv)>1 else Path(__file__).resolve().parents[2]
result={}
for path in sorted(root.glob('*.sqlite3')):
    db=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)
    try:
        tables={}
        for (table,) in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"):
            quote='"'+table.replace('"','""')+'"'
            columns=db.execute('PRAGMA table_info('+quote+')').fetchall()
            order=','.join(str(i+1) for i in range(len(columns)))
            digest=hashlib.sha256();count=0
            for row in db.execute('SELECT * FROM '+quote+' ORDER BY '+order):
                digest.update(json.dumps(row,ensure_ascii=True,default=lambda v:v.hex()).encode())
                digest.update(b'\n');count+=1
            tables[table]={'rows':count,'sha256':digest.hexdigest()}
        result[path.name]={'integrity':db.execute('PRAGMA integrity_check').fetchone()[0],'tables':tables}
    finally:db.close()
print(json.dumps(result))
