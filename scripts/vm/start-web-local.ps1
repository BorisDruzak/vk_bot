param([string]$ProjectRoot=(Join-Path $env:USERPROFILE 'vk_bot'))
$ErrorActionPreference='Stop'
$root=(Resolve-Path -LiteralPath $ProjectRoot).Path
$owner=[Security.Principal.WindowsIdentity]::GetCurrent().Name
icacls (Join-Path $root 'secrets_vk_web.json') /inheritance:r /grant:r ($owner+':(F)') | Out-Null
& (Join-Path $root 'scripts/install_vk_web.ps1') -ProjectRoot $root -LocalBackend -Start
$rule='VKUnifiedWebPanel-LAN'
$firewallPython=(& (Join-Path $root '.venv/Scripts/python.exe') -c "import pathlib,sys; print(pathlib.Path(sys._base_executable).with_name('pythonw.exe'))").Trim()
if (!(Get-NetFirewallRule -Name $rule -ErrorAction SilentlyContinue)) {
 New-NetFirewallRule -Name $rule -DisplayName 'VK Unified LAN HTTP' -Direction Inbound -Action Allow -Protocol TCP -LocalPort 8787 -Program $firewallPython -Profile Any -RemoteAddress 192.168.100.0/24,10.10.10.0/24 | Out-Null
} else {
 Set-NetFirewallRule -Name $rule -Program $firewallPython -Profile Any -RemoteAddress 192.168.100.0/24,10.10.10.0/24 -Enabled True | Out-Null
}
# Windows can create a per-program TCP deny when its first desktop prompt is
# dismissed. That deny overrides the explicit LAN/8787 allow above. Keep UDP
# and the default inbound policy; disable only the exact new runtime TCP deny.
Get-NetFirewallApplicationFilter -Program $firewallPython | Get-NetFirewallRule |
 Where-Object { $_.Enabled -eq 'True' -and $_.Direction -eq 'Inbound' -and $_.Action -eq 'Block' } |
 ForEach-Object {
  $port=$_ | Get-NetFirewallPortFilter
  if ($port.Protocol -eq 'TCP') { $_ | Disable-NetFirewallRule | Out-Null }
 }
Get-ScheduledTask -TaskName VKUnifiedWebPanel,VKUnifiedDailyWorker | Format-List TaskName,State
