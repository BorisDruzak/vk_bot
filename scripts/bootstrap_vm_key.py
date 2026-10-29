"""One-time password bootstrap; no credential is stored or printed."""
import getpass
from pathlib import Path
import paramiko

root = Path.home() / '.ssh'
client = paramiko.SSHClient()
client.load_system_host_keys(str(root / 'known_hosts'))
client.connect('192.168.100.46', username='vk-bot',
               password=getpass.getpass('VM password: '), look_for_keys=False,
               allow_agent=False, timeout=15)
sftp = client.open_sftp()
print('Remote home:', sftp.normalize('.'))
for command in ['whoami', 'cmd /c ver', 'powershell -NoProfile -Command "Get-Content C:/ProgramData/ssh/sshd_config | Select-Object -Last 14"']:
    _, out, err = client.exec_command(command)
    print(out.read().decode('utf-8', 'replace'))
    print(err.read().decode('utf-8', 'replace'))
pub = (root / 'id_rsa_vk_bot.pub').read_text().strip()
target = 'C:/ProgramData/ssh/administrators_authorized_keys'
try:
    with sftp.file(target, 'r') as handle:
        old = handle.read().decode('utf-8')
except FileNotFoundError:
    old = ''
if pub not in old:
    with sftp.file(target, 'a') as handle:
        handle.write(('\n' if old and not old.endswith('\n') else '') + pub + '\n')
_, out, err = client.exec_command('icacls C:/ProgramData/ssh/administrators_authorized_keys /inheritance:r /grant "*S-1-5-32-544:F" /grant "SYSTEM:F"')
print(out.read().decode('utf-8', 'replace'))
print(err.read().decode('utf-8', 'replace'))
client.close()
