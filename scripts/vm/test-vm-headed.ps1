param([string]$ProjectRoot = (Join-Path $env:USERPROFILE 'vk_bot'))
# Neutral, isolated browser smoke test only. No VK profile, database or worker.
$ErrorActionPreference = 'Stop'
$root = (Resolve-Path -LiteralPath $ProjectRoot).Path
$python = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) { throw 'Project venv is missing' }
$testRoot = Join-Path $env:TEMP ('vk-headed-smoke-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $testRoot | Out-Null
$testScript = Join-Path $testRoot 'smoke.py'
$resultFile = Join-Path $testRoot 'result.json'
$code = @'
import ctypes
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from playwright.sync_api import sync_playwright

result_file = Path(sys.argv[1])
result = {"pid": os.getpid(), "user": os.environ.get("USERNAME"), "headless": False}
session = ctypes.c_ulong()
if not ctypes.windll.kernel32.ProcessIdToSessionId(os.getpid(), ctypes.byref(session)):
    raise RuntimeError("Cannot resolve process session")
result["session_id"] = session.value
if session.value == 0:
    raise RuntimeError("Headed smoke must run in interactive user session")
try:
    with sync_playwright() as pw:
        result["browsers"] = []
        for channel in ("chrome", None):
            with tempfile.TemporaryDirectory(prefix="vk-neutral-profile-") as profile:
                options = {"headless": False, "user_data_dir": profile}
                if channel:
                    options["channel"] = channel
                context = pw.chromium.launch_persistent_context(**options)
                try:
                    page = context.new_page()
                    page.goto("about:blank")
                    page.set_content("<html><head><title>VM neutral browser smoke</title></head><body><h1>VM browser ready</h1></body></html>")
                    assert page.title() == "VM neutral browser smoke"
                    assert page.locator("h1").inner_text() == "VM browser ready"
                    time.sleep(3)
                    result["browsers"].append({"channel": channel or "bundled-chromium", "version": context.browser.version, "title": page.title(), "url": page.url})
                finally:
                    context.close()
    result["ok"] = True
except Exception as exc:
    result["ok"] = False
    result["error_type"] = type(exc).__name__
    raise
finally:
    result_file.write_text(json.dumps(result, indent=2), encoding="utf-8")
'@
[IO.File]::WriteAllText($testScript, $code, [Text.UTF8Encoding]::new($false))
$taskName = 'VKVMNeutralSmoke-' + [Guid]::NewGuid().ToString('N')
$user = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$action = New-ScheduledTaskAction -Execute $python -Argument ('"' + $testScript + '" "' + $resultFile + '"') -WorkingDirectory $testRoot
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 2)
Register-ScheduledTask -TaskName $taskName -Action $action -Principal $principal -Settings $settings | Out-Null
try {
    Start-ScheduledTask -TaskName $taskName
    for ($index = 0; $index -lt 90; $index++) {
        if (Test-Path -LiteralPath $resultFile) { break }
        Start-Sleep -Seconds 1
    }
    if (-not (Test-Path -LiteralPath $resultFile)) { throw 'Interactive smoke did not produce a result within 90 seconds; its task was not forcibly stopped' }
    $result = Get-Content -LiteralPath $resultFile -Raw | ConvertFrom-Json
    $result | ConvertTo-Json -Depth 6
    if (-not $result.ok) { throw 'Headed smoke failed' }
} finally {
    # Remove only our task; never stop a user process or the VK worker.
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
}
Write-Output ('Smoke evidence retained at ' + $resultFile)
