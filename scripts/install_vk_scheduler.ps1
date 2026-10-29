param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    [switch]$StatusOnly,
    [switch]$ExecuteActions
)
$ErrorActionPreference = 'Stop'
$taskName = 'VKUnifiedDailyWorker'
$projectPath = (Resolve-Path -LiteralPath $ProjectRoot).Path
$entryPath = Join-Path $projectPath 'vk_unified.py'
if (-not (Test-Path -LiteralPath $entryPath -PathType Leaf)) { throw 'Project CLI is missing' }
$existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($StatusOnly) {
    if ($existing) {
        $existing | Select-Object TaskName, State
        Get-ScheduledTaskInfo -TaskName $taskName | Select-Object LastRunTime, LastTaskResult, NextRunTime
    }
    exit
}
$pythonExe = (Get-Command python -CommandType Application | Select-Object -First 1).Source
$pythonWindowless = Join-Path (Split-Path -Parent $pythonExe) 'pythonw.exe'
if (-not (Test-Path -LiteralPath $pythonWindowless -PathType Leaf)) { throw 'pythonw.exe is required' }
if ($existing -and $existing.Actions[0].Arguments -notlike ('*' + $entryPath + '*')) {
    throw 'Task name belongs to another project; no changes made'
}
if ($existing -and $existing.State -eq 'Running') {
    Disable-ScheduledTask -TaskName $taskName | Out-Null
    & $pythonExe -X utf8 $entryPath --scheduler-stop
    for ($waitIndex = 0; $waitIndex -lt 30; $waitIndex++) {
        if ((Get-ScheduledTask -TaskName $taskName).State -ne 'Running') { break }
        Start-Sleep -Seconds 1
    }
    if ((Get-ScheduledTask -TaskName $taskName).State -eq 'Running') {
        throw 'Existing worker is finishing its operation. Its future triggers are disabled; retry after it stops.'
    }
}
$identityName = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$workerMode = if ($ExecuteActions) { '--confirm' } else { '--dry-run' }
$action = New-ScheduledTaskAction -Execute $pythonWindowless -Argument ('-X utf8 -u "' + $entryPath + '" --scheduler-daemon ' + $workerMode) -WorkingDirectory $projectPath
$triggers = @(
    (New-ScheduledTaskTrigger -AtLogOn -User $identityName),
    (New-ScheduledTaskTrigger -Daily -At '08:55')
)
$principal = New-ScheduledTaskPrincipal -UserId $identityName -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $triggers -Principal $principal -Settings $settings -Description 'VK local worker. Daily limits and09:00–19:00 YEKT are enforced by Python, no credentials in task arguments.' -Force | Select-Object TaskName, State
Start-ScheduledTask -TaskName $taskName
Write-Output 'VK worker task installed and started. Chromium requires this signed-in interactive user session.'
