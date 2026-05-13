@echo off
cd /d "%~dp0"
title MapAgent Server
echo Starting MapAgent...
echo Keep this window open while using the site.
echo.
echo Local URL:
echo   http://127.0.0.1:6006/
echo.
start "" powershell -NoProfile -WindowStyle Hidden -Command "Start-Sleep -Seconds 2; Start-Process 'http://127.0.0.1:6006/?v=20260514h'"
"F:\python\python.exe" -m uvicorn main:app --host 127.0.0.1 --port 6006
echo.
echo MapAgent has stopped. Press any key to close this window.
pause > nul
