#Requires -RunAsAdministrator
<#
Installs, upgrades or removes BACpack as a Windows service (WinSW).

  powershell -ExecutionPolicy Bypass -File install.ps1 [-Python PATH] [-Port N] [-LoginUser NAME | -NoLogin]
  powershell -ExecutionPolicy Bypass -File install.ps1 -Uninstall [-Purge]

-LoginUser NAME creates the web login: the password comes from the environment
variable BACPACK_LOGIN_PASSWORD, else it is asked twice; never a parameter.
-NoLogin records "run without login". With neither, a new install asks in the
browser and an upgrade keeps its current setting.

Bundle mode: a python\ folder next to this script (the release zip) is used
as is, offline. Source mode (an archive or clone): a Python 3.13+ installed
for all users.
#>
param(
    [string]$Python,
    [int]$Port = 0,
    [string]$LoginUser,
    [switch]$NoLogin,
    [switch]$Uninstall,
    [switch]$Purge
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$ServiceName = 'BACpack'
$Account = "NT SERVICE\$ServiceName"
$InstallDir = Join-Path $env:ProgramFiles 'BACpack'
$DataDir = Join-Path $env:ProgramData 'BACpack'
$WinswExe = Join-Path $InstallDir 'bacpack-service.exe'
# Written after a start that held. A registered service says only that an
# install began, not that it ever ran (a refused first start, a failed login step).
$StartedMarker = Join-Path $InstallDir 'started.marker'
$WinswXml = Join-Path $InstallDir 'bacpack-service.xml'
$WinswUrl = 'https://github.com/winsw/winsw/releases/download/v2.12.0/WinSW-x64.exe'
$WinswSha256 = '05B82D46AD331CC16BDC00DE5C6332C1EF818DF8CEEFCD49C726553209B3A0DA'
$BacnetPort = 47808
$FirewallGroup = 'BACpack'
$Here = $PSScriptRoot
$BundleMode = Test-Path (Join-Path $Here 'python\python.exe')
$RepoRoot = if ($BundleMode) { $Here } else { Split-Path $Here -Parent }

# A failed upgrade (a pip or network error) mustn't leave a working install
# stopped: Fail and the trap below start it again, on the previous code.
$RestoreOnFail = $false
function Restore-Previous {
    if (-not $script:RestoreOnFail) { return }
    Write-Host 'error: the upgrade failed; starting the previous install again.' -ForegroundColor Red
    try { Start-Service $ServiceName -ErrorAction Stop }
    catch { Write-Host "error: it didn't start either; fix the cause above and run the installer again." -ForegroundColor Red }
}
function Fail([string]$Message) { Write-Host "error: $Message" -ForegroundColor Red; Restore-Previous; exit 1 }
trap { Write-Host "error: $_" -ForegroundColor Red; Restore-Previous; exit 1 }

function Remove-Service-Registration {
    $svc = Get-Service $ServiceName -ErrorAction SilentlyContinue
    if (-not $svc) { return }
    if ($svc.Status -ne 'Stopped') { Stop-Service $ServiceName; $svc.WaitForStatus('Stopped', '00:02:00') }
    if (Test-Path $WinswExe) { & $WinswExe uninstall | Out-Null } else { sc.exe delete $ServiceName | Out-Null }
}

if ($Uninstall) {
    Remove-Service-Registration
    # Devices left by a crashed web process outlive the service, keep serving
    # and lock the install's files.
    $leftover = @(Get-Process -IncludeUserName | Where-Object { $_.UserName -eq $Account })
    if ($leftover) {
        $leftover | Stop-Process -Force
        $leftover | Wait-Process -Timeout 30 -ErrorAction SilentlyContinue
    }
    Get-NetFirewallRule -Group $FirewallGroup -ErrorAction SilentlyContinue | Remove-NetFirewallRule
    if (Test-Path $InstallDir) { Remove-Item $InstallDir -Recurse -Force }
    if ($Purge) {
        if (Test-Path $DataDir) { Remove-Item $DataDir -Recurse -Force }
        Write-Host 'BACpack removed, data deleted.'
    } else {
        Write-Host "BACpack removed. Data kept in $DataDir (a reinstall picks it up; -Purge deletes it)."
    }
    exit 0
}

# The address of the interface that carries the default route, so a host-only or
# virtual adapter is never offered. Falls back to the first DHCP or manual
# address; never fails the install.
function Get-LanAddress {
    try {
        $routes = Get-NetRoute -AddressFamily IPv4 -DestinationPrefix 0.0.0.0/0 -ErrorAction SilentlyContinue |
            ForEach-Object {
                $iface = Get-NetIPInterface -AddressFamily IPv4 -InterfaceIndex $_.InterfaceIndex -ErrorAction SilentlyContinue |
                    Select-Object -First 1
                [pscustomobject]@{ Index = $_.InterfaceIndex; Metric = [int]$_.RouteMetric + [int]$iface.InterfaceMetric }
            } | Sort-Object Metric
        foreach ($route in $routes) {
            $addr = (Get-NetIPAddress -AddressFamily IPv4 -InterfaceIndex $route.Index -ErrorAction SilentlyContinue |
                     Where-Object { $_.AddressState -eq 'Preferred' -and $_.IPAddress -notlike '169.254.*' } |
                     Select-Object -First 1).IPAddress
            if ($addr) { return $addr }
        }
    } catch { Write-Verbose "default-route lookup failed: $_" }
    try {
        return (Get-NetIPAddress -AddressFamily IPv4 -PrefixOrigin Dhcp, Manual -ErrorAction SilentlyContinue |
                Where-Object { $_.IPAddress -notlike '169.254.*' } | Select-Object -First 1).IPAddress
    } catch { return $null }
}

# --- preflight: nothing is changed before all of this passes ---------------
# Native commands that write to stderr throw under 'Stop' in PowerShell 5.1,
# even with 2>$null; these probes only read the exit code.
function Test-Python([string]$Exe) {
    $ErrorActionPreference = 'Continue'
    & $Exe -c 'import sys; sys.exit(sys.version_info < (3, 13))' 2>$null
    return $LASTEXITCODE -eq 0
}

function Find-Python {
    if ($Python) { return $Python }
    $ErrorActionPreference = 'Continue'
    # An upgrade keeps the interpreter the install was made with (often a
    # -Python one), while it still exists.
    $cfg = Join-Path $InstallDir '.venv\pyvenv.cfg'
    if (Test-Path $cfg) {
        $m = [regex]::Match((Get-Content $cfg -Raw), '(?m)^executable\s*=\s*(.+?)\s*$')
        if ($m.Success -and (Test-Path $m.Groups[1].Value) -and (Test-Python $m.Groups[1].Value)) { return $m.Groups[1].Value }
    }
    if (Get-Command py -ErrorAction SilentlyContinue) {
        foreach ($ver in '-3.14', '-3.13') {
            $exe = & py $ver -c 'import sys; print(sys.executable)' 2>$null
            if ($LASTEXITCODE -eq 0 -and $exe) { return $exe.Trim() }
        }
    }
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd -and (Test-Python $cmd.Source)) {
        return (& $cmd.Source -c 'import sys; print(sys.executable)').Trim()
    }
    return $null
}

