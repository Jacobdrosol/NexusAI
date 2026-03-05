param(
  [string]$ControlPlaneUrl = "http://localhost:8000",
  [string]$DashboardUrl = "http://localhost:5000",
  [string]$NexusWorkerUrl = "http://localhost:8010",
  [string]$ApiToken = ""
)

$ErrorActionPreference = "Stop"
$failures = @()

function Write-Check {
  param([string]$Message)
  Write-Host "[CHECK] $Message"
}

function Write-Pass {
  param([string]$Message)
  Write-Host "[PASS]  $Message" -ForegroundColor Green
}

function Write-Fail {
  param([string]$Message)
  Write-Host "[FAIL]  $Message" -ForegroundColor Red
  $script:failures += $Message
}

function Invoke-Json {
  param(
    [string]$Method,
    [string]$Url,
    [hashtable]$Headers = @{},
    [object]$Body = $null
  )
  $invokeParams = @{
    Method = $Method
    Uri = $Url
    Headers = $Headers
    TimeoutSec = 15
  }
  if ($null -ne $Body) {
    $invokeParams["ContentType"] = "application/json"
    $invokeParams["Body"] = ($Body | ConvertTo-Json -Depth 10)
  }
  return Invoke-RestMethod @invokeParams
}

function Get-StatusCode {
  param(
    [string]$Method,
    [string]$Url,
    [hashtable]$Headers = @{},
    [object]$Body = $null
  )
  try {
    $invokeParams = @{
      Method = $Method
      Uri = $Url
      Headers = $Headers
      TimeoutSec = 15
    }
    if ($null -ne $Body) {
      $invokeParams["ContentType"] = "application/json"
      $invokeParams["Body"] = ($Body | ConvertTo-Json -Depth 10)
    }
    Invoke-WebRequest @invokeParams | Out-Null
    return 200
  } catch {
    if ($_.Exception.Response -and $_.Exception.Response.StatusCode) {
      return [int]$_.Exception.Response.StatusCode
    }
    return -1
  }
}

Write-Check "Control plane health"
try {
  $cpHealth = Invoke-Json -Method "GET" -Url "$ControlPlaneUrl/health"
  if ($cpHealth.status -eq "ok") { Write-Pass "Control plane is healthy" } else { Write-Fail "Control plane health is not ok" }
} catch {
  Write-Fail "Control plane health check failed: $($_.Exception.Message)"
}

Write-Check "Dashboard health"
try {
  $dashHealth = Invoke-Json -Method "GET" -Url "$DashboardUrl/health"
  if ($dashHealth.status -eq "ok") { Write-Pass "Dashboard is healthy" } else { Write-Fail "Dashboard health is not ok" }
} catch {
  Write-Fail "Dashboard health check failed: $($_.Exception.Message)"
}

Write-Check "nexus_worker health"
try {
  $workerHealth = Invoke-Json -Method "GET" -Url "$NexusWorkerUrl/health"
  if ($workerHealth.status -eq "ok") { Write-Pass "nexus_worker is healthy" } else { Write-Fail "nexus_worker health is not ok" }
} catch {
  Write-Fail "nexus_worker health check failed: $($_.Exception.Message)"
}

if ($ApiToken -and $ApiToken.Trim().Length -gt 0) {
  Write-Check "Control-plane auth enforcement without token"
  $unauthStatus = Get-StatusCode -Method "GET" -Url "$ControlPlaneUrl/v1/workers"
  if ($unauthStatus -eq 401) {
    Write-Pass "Control-plane blocks unauthenticated requests"
  } else {
    Write-Fail "Expected 401 without token, got $unauthStatus"
  }

  Write-Check "Control-plane auth with token"
  $authHeaders = @{ "X-Nexus-API-Key" = $ApiToken }
  $authStatus = Get-StatusCode -Method "GET" -Url "$ControlPlaneUrl/v1/workers" -Headers $authHeaders
  if ($authStatus -eq 200) {
    Write-Pass "Control-plane accepts token-authenticated requests"
  } else {
    Write-Fail "Expected 200 with token, got $authStatus"
  }
} else {
  Write-Host "[WARN]  ApiToken not provided; skipping auth enforcement checks" -ForegroundColor Yellow
}

Write-Check "nexus_worker cloud context block policy"
$blockStatus = Get-StatusCode `
  -Method "POST" `
  -Url "$NexusWorkerUrl/infer" `
  -Body @{
    provider = "openai"
    model = "gpt-4o-mini"
    messages = @(
      @{ role = "system"; content = "Context:`nprivate-data" },
      @{ role = "user"; content = "hello" }
    )
  }
if ($blockStatus -eq 403) {
  Write-Pass "Cloud context egress is blocked as expected"
} else {
  Write-Fail "Expected 403 for blocked cloud context policy, got $blockStatus"
}

if ($failures.Count -gt 0) {
  Write-Host ""
  Write-Host "Pre-UAT security checks FAILED:" -ForegroundColor Red
  $failures | ForEach-Object { Write-Host " - $_" -ForegroundColor Red }
  exit 1
}

Write-Host ""
Write-Host "All pre-UAT security checks passed." -ForegroundColor Green
exit 0

