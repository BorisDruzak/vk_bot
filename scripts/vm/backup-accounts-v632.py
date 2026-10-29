"""Private pre-update snapshot; never export cookies, secrets or database rows."""
from pathlib import Path
from datetime import datetime,timezone
import json
import shutil
import sqlite3

root=Path(__file__).resolve().parents[2]
target=Path.home()/'vk_backups'/('before-add-any-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'))
target.mkdir(parents=True,exist_ok=False)
databases=0
for source in root.glob('*.sqlite3'):
    with sqlite3.connect(source.resolve().as_uri()+'?mode=ro',uri=True) as incoming,sqlite3.connect(target/source.name) as saved:
        incoming.backup(saved)
        if saved.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise RuntimeError('Backup integrity failed')
    databases+=1
files=0
for source in list(root.glob('vk_*.py'))+[root/'vk_account_registry.json',root/'vk_account_bindings.json']+list(root.glob('.vk-authentication-*.json'))+list(root.glob('.vk-login-*.json')):
    if source.is_file():shutil.copy2(source,target/source.name);files+=1
shutil.copytree(root/'web',target/'web')
print(json.dumps({'backup':str(target),'databases':databases,'files':files,'integrity':'ok'}))
