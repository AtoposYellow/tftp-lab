@echo off
chcp 936 >nul
powershell -NoProfile -Command "MSFT_NetUDPEndpoint (InstanceID = "0.0.0.0??69") = Get-NetUDPEndpoint -LocalPort 69 -ErrorAction SilentlyContinue; if ($u) { Write-Host '[OK] TFTP 服务器已在运行，无需重复启动。' } else { Start-Process python -ArgumentList 'server.py' -WorkingDirectory '%~dp0tftp-server\tftp-server' -WindowStyle Hidden; Start-Sleep 2; Write-Host '[OK] TFTP 服务器已在后台启动（无窗口）。管理台: http://127.0.0.1:8080' }"
start "" "http://127.0.0.1:8080"
pause