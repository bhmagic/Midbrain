param([string]$ProviderUrl = "http://127.0.0.1:8791")
$ErrorActionPreference = "Stop"

function Invoke-JsonPost {
    param([string]$Uri, [hashtable]$Body)
    Invoke-RestMethod -Method Post -Uri $Uri -ContentType "application/json" -Body ($Body | ConvertTo-Json -Depth 10)
}

$first = Invoke-JsonPost -Uri "$ProviderUrl/v1/control/lease" -Body @{ holder = "handover-test-a"; duration_ms = 6000 }
Write-Host "Lease A generation: $($first.fencing_generation)"

try {
    Invoke-JsonPost -Uri "$ProviderUrl/v1/control/lease" -Body @{ holder = "handover-test-b"; duration_ms = 6000 } | Out-Null
    throw "A second lease unexpectedly replaced the active owner."
} catch {
    if ($_.Exception.Response.StatusCode.value__ -ne 409) { throw }
    Write-Host "Second acquisition correctly returned HTTP 409."
}

Invoke-JsonPost -Uri "$ProviderUrl/v1/control/lease/release" -Body @{
    lease_id = $first.lease_id
    fencing_generation = $first.fencing_generation
    reason = "handover test release"
} | Out-Null

$second = Invoke-JsonPost -Uri "$ProviderUrl/v1/control/lease" -Body @{ holder = "handover-test-b"; duration_ms = 6000 }
Write-Host "Lease B generation: $($second.fencing_generation)"

try {
    Invoke-JsonPost -Uri "$ProviderUrl/v1/control/lease/release" -Body @{
        lease_id = $first.lease_id
        fencing_generation = $first.fencing_generation
        reason = "stale release test"
    } | Out-Null
    throw "A stale release unexpectedly revoked the newer lease."
} catch {
    if ($_.Exception.Response.StatusCode.value__ -ne 409) { throw }
    Write-Host "Stale release correctly returned HTTP 409."
}

Invoke-JsonPost -Uri "$ProviderUrl/v1/control/lease/release" -Body @{
    lease_id = $second.lease_id
    fencing_generation = $second.fencing_generation
    reason = "handover test complete"
} | Out-Null
Write-Host "Operational lease handover test passed."
