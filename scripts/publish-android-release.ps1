param(
    [Parameter(Mandatory = $true)]
    [string]$ReleaseTarget
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$signingFile = Join-Path $env:USERPROFILE '.nexusai\android-release\release-signing.env'
if (-not (Test-Path -LiteralPath $signingFile)) {
    throw "Missing Android signing configuration: $signingFile"
}

Get-Content -LiteralPath $signingFile | ForEach-Object {
    $name, $value = $_.Split('=', 2)
    Set-Item -Path "Env:$name" -Value $value
}
$env:ANDROID_HOME = Join-Path $env:LOCALAPPDATA 'Android\Sdk'
$env:ANDROID_SDK_ROOT = $env:ANDROID_HOME
Push-Location (Join-Path $repoRoot 'android')
try {
    .\gradlew.bat :app:assembleRelease
    if ($LASTEXITCODE -ne 0) { throw 'Android release build failed.' }
    $apk = Join-Path $PWD 'app\build\outputs\apk\release\app-release.apk'
    scp $apk $ReleaseTarget
    if ($LASTEXITCODE -ne 0) { throw 'APK upload failed.' }
} finally {
    Pop-Location
}
