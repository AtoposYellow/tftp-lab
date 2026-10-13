@echo off
chcp 936 >nul
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -match 'server.py' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force; Write-Host ('[OK] ÒÑÍ£Ö¹·þÎñÆ÷ PID ' + $_.ProcessId) }"
pause