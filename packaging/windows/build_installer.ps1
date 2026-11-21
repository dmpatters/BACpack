<#
Builds dist\BACpack-<version>-setup.exe, the Windows setup wizard, from the
bundle that build_bundle.ps1 leaves in dist\bundle:
  powershell -File packaging\windows\build_installer.ps1
It runs build_bundle.ps1 first (-OutDir and -Python go to it), then compiles
packaging\windows\bacpack.iss with Inno Setup's ISCC.

The compiler is fetched at a pinned version, checked against a pinned SHA-256
and installed for the current user only (no administrator rights, nothing in
Program Files) under %LOCALAPPDATA%\bacpack-build; later builds reuse it.
-Clean removes that copy again (its uninstaller also removes the per-user
"Installed apps" entry the Inno installer makes).
#>
param([string]$OutDir, [string]$Python, [switch]$Clean)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$InnoVersion = '6.7.4'
$InnoUrl = 'https://github.com/jrsoftware/issrc/releases/download/is-6_7_4/innosetup-6.7.4.exe'
$InnoSha256 = 'A549929319F9120716677C685535CA4671E4B937B5FDC87E2585F32D8EB30E8C'

$root = (Resolve-Path "$PSScriptRoot\..\..").Path
$cache = Join-Path $env:LOCALAPPDATA "bacpack-build\innosetup-$InnoVersion"
$iscc = Join-Path $cache 'ISCC.exe'

if ($Clean) {
    $uninstaller = Join-Path $cache 'unins000.exe'
    if (Test-Path $uninstaller) { Start-Process $uninstaller '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART' -Wait }
    if (Test-Path $cache) { Remove-Item $cache -Recurse -Force }
    Write-Host "removed $cache"
    return
}

if (-not (Test-Path $iscc)) {
    $download = Join-Path $env:TEMP "innosetup-$InnoVersion.exe"
    Write-Host "Downloading Inno Setup $InnoVersion..."
    Invoke-WebRequest $InnoUrl -OutFile $download -UseBasicParsing
    if ((Get-FileHash $download -Algorithm SHA256).Hash -ne $InnoSha256) {
        Remove-Item $download
        throw "$InnoUrl failed its SHA-256 check"
    }
    Write-Host "Installing it for the current user into $cache..."
    New-Item -ItemType Directory -Force -Path (Split-Path $cache) | Out-Null
    $setup = Start-Process $download '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/CURRENTUSER', '/NOICONS', "/DIR=$cache" -Wait -PassThru
    Remove-Item $download
    if ($setup.ExitCode -ne 0 -or -not (Test-Path $iscc)) { throw "installing Inno Setup failed (exit $($setup.ExitCode))" }
}

$bundleArgs = @{}
if ($OutDir) { $bundleArgs['OutDir'] = $OutDir }
if ($Python) { $bundleArgs['Python'] = $Python }
& "$PSScriptRoot\build_bundle.ps1" @bundleArgs
if (-not $OutDir) { $OutDir = Join-Path $root 'dist' }
$bundle = Join-Path $OutDir 'bundle'
$version = [regex]::Match((Get-Content "$root\pyproject.toml" -Raw), '(?m)^version\s*=\s*"([^"]+)"').Groups[1].Value
if (-not $version) { throw 'no version in pyproject.toml' }

& $iscc "/DAppVersion=$version" "/DBundleDir=$bundle" "/O$OutDir" "$PSScriptRoot\bacpack.iss"
if ($LASTEXITCODE) { throw 'ISCC failed' }
$setupExe = Join-Path $OutDir "BACpack-$version-setup.exe"
Write-Host "built $setupExe"
Write-Host ('SHA-256 ' + (Get-FileHash $setupExe -Algorithm SHA256).Hash)