# install.sh's root-owner warning: anyone else who can write the Python's
# folder can change the interpreter the service runs. A root-level folder
# such as C:\Python313 inherits Modify for Authenticated Users.
function Get-Writers([string]$Dir) {
    $trusted = 'S-1-5-18', 'S-1-5-32-544', 'S-1-3-0',  # SYSTEM, Administrators, CREATOR OWNER
        'S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464'  # TrustedInstaller
    # WriteData, AppendData, DeleteSubdirectoriesAndFiles, Delete, ChangePermissions,
    # TakeOwnership, GENERIC_ALL, GENERIC_WRITE
    [long]$mask = 0x2 -bor 0x4 -bor 0x40 -bor 0x10000 -bor 0x40000 -bor 0x80000 -bor 0x10000000 -bor 0x40000000
    foreach ($rule in (Get-Acl $Dir).GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier])) {
        if ($rule.AccessControlType -ne 'Allow' -or $trusted -contains $rule.IdentityReference.Value) { continue }
        if (([long]$rule.FileSystemRights -band $mask) -eq 0) { continue }
        try { $rule.IdentityReference.Translate([Security.Principal.NTAccount]).Value }
        catch { $rule.IdentityReference.Value }
    }
}

if ($Port -ne 0 -and ($Port -lt 1 -or $Port -gt 65535)) { Fail "-Port must be 1-65535, not $Port" }

