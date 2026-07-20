try {
    Invoke-RestMethod "http://127.0.0.1:8000/api/status" -TimeoutSec 5 | ConvertTo-Json -Depth 15
}
catch {
    Write-Host "Test UI unavailable: $($_.Exception.Message)" -ForegroundColor Yellow
}
