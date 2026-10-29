param(
    [string]$ProjectRoot = (Join-Path $env:USERPROFILE 'vk_bot'),
    [string]$PythonVersion = '3.13.17',
    [string]$TimeZoneId = 'Ekaterinburg Standard Time'
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
Set-TimeZone -Id $TimeZoneId
$root = [IO.Path]::GetFullPath($ProjectRoot)
New-Item -ItemType Directory -Path $root -Force | Out-Null
$downloadRoot = Join-Path $env:LOCALAPPDATA 'VKBotSetup'
New-Item -ItemType Directory -Path $downloadRoot -Force | Out-Null
$pythonRoot = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python313'
$pythonExe = Join-Path $pythonRoot 'python.exe'
if (-not (Test-Path -LiteralPath $pythonExe -PathType Leaf)) {
    $installer = Join-Path $downloadRoot ('python-' + $PythonVersion + '-amd64.exe')
    Invoke-WebRequest -UseBasicParsing -Uri ('https://www.python.org/ftp/python/' + $PythonVersion + '/python-' + $PythonVersion + '-amd64.exe') -OutFile $installer
    $signature = Get-AuthenticodeSignature -LiteralPath $installer
    if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -notmatch 'Python Software Foundation') { throw 'Python installer signature validation failed' }
    $process = Start-Process -FilePath $installer -ArgumentList @('/quiet', 'InstallAllUsers=0', 'Include_launcher=0', 'Include_test=0', 'Include_pip=1', 'PrependPath=1', ('TargetDir="' + $pythonRoot + '"')) -WindowStyle Hidden -Wait -PassThru
    if ($process.ExitCode -notin @(0,3010)) { throw ('Python install exit code ' + $process.ExitCode) }
}
$chromeExe = Join-Path $env:ProgramFiles 'Google\Chrome\Application\chrome.exe'
if (-not (Test-Path -LiteralPath $chromeExe -PathType Leaf)) {
    $msi = Join-Path $downloadRoot 'GoogleChromeStandaloneEnterprise64.msi'
    Invoke-WebRequest -UseBasicParsing -Uri 'https://dl.google.com/dl/chrome/install/googlechromestandaloneenterprise64.msi' -OutFile $msi
    $signature = Get-AuthenticodeSignature -LiteralPath $msi
    if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -notmatch 'Google LLC') { throw 'Chrome installer signature validation failed' }
    $process = Start-Process -FilePath 'msiexec.exe' -ArgumentList @('/i', ('"' + $msi + '"'), '/qn', '/norestart') -WindowStyle Hidden -Wait -PassThru
    if ($process.ExitCode -notin @(0,3010)) { throw ('Chrome install exit code ' + $process.ExitCode) }
}
if (-not (Test-Path -LiteralPath $chromeExe)) { throw 'Chrome installation was not found' }
$venvPython = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $venvPython)) {
    & $pythonExe -m venv (Join-Path $root '.venv')
    if ($LASTEXITCODE -ne 0) { throw 'venv creation failed' }
}
& $venvPython -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw 'pip update failed' }
$requirements = Join-Path $root 'requirements.txt'
if (-not (Test-Path -LiteralPath $requirements)) { throw 'Deploy project requirements.txt before installing dependencies' }
& $venvPython -m pip install -r $requirements
if ($LASTEXITCODE -ne 0) { throw 'Project dependency install failed' }
& $venvPython -m playwright install chromium
if ($LASTEXITCODE -ne 0) { throw 'Playwright browser install failed' }
& $venvPython -m pip check
if ($LASTEXITCODE -ne 0) { throw 'Dependency check failed' }
& $venvPython --version
if ($LASTEXITCODE -ne 0) { throw 'Python version check failed' }
& $venvPython -m playwright --version
if ($LASTEXITCODE -ne 0) { throw 'Playwright version check failed' }
Get-Item -LiteralPath $chromeExe | Select-Object FullName, @{N='Version';E={$_.VersionInfo.ProductVersion}}
Write-Output ('VenvPython=' + $venvPython)
Write-Output 'Runtime preparation complete; no VK worker was started.'