# The login password is settled here, before anything changes: from the
# environment (unattended), else asked twice. The admin command checks the
# rules again; this catches the common mistake early.
if ($LoginUser -and $NoLogin) { Fail "-LoginUser and -NoLogin can't be used together" }
$LoginPassword = ''
if ($LoginUser) {
    $LoginPassword = $env:BACPACK_LOGIN_PASSWORD
    if (-not $LoginPassword) {
        if (-not [Environment]::UserInteractive) { Fail '-LoginUser needs a password: set BACPACK_LOGIN_PASSWORD, or run this from a console' }
        function ConvertTo-PlainText([Security.SecureString]$Secret) {
            $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Secret)
            try { [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) } finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
        }
        $LoginPassword = ConvertTo-PlainText (Read-Host "Password for the web login '$LoginUser' (12 characters or more)" -AsSecureString)
        $again = ConvertTo-PlainText (Read-Host 'Type it again' -AsSecureString)
        if ($LoginPassword -ne $again) { Fail 'the two passwords differ' }
    }
    if ($LoginPassword.Length -lt 12) { Fail 'the web login password needs at least 12 characters' }
}

if (-not $BundleMode) {
    $Py = Find-Python
    if (-not $Py) {
        Fail ("BACpack needs Python 3.13 or newer installed for all users, and none was found.`n" +
              "Install it from python.org, for example:`n" +
              "  python-3.14.x-amd64.exe /quiet InstallAllUsers=1 PrependPath=1`n" +
              "or pass one with -Python C:\path\to\python.exe")
    }
    if (-not (Test-Path $Py)) { Fail "-Python $Py does not exist" }
    if (-not (Test-Python $Py)) { Fail "$Py is older than Python 3.13" }
    if ($Py -like '*\AppData\*' -or $Py -like '*\WindowsApps\*') {
        Fail ("$Py is installed for one user only; the service account can't read it.`n" +
              "Reinstall Python for all users (InstallAllUsers=1) or pass one under Program Files with -Python.")
    }
    & $Py -c 'import sqlite3, sys; sys.exit(sqlite3.sqlite_version_info < (3, 34, 0))'
    if ($LASTEXITCODE -ne 0) { Fail "$Py uses an SQLite older than 3.34; BACpack needs 3.34 or newer." }
    $PyPrefix = (& $Py -c 'import sys; print(sys.base_prefix)').Trim()
    $PyWriters = @(Get-Writers $PyPrefix)
}

# --- port: -Port, else the previous install's, else 8000 -------------------
if ($Port -eq 0) {
    $Port = 8000
    if (Test-Path $WinswXml) {
        $m = [regex]::Match((Get-Content $WinswXml -Raw), '--port (\d+)')
        if ($m.Success) { $Port = [int]$m.Groups[1].Value }
    }
}

# --- stop an existing install (devices stop and are remembered) -----------
$existing = Get-Service $ServiceName -ErrorAction SilentlyContinue
$Fresh = -not $existing
$WasRunning = $existing -and $existing.Status -eq 'Running'
$HadMarker = Test-Path $StartedMarker
# Start unless the user stopped a marked install: no marker is a first install
# (or one that never ran); a running install is started again whatever the marker.
$Start = -not $HadMarker -or $WasRunning
if ($WasRunning) {
    Write-Host 'Stopping BACpack (its running devices are restarted afterwards)...'
    Stop-Service $ServiceName
    $existing.WaitForStatus('Stopped', '00:02:00')
    $RestoreOnFail = $true
}

