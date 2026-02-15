$job="3f5e5918-3756-439c-95e6-bfbee134049f"
$r = Invoke-RestMethod -Method Post `
  -Uri "http://localhost:8000/jobs/$job/retrieve" `
  -ContentType "application/json" `
  -Body '{"query":"packaging requirements", "k": 5}'

$r.hits | Format-List

