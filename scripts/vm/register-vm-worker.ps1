param(
    [string]$ProjectRoot = (Join-Path $env:USERPROFILE 'vk_bot'),
    [string]$TaskName = 'VKUnifiedDailyWorker'
)
# Registration only. Enabling or starting the worker is a separate cutover step.
$ErrorActionPreference = 'Stop'
$root = (Resolve-Path -LiteralPath $ProjectRoot).Path
$entryPath = Join-Path $root 'vk_unified.py'
$pythonw = Join-Path $root '.venv\Scripts\pythonw.exe'
foreach ($path in @($entryPath, $pythonw)) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw ('Required deployment file missing: ' + $path) }
}
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) { throw 'Task already exists; inspect it explicitly before replacing it' }
$user = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$action = New-ScheduledTaskAction -Execute $pythonw -Argument ('-X utf8 -u "' + $entryPath + '" --scheduler-daemon --confirm') -WorkingDirectory $root
$triggers = @((New-ScheduledTaskTrigger -AtLogOn -User $user), (New-ScheduledTaskTrigger -Daily -At '08:55'))
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -Disable -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $triggers -Principal $principal -Settings $settings -Description 'Prepared disabled for controlled cutover. Interactive user session required; Python enforces quotas and YEKT window.' | Select-Object TaskName, State
Write-Output 'Task registered disabled. No worker was started.'