# On a taken web port the service exits a few seconds into each start and WinSW
# restarts it every 5 s, for good: refuse before anything changes.
function Get-PortOwner([int]$TcpPort) {
    $listener = Get-NetTCPConnection -State Listen -LocalPort $TcpPort -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $listener) { return $null }
    $proc = Get-Process -Id $listener.OwningProcess -ErrorAction SilentlyContinue
    if ($proc) { return "$($proc.ProcessName) (process $($listener.OwningProcess))" }
    return "process $($listener.OwningProcess)"
}
if ($Start) {
    $owner = Get-PortOwner $Port
    if ($owner) { Fail "port $Port/tcp is in use by $owner. Stop that program, or choose another port (the setup wizard's port page, setup.exe /PORT=N, or install.ps1 -Port N)." }
}

# --- data directory: only the service, SYSTEM and Administrators ----------
New-Item -ItemType Directory -Force -Path $DataDir, (Join-Path $DataDir 'logs') | Out-Null
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
# A copy of this script, so "bacpack-admin.cmd uninstall" needs no release folder.
# Refreshed at every run, so it always matches the installed version.
$KeptScript = Join-Path $InstallDir 'install.ps1'
if ($PSCommandPath -and ([IO.Path]::GetFullPath($PSCommandPath) -ne [IO.Path]::GetFullPath($KeptScript))) {
    Copy-Item $PSCommandPath $KeptScript -Force
}

# --- code ------------------------------------------------------------------
if ($BundleMode) {
    Write-Host "Copying BACpack's files..."
    if (Test-Path (Join-Path $InstallDir '.venv')) { Remove-Item (Join-Path $InstallDir '.venv') -Recurse -Force }
    if (Test-Path (Join-Path $InstallDir 'python')) { Remove-Item (Join-Path $InstallDir 'python') -Recurse -Force }
    Copy-Item (Join-Path $Here 'python') (Join-Path $InstallDir 'python') -Recurse
    Copy-Item (Join-Path $Here 'bacpack-service.exe') $WinswExe -Force
    # The bundle's copy of the interpreter named BACpack (Task Manager shows
    # that name); its device processes use bacpack-worker.exe beside it.
    $ServicePython = Join-Path $InstallDir 'python\bacpack.exe'
} else {
    if (Test-Path (Join-Path $InstallDir 'python')) { Remove-Item (Join-Path $InstallDir 'python') -Recurse -Force }
    $venv = Join-Path $InstallDir '.venv'
    $venvPy = Join-Path $venv 'Scripts\python.exe'
    # Rebuilt when its interpreter is gone or isn't the one chosen above (a
    # -Python of the same version included).
    if (Test-Path $venv) {
        $cfgPath = Join-Path $venv 'pyvenv.cfg'
        $haveExe = if (Test-Path $cfgPath) { [regex]::Match((Get-Content $cfgPath -Raw), '(?m)^executable\s*=\s*(.+?)\s*$').Groups[1].Value } else { '' }
        $works = (Test-Path $venvPy) -and (Test-Python $venvPy)
        if (-not $works -or -not $haveExe -or [IO.Path]::GetFullPath($haveExe) -ne [IO.Path]::GetFullPath($Py)) {
            Write-Host "Recreating the virtualenv for $Py..."
            Remove-Item $venv -Recurse -Force
        }
    }
    if (-not (Test-Path $venvPy)) { & $Py -m venv $venv; if ($LASTEXITCODE) { Fail 'creating the virtualenv failed' } }
    $stage = Join-Path $env:TEMP ("bacpack-src-" + [guid]::NewGuid())
    New-Item -ItemType Directory -Path $stage | Out-Null
    try {
        foreach ($item in 'pyproject.toml', 'README.md', 'LICENSE', 'src', 'packaging') {
            $path = Join-Path $RepoRoot $item
            if (Test-Path $path) { Copy-Item $path (Join-Path $stage $item) -Recurse }
        }
        Write-Host 'Installing BACpack and its pinned dependencies (this can take a few minutes)...'
        & $venvPy -m pip install --upgrade pip
        & $venvPy -m pip install -c (Join-Path $stage 'packaging\constraints.txt') $stage
        if ($LASTEXITCODE) { Fail 'pip install failed (see above)' }
    } finally { Remove-Item $stage -Recurse -Force }
    if (-not (Test-Path $WinswExe) -or (Get-FileHash $WinswExe -Algorithm SHA256).Hash -ne $WinswSha256) {
        Write-Host 'Downloading WinSW v2.12.0...'
        Invoke-WebRequest $WinswUrl -OutFile "$WinswExe.download" -UseBasicParsing
        if ((Get-FileHash "$WinswExe.download" -Algorithm SHA256).Hash -ne $WinswSha256) {
            Remove-Item "$WinswExe.download"; Fail 'WinSW download failed its SHA-256 check'
        }
        Move-Item "$WinswExe.download" $WinswExe -Force
    }
    $ServicePython = $venvPy
}

