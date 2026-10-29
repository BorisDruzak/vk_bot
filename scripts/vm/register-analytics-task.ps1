param(
    [string]$ProjectRoot = (Join-Path $env:USERPROFILE 'vk_bot')
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$root = (Resolve-Path -LiteralPath $ProjectRoot).Path
$taskName = 'VKOMSUAnalytics'
if ((Get-TimeZone).Id -ne 'Ekaterinburg Standard Time') { throw 'Expected VM time zone: Ekaterinburg Standard Time' }
if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) { throw 'Analytics task already exists; inspect before replacing' }
$entry = Join-Path $root 'vk_analytics_scheduled.py'
$python = Join-Path $root '.venv\Scripts\pythonw.exe'
foreach ($path in @($entry, $python, (Join-Path $root 'analytics_local\observation-from-2026-10-14.json'), (Join-Path $root 'analytics_local\observation-from-2026-10-14\registry.json'))) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw 'Analytics deployment incomplete' }
}
$user = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$action = New-ScheduledTaskAction -Execute $python -Argument ('-X utf8 -u "' + $entry + '"') -WorkingDirectory $root
$triggers = @((New-ScheduledTaskTrigger -Daily -At '08:00'), (New-ScheduledTaskTrigger -Daily -At '20:00'))
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType S4U -RunLevel Limited
# Do not force-kill a reader while it owns the worker's conservative file guard.
# Python checks the 25-minute deadline before every bounded network read and
# leaves the shared guards through their normal finally blocks.
$settings = New-ScheduledTaskSettingsSet -Disable -MultipleInstances IgnoreNew -Priority 8 -ExecutionTimeLimit (New-TimeSpan -Minutes 30) -DisallowHardTerminate -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $triggers -Principal $principal -Settings $settings -Description 'Public OMSU statistics only, 08/20 YEKT; fixed14Oct start; worker priority; no Codex or interactive desktop required.' | Out-Null
Get-ScheduledTask -TaskName $taskName | Select-Object TaskName,State,@{n='LogonType';e={$_.Principal.LogonType}},@{n='Priority';e={$_.Settings.Priority}},@{n='CatchUp';e={$_.Settings.StartWhenAvailable}}
