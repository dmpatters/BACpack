<#
Builds dist\BACpack-<version>-windows-x64.zip: embeddable Python 3.14 with
BACpack and its pinned dependencies (the interpreter also as bacpack.exe and
bacpack-worker.exe, so processes are named BACpack), WinSW, install.ps1 and the licences.
Needs a full Python 3.14 x64 to run pip (the embeddable one has none):
  powershell -File packaging\windows\build_bundle.ps1
It takes -Python if given, else the py launcher's 3.14-64, else the python on PATH,
and stops unless that Python matches the bundle's version: pip installs wheels
for the Python it runs under, so another version's would not load in the bundle.
-OutDir puts the build elsewhere (a synced folder can lock files mid-build).
#>
param([string]$OutDir, [string]$Python)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$PyVersion = '3.14.10'
$PyZipSha256 = '90905E64A840557D8C1BC4F5B74DB3D7EE85E3FFB217611CAC1C7813ACEA4159'
$WinswUrl = 'https://github.com/winsw/winsw/releases/download/v2.12.0/WinSW-x64.exe'
$WinswSha256 = '05B82D46AD331CC16BDC00DE5C6332C1EF818DF8CEEFCD49C726553209B3A0DA'
# A build tool only (it rewrites the copies' version resources); not shipped.
$RceditUrl = 'https://github.com/electron/rcedit/releases/download/v2.0.0/rcedit-x64.exe'
$RceditSha256 = '3E7801DB1A5EDBEC91B49A24A094AAD776CB4515488EA5A4CA2289C400EADE2A'

$root = (Resolve-Path "$PSScriptRoot\..\..").Path
$pyMinor = ($PyVersion -split '\.')[0..1] -join '.'
$BuildPy = $Python
if (-not $BuildPy -and (Get-Command py.exe -ErrorAction SilentlyContinue)) {
    # Under 'Stop', Windows PowerShell turns the launcher's "not installed" stderr into an error.
    try {
        $found = & py.exe "-$pyMinor-64" -c 'import sys; print(sys.executable)' 2>$null
        if ($LASTEXITCODE -eq 0 -and $found) { $BuildPy = "$found".Trim() }
    } catch { $BuildPy = $null }
}
if (-not $BuildPy) { $BuildPy = (Get-Command python -ErrorAction SilentlyContinue).Source }
if (-not $BuildPy) { throw "no Python found: install Python $pyMinor x64, or pass -Python C:\path\to\python.exe" }
# No double quotes in -c: Windows PowerShell drops them from a native command's arguments.
$have = "$(& $BuildPy -c 'import sys; print(*sys.version_info[:2], sep=chr(46), end=chr(32)); print(sys.maxsize > 2**32)')".Trim()
if ($have -ne "$pyMinor True") {
    $what = ($have -replace ' True$', ' x64') -replace ' False$', ' 32-bit'
    throw "the bundle is Python $pyMinor x64, but $BuildPy is Python $what; install Python $pyMinor x64, or pass -Python C:\path\to\python.exe"
}
$version = (& $BuildPy -c "import tomllib; print(tomllib.load(open(r'$root\pyproject.toml','rb'))['project']['version'])").Trim()
if (-not $OutDir) { $OutDir = Join-Path $root 'dist' }
$out = Join-Path $OutDir 'bundle'
$zip = Join-Path $OutDir "BACpack-$version-windows-x64.zip"
if (Test-Path $out) { Remove-Item $out -Recurse -Force }
New-Item -ItemType Directory -Force -Path "$out\licenses" | Out-Null

function Get-Checked([string]$Url, [string]$Sha, [string]$Dest) {
    Invoke-WebRequest $Url -OutFile $Dest -UseBasicParsing
    if ((Get-FileHash $Dest -Algorithm SHA256).Hash -ne $Sha) { throw "$Url failed its SHA-256 check" }
}

$pyZip = Join-Path $env:TEMP "python-$PyVersion-embed-amd64.zip"
Get-Checked "https://www.python.org/ftp/python/$PyVersion/python-$PyVersion-embed-amd64.zip" $PyZipSha256 $pyZip
Expand-Archive $pyZip "$out\python"
# The embeddable Python ignores PYTHONPATH and site-packages unless its ._pth lists them.
$pyTag = 'python' + (($PyVersion -split '\.')[0..1] -join '')
$pth = "$out\python\${pyTag}._pth"
if (-not (Test-Path $pth)) { throw "${pyTag}._pth is not in the embeddable zip; check `$PyVersion" }
Set-Content $pth "${pyTag}.zip`r`n.`r`nLib\site-packages`r`nimport site`r`n" -NoNewline
# pip builds a local project in place; a staged copy keeps build\ out of the checkout.
$stage = Join-Path $env:TEMP ("bacpack-bundle-src-" + [guid]::NewGuid())
New-Item -ItemType Directory -Path $stage | Out-Null
try {
    Copy-Item "$root\pyproject.toml", "$root\README.md" $stage
    Copy-Item "$root\src" "$stage\src" -Recurse
    Get-ChildItem "$stage\src" -Recurse -Directory -Include '__pycache__', '*.egg-info' | Remove-Item -Recurse -Force
    & $BuildPy -m pip install --target "$out\python\Lib\site-packages" -c "$root\packaging\constraints.txt" $stage
    if ($LASTEXITCODE) { throw 'pip install into the bundle failed' }
} finally { Remove-Item $stage -Recurse -Force }