# --- service: XML, registration, account, permissions ----------------------
# Unregistered only now, so a failure above leaves the old service in place.
Write-Host 'Registering the service...'
Remove-Service-Registration
$RestoreOnFail = $false
# Dropped whenever this run will start the service and written back only once
# that start holds; a stopped, marked install keeps it.
if ($Start) { Remove-Item $StartedMarker -Force -ErrorAction SilentlyContinue }
$templatePath = if ($BundleMode) { Join-Path $Here 'bacpack-service.xml.template' } else { Join-Path $Here 'windows\bacpack-service.xml.template' }
(Get-Content $templatePath -Raw).Replace('{{PYTHON}}', $ServicePython).Replace('{{PORT}}', "$Port").Replace('{{DATA}}', $DataDir) |
    Set-Content $WinswXml -Encoding UTF8
& $WinswExe install | Out-Null
if ($LASTEXITCODE) { Fail 'registering the service failed' }
sc.exe config $ServiceName obj= $Account | Out-Null
if ($LASTEXITCODE) { Fail "setting the service account to $Account failed" }
# The account may stop (and query) its own service: for an exit a restart
# cannot fix, the web process stops the service itself, since WinSW 2.12
# restarts on every non-zero exit.
$sid = (New-Object System.Security.Principal.NTAccount($Account)).Translate([System.Security.Principal.SecurityIdentifier]).Value
$sddl = ((sc.exe sdshow $ServiceName) -join '').Trim()
if ($LASTEXITCODE) { Fail "reading the service's permissions failed: $sddl" }
$ace = "(A;;LCWP;;;$sid)"
if (-not $sddl.Contains($ace)) {
    $sddl = if ($sddl.Contains('S:')) { $sddl.Replace('S:', "${ace}S:") } else { "$sddl$ace" }
    sc.exe sdset $ServiceName $sddl | Out-Null
    if ($LASTEXITCODE) { Fail "letting $Account stop its own service failed" }
}
# Accounts by SID (the * form): SYSTEM and Administrators have other names on a localised Windows.
icacls $InstallDir /grant "*${sid}:(OI)(CI)RX" | Out-Null
if ($LASTEXITCODE) { Fail "setting permissions on $InstallDir failed" }
icacls $DataDir /inheritance:r /grant:r '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' "*${sid}:(OI)(CI)M" | Out-Null
if ($LASTEXITCODE) { Fail "setting permissions on $DataDir failed" }

