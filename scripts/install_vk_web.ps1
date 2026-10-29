param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$Python = '',
    [string]$RemoteHost = 'vk-bot@192.168.100.46',
    [string]$RemoteRoot = 'C:/Users/vk-bot/vk_bot',
    [string]$RemotePython = '',
    [string]$IdentityFile = (Join-Path $env:USERPROFILE '.ssh/id_rsa_vk_bot'),
    [int]$Port = 8787,
    [string]$TaskName = 'VKUnifiedWebPanel',
    [switch]$AllowPrivateLan,
    [switch]$LocalBackend,
    [switch]$Start
)
$ErrorActionPreference = 'Stop'
$ProjectRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path
if (!$Python) {
    $candidate = Join-Path $ProjectRoot '.venv/Scripts/pythonw.exe'
    if (Test-Path -LiteralPath $candidate) { $Python = $candidate }
    else { $Python = (Get-Command pythonw.exe -ErrorAction Stop).Source }
}
$Python = (Resolve-Path -LiteralPath $Python).Path
if (!(Test-Path -LiteralPath (Join-Path $ProjectRoot 'secrets_vk_web.json'))) {
    throw 'Initialize credentials first: python vk_web.py --init-auth --username operator'
}
if (!$LocalBackend -and !(Test-Path -LiteralPath $IdentityFile)) { throw 'SSH identity file is missing.' }
if ($Port -lt 1 -or $Port -gt 65535) { throw 'Invalid port.' }
# Native Windows argv quoting; no PowerShell command constructed from operator fields.
function Quote-Argument([string]$Value) {
    if ($Value.Contains('"') -or $Value.Contains("`n") -or $Value.Contains("`r")) { throw 'Unsupported quote or newline in argument.' }
    return '"' + $Value.TrimEnd('\') + '"'
}
$webArguments = @((Join-Path $ProjectRoot 'vk_web.py'), '--host', '0.0.0.0', '--port', "$Port")
if ($LocalBackend) { $webArguments += '--local-backend' }
else {
    $webArguments += @('--remote-host', $RemoteHost, '--remote-root', $RemoteRoot, '--identity-file', $IdentityFile)
    if ($RemotePython) { $webArguments += @('--remote-python', $RemotePython) }
}
$argumentString = ($webArguments | ForEach-Object { Quote-Argument $_ }) -join ' '
$user = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$action = New-ScheduledTaskAction -Execute $Python -Argument $argumentString -WorkingDirectory $ProjectRoot
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Description 'Authenticated VK Unified LAN panel; execution stays on Windows VM.' -Force | Out-Null
if ($AllowPrivateLan) {
    $ruleName = 'VKUnifiedWebPanel-PrivateLAN'
    if (Get-NetFirewallRule -Name $ruleName -ErrorAction SilentlyContinue) {
        Set-NetFirewallRule -Name $ruleName -Enabled True -Profile Domain,Private -RemoteAddress LocalSubnet -Action Allow | Out-Null
        Get-NetFirewallRule -Name $ruleName | Get-NetFirewallPortFilter | Set-NetFirewallPortFilter -Protocol TCP -LocalPort $Port | Out-Null
        Get-NetFirewallRule -Name $ruleName | Get-NetFirewallApplicationFilter | Set-NetFirewallApplicationFilter -Program $Python | Out-Null
    } else {
        New-NetFirewallRule -Name $ruleName -DisplayName 'VK Unified panel (domain/private LAN)' -Direction Inbound -Action Allow -Protocol TCP -LocalPort $Port -Program $Python -Profile Domain,Private -RemoteAddress LocalSubnet | Out-Null
    }
}
if ($Start) { Start-ScheduledTask -TaskName $TaskName }
Write-Output "Registered $TaskName. Starts at logon; current start requested: $([bool]$Start). HTTP port $Port."