Get-Checked $WinswUrl $WinswSha256 "$out\bacpack-service.exe"
# Task Manager's Processes tab shows an executable's FileDescription, so the web
# process runs as bacpack.exe ("BACpack") and each device process as
# bacpack-worker.exe ("BACpack device"): copies of the embeddable python.exe
# with their version resources rewritten. python.exe stays beside them for
# anyone running a script. The interpreter finds python314.dll and its ._pth
# by the DLL's name, so a renamed copy loads the same libraries.
$rcedit = Join-Path $env:TEMP 'rcedit-x64-v2.0.0.exe'
Get-Checked $RceditUrl $RceditSha256 $rcedit
foreach ($named in @(@('bacpack.exe', 'BACpack'), @('bacpack-worker.exe', 'BACpack device'))) {
    $copy = "$out\python\$($named[0])"
    Copy-Item "$out\python\python.exe" $copy
    & $rcedit $copy --set-version-string FileDescription $named[1] --set-version-string ProductName 'BACpack' `
        --set-version-string InternalName ($named[0] -replace '\.exe$', '') --set-version-string OriginalFilename $named[0] `
        --set-file-version $version --set-product-version $version
    if ($LASTEXITCODE) { throw "rcedit failed on $($named[0])" }
    if ((Get-Item $copy).VersionInfo.FileDescription -ne $named[1]) { throw "$($named[0]) did not take its FileDescription" }
}
Remove-Item $rcedit
Copy-Item "$root\packaging\install.ps1", "$root\packaging\windows\bacpack-service.xml.template", "$root\README.md" $out
if (Test-Path "$root\LICENSE") { Copy-Item "$root\LICENSE" $out }
Copy-Item "$root\THIRD-PARTY-NOTICES.txt" $out
Copy-Item "$root\PICS.md" $out
Copy-Item "$root\SECURITY.md" $out
Copy-Item "$out\python\LICENSE.txt" "$out\licenses\Python-LICENSE.txt" -ErrorAction SilentlyContinue
Invoke-WebRequest 'https://raw.githubusercontent.com/winsw/winsw/v2.12.0/LICENSE.txt' -OutFile "$out\licenses\WinSW-LICENSE.txt" -UseBasicParsing
Get-ChildItem "$out\python\Lib\site-packages" -Directory -Filter '*.dist-info' | ForEach-Object {
    $dist = $_
    Get-ChildItem $dist.FullName -Recurse -File | Where-Object { $_.Name -match '^(LICEN[CS]E|COPYING|NOTICE)' } |
        ForEach-Object { Copy-Item $_.FullName (Join-Path "$out\licenses" "$($dist.Name -replace '\.dist-info$','')-$($_.Name)") }
}
# Every package with its declared licence, including those whose wheel ships no licence file.
$notices = Get-ChildItem "$out\python\Lib\site-packages" -Directory -Filter '*.dist-info' | Sort-Object Name | ForEach-Object {
    $meta = Get-Content (Join-Path $_.FullName 'METADATA')
    $field = { param($key) ($meta | Where-Object { $_ -like "${key}: *" } | Select-Object -First 1) -replace "^${key}: ", '' }
    $license = & $field 'License-Expression'
    if (-not $license) { $license = & $field 'License' }
    if (-not $license) { $license = (($meta | Where-Object { $_ -like 'Classifier: License ::*' }) -replace '^Classifier: License :: (OSI Approved :: )?', '') -join '; ' }
    "{0} {1}: {2}" -f (& $field 'Name'), (& $field 'Version'), $license
}
@("Python $PyVersion (embeddable): PSF-2.0, see Python-LICENSE.txt", 'WinSW 2.12.0: MIT, see WinSW-LICENSE.txt') + $notices |
    Set-Content "$out\licenses\THIRD-PARTY.txt"

# No bytecode from the smoke run in the shipped tree.
$env:PYTHONDONTWRITEBYTECODE = '1'
& "$out\python\bacpack.exe" "$root\packaging\windows\bundle_smoke.py"
Remove-Item Env:\PYTHONDONTWRITEBYTECODE
if ($LASTEXITCODE) { throw 'bundle smoke test failed' }

if (Test-Path $zip) { Remove-Item $zip }
Compress-Archive -Path "$out\*" -DestinationPath $zip
Write-Host "built $zip"
