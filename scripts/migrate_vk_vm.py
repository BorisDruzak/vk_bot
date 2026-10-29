"""Explicit deployment helper: copy source, then stopped-state snapshots and profiles.

Uses the separately provisioned SSH key and pinned known_hosts. No credentials
or cookie values are printed. Original Windows state is retained.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import zipfile
import paramiko

ROOT=Path(__file__).resolve().parents[1]
REMOTE='/C:/Users/vk-bot/vk_bot'


def connect():
    client=paramiko.SSHClient()
    client.load_system_host_keys(str(Path.home()/'.ssh/known_hosts'))
    client.connect('192.168.100.46',username='vk-bot',
                   key_filename=str(Path.home()/'.ssh/id_rsa_vk_bot'),
                   look_for_keys=False,allow_agent=False,timeout=15)
    return client


def put_tree(sftp,files):
    directories=set()
    for path in files:
        relative=path.relative_to(ROOT).as_posix()
        target=REMOTE+'/'+relative
        parent=target.rsplit('/',1)[0]
        if parent not in directories:
            parts=parent[len(REMOTE):].strip('/').split('/')
            current=REMOTE
            for part in parts:
                if not part:continue
                current+='/'+part
                try:sftp.mkdir(current)
                except OSError:pass
            directories.add(parent)
        sftp.put(str(path),target)
    print('Source files transferred:',len(files),flush=True)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',action='store_true')
    parser.add_argument('--state',action='store_true')
    parser.add_argument('--profiles',action='store_true')
    parser.add_argument('--backup',type=Path)
    args=parser.parse_args()
    if args.state or args.profiles:
        status=subprocess.run(['powershell','-NoProfile','-Command',
            "(Get-ScheduledTask -TaskName VKUnifiedDailyWorker).State.ToString()"],capture_output=True,text=True,check=True).stdout.strip()
        if status!='Disabled':raise RuntimeError('Local worker must be disabled and gracefully stopped for migration')
        processes=subprocess.run(['powershell','-NoProfile','-Command',
            "Get-CimInstance Win32_Process | Where-Object {$_.Name -in @('python.exe','pythonw.exe') -and $_.CommandLine -match 'vk_unified.py.*--scheduler-daemon'} | Select-Object -ExpandProperty ProcessId"],capture_output=True,text=True,check=True).stdout.strip()
        if processes:raise RuntimeError('A local worker is still running')
    client=connect();sftp=client.open_sftp()
    try:
        if args.source:
            files=[p for p in ROOT.iterdir() if p.is_file() and (p.suffix in ('.py','.md') or p.name in ('requirements.txt','.gitignore','vk_groups.json','vk_automation.json'))]
            for folder in ('docs','tests','scripts','web'):
                files.extend(p for p in (ROOT/folder).rglob('*') if p.is_file() and '__pycache__' not in p.parts and p.suffix in ('.py','.ps1','.md','.html','.css','.js','.json','.svg'))
            put_tree(sftp,files)
        if args.state:
            if args.backup is None:raise ValueError('--backup required')
            args.backup.mkdir(parents=True,exist_ok=True)
            for path in ROOT.glob('*.sqlite3'):
                saved=args.backup/path.name
                source=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)
                target=sqlite3.connect(saved)
                try:
                    source.backup(target)
                    if target.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise RuntimeError('Snapshot integrity failed')
                finally:target.close();source.close()
                try:sftp.stat(REMOTE+'/'+path.name)
                except FileNotFoundError:pass
                else:raise RuntimeError('Remote database exists; refusing automatic overwrite')
                sftp.put(str(saved),REMOTE+'/'+path.name)
                print('Snapshot transferred:',path.name,flush=True)
            extras=[ROOT/'api_keys.txt',ROOT/'vk_account_bindings.json',ROOT/'vk_account_registry.json']
            extras.extend(ROOT.glob('vk_*results*.csv'))
            extras.extend(ROOT.glob('*.log'))
            for path in extras:
                if path.is_file():sftp.put(str(path),REMOTE+'/'+path.name)
            print('Private configuration/reports transferred over SSH; values suppressed',flush=True)
        if args.profiles:
            if args.backup is None:raise ValueError('--backup required')
            for profile in ROOT.glob('.vk-local-profile*'):
                if not profile.is_dir():continue
                archive=args.backup/(profile.name+'.zip')
                skipped={'SingletonLock','SingletonCookie','SingletonSocket'}
                count=0
                with zipfile.ZipFile(archive,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=1) as output:
                    for path in profile.rglob('*'):
                        if path.is_file() and path.name not in skipped:
                            output.write(path,path.relative_to(ROOT));count+=1
                remote_archive=REMOTE+'/'+archive.name
                sftp.put(str(archive),remote_archive)
                command=f'powershell -NoProfile -NonInteractive -Command "Expand-Archive -LiteralPath \'C:/Users/vk-bot/vk_bot/{archive.name}\' -DestinationPath \'C:/Users/vk-bot/vk_bot\'; Remove-Item -LiteralPath \'C:/Users/vk-bot/vk_bot/{archive.name}\'"'
                _,out,err=client.exec_command(command);out.read();error=err.read()
                if out.channel.recv_exit_status():raise RuntimeError('Profile expansion failed')
                print('Full profile transferred:',profile.name,'files',count,'archive SHA256',hashlib.file_digest(archive.open('rb'),'sha256').hexdigest(),flush=True)
    finally:sftp.close();client.close()


if __name__=='__main__':main()