# --- bacpack-admin: local recovery and setup --------------------------------
# A .cmd next to the service, run from an elevated prompt: the data folder's
# ACL is what keeps anyone else out of the database.
$AdminCmd = Join-Path $InstallDir 'bacpack-admin.cmd'
# "uninstall" deletes this folder, this file with it, and cmd re-reads a batch file
# after each command (even after exit /b inside a block: "The system cannot find
# the path specified", exit 1). So it copies itself and the kept install.ps1 to
# %TEMP% and hands over to that copy: a batch started without CALL never returns.
@(
    '@echo off',
    'if /i "%~1"=="uninstall" if /i not "%~nx0"=="bacpack-uninstall.cmd" (',
    '  if not exist "%~dp0install.ps1" (',
    '    echo error: install.ps1 is missing here; run install.ps1 -Uninstall from the release folder',
    '    exit /b 1',
    '  )',
    '  copy /y "%~dp0install.ps1" "%TEMP%\bacpack-uninstall.ps1" >nul',
    '  copy /y "%~f0" "%TEMP%\bacpack-uninstall.cmd" >nul',
    '  "%TEMP%\bacpack-uninstall.cmd" %*',
    ')',
    'if /i "%~1"=="uninstall" (',
    '  setlocal EnableDelayedExpansion',
    '  set "BP_PURGE="',
    '  if /i "%~2"=="--purge" set "BP_PURGE=-Purge"',
    '  if /i "%~2"=="-purge" set "BP_PURGE=-Purge"',
    '  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0bacpack-uninstall.ps1" -Uninstall !BP_PURGE!',
    '  set "BP_RC=!errorlevel!"',
    '  del "%~dp0bacpack-uninstall.ps1" 2>nul',
    '  exit /b !BP_RC!',
    ')',
    "set BACPACK_DB_PATH=$DataDir\bacpack.db",
    "set BACPACK_PORT=$Port",
    "`"$ServicePython`" -P -m bacpack.admin %*"
) | Set-Content $AdminCmd -Encoding ASCII
# Before the service starts, so a fresh install is never open between start and login.
# The service is registered by now (its account is needed for the folder's ACL), so a
# failure here unregisters a fresh one: it would otherwise start on the next boot in setup
# mode. An upgrade keeps its service; its database already holds its login choice.
if ($LoginUser) {
    Write-Host 'Creating the login...'
    $env:BACPACK_LOGIN_PASSWORD = $LoginPassword
    try { & $AdminCmd set-login --username $LoginUser } finally { Remove-Item Env:\BACPACK_LOGIN_PASSWORD -ErrorAction SilentlyContinue }
    if ($LASTEXITCODE -eq 2) { if ($Fresh) { Remove-Service-Registration }; Fail 'bacpack-admin stopped (its message is above); BACpack was not started. Fix the cause and run the installer again.' }
    if ($LASTEXITCODE) { if ($Fresh) { Remove-Service-Registration }; Fail 'the login was not created (see above); BACpack was not started. Run the installer again.' }
} elseif ($NoLogin) {
    Write-Host "Recording 'run without login'..."
    & $AdminCmd no-login
    if ($LASTEXITCODE) { if ($Fresh) { Remove-Service-Registration }; Fail "recording 'run without login' failed (see above); BACpack was not started. Run the installer again." }
}

# --- firewall: the BACpack group only --------------------------------------
Write-Host 'Opening the firewall...'
Get-NetFirewallRule -Group $FirewallGroup -ErrorAction SilentlyContinue | Remove-NetFirewallRule
New-NetFirewallRule -DisplayName "BACpack web UI ($Port/tcp)" -Group $FirewallGroup -Direction Inbound `
    -Protocol TCP -LocalPort $Port -Action Allow -Profile Any | Out-Null
New-NetFirewallRule -DisplayName "BACpack BACnet/IP ($BacnetPort/udp)" -Group $FirewallGroup -Direction Inbound `
    -Protocol UDP -LocalPort $BacnetPort -Action Allow -Profile Any | Out-Null

if ($Start) {
    Write-Host 'Starting BACpack...'
    Start-Service $ServiceName
    # WinSW restarts a service that exits non-zero after 5 s, so "Running"
    # later can be a second attempt. The wrapper process is the service
    # process: it exits with its child, so a changed process id, or a state
    # other than Running, before the web port listens means the start did not
    # hold. The port was free above, so a listener on it is BACpack's.
    $firstPid = (Get-CimInstance Win32_Service -Filter "Name='$ServiceName'").ProcessId
    $held = $true
    $listening = $false
    for ($second = 0; $second -lt 90 -and $held -and -not $listening; $second++) {
        Start-Sleep -Seconds 1
        $now = Get-CimInstance Win32_Service -Filter "Name='$ServiceName'"
        $held = ($now.State -eq 'Running') -and ($now.ProcessId -eq $firstPid)
        $listening = $held -and [bool](Get-PortOwner $Port)
    }
    if (-not $held) {
        # WinSW would keep restarting it after this installer is gone.
        Stop-Service $ServiceName -ErrorAction SilentlyContinue
        Write-Host 'error: BACpack started but stopped before its web UI came up; the installer stopped the service. The last log lines:' -ForegroundColor Red
        # The log names come from WinSW, so take the files written last rather than guess them.
        Get-ChildItem (Join-Path $DataDir 'logs') -Filter '*.log' -ErrorAction SilentlyContinue |
            Sort-Object LastWriteTime -Descending | Select-Object -First 3 |
            ForEach-Object { Write-Host "--- $($_.Name)"; Get-Content $_.FullName -Tail 20 }
        exit 1
    }
    # Still running, so left running: it may be waiting for its saved listen
    # address to appear on this computer.
    if (-not $listening) {
        Write-Host "warning: BACpack is running but its web UI is not listening on port $Port after 90 seconds; see $DataDir\logs." -ForegroundColor Yellow
    }
    Set-Content $StartedMarker 'started' -Encoding ASCII
}

$ip = Get-LanAddress
$version = (& $ServicePython -c 'from importlib.metadata import version; print(version(''bacpack''))').Trim()
$mode = if ($BundleMode) { 'bundle' } else { 'source' }
$reach = if ($ip) { $ip } else { "<this computer's address>" }
Write-Host ""
Write-Host "BACpack $version is installed ($mode mode)."
Write-Host ""
Write-Host "  Web UI:   https://localhost:$Port"
Write-Host "            from another computer: https://${reach}:$Port"
Write-Host "  Logs:     $DataDir\logs"
Write-Host "  Service:  Get-Service BACpack (Stop-, Start-, Restart-Service)"
# Data kept from an earlier install may already hold a login: ask for one in the
# hint only when none is chosen yet (if the state can't be read, the hint stays).
$LoginNotChosen = $true
if (-not $LoginUser -and -not $NoLogin -and -not $HadMarker -and -not $WasRunning) {
    try {
        $status = & $AdminCmd status 2>&1
        if ($LASTEXITCODE -eq 0 -and "$status" -notmatch 'not chosen yet') { $LoginNotChosen = $false }
    } catch { Write-Verbose "login status unavailable: $_" }
}
if ($LoginUser) {
    Write-Host ""
    Write-Host "Next: sign in as '$LoginUser'."
} elseif ($NoLogin) {
    Write-Host ""
    Write-Host 'Warning: BACpack runs without a login, so anyone on the'
    Write-Host 'network can control it. Add a login under Security in the'
    Write-Host 'web UI.'
} elseif (-not $HadMarker -and -not $WasRunning -and $LoginNotChosen) {
    Write-Host ""
    Write-Host 'Next: open the web UI and create a login (the first visit'
    Write-Host 'claims it), or choose to run without one.'
}
Write-Host ""
Write-Host 'Notes:'
Write-Host '- The certificate is self-signed, so the browser warns once.'
Write-Host '    The Web Server page shows its fingerprint.'
Write-Host "- Firewall: $Port/tcp and $BacnetPort/udp are open (group '$FirewallGroup')."
Write-Host '    A device on another UDP port needs its own rule.'
Write-Host '- For port 443, run the installer again with -Port 443.'
Write-Host '- Read SECURITY.md before you put BACpack on a shared network.'
if (-not $NoLogin) {
    Write-Host '- Forgotten password, from an elevated prompt:'
    Write-Host "    `"$AdminCmd`" reset-login"
}
if (-not $Start) {
    Write-Host ""
    Write-Host 'The service was stopped before the upgrade and is left stopped.'
}
if ($PyWriters) {
    $writers = ($PyWriters | Sort-Object -Unique) -join ', '
    Write-Host ""
    Write-Host "Warning: $PyPrefix can be changed by" -ForegroundColor Yellow
    Write-Host "  $writers," -ForegroundColor Yellow
    Write-Host 'who can then change the Python this service runs.' -ForegroundColor Yellow
    Write-Host 'Install Python under Program Files (the python.org all-users' -ForegroundColor Yellow
    Write-Host 'default), or remove that write access.' -ForegroundColor Yellow
}
Write-Host ""
# Not the exit code of the last native command above.
exit 0
