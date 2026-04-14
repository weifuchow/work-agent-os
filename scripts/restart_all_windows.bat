@echo off
setlocal

cd /d "%~dp0.."
set "ROOT=%CD%"
set "LOG_DIR=%ROOT%\.tmp\run-logs"

if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"
del /q "%LOG_DIR%\api.log" "%LOG_DIR%\worker.log" "%LOG_DIR%\scheduler.log" "%LOG_DIR%\frontend.log" 2>nul

echo === Work Agent OS: Restart All Services (Windows) ===
echo Project root: %ROOT%
echo Logs: %LOG_DIR%

echo.
echo [1/4] Stopping old services...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$regex = 'apps\.api\.main:app|apps\.worker\.feishu_worker|apps\.worker\.scheduler|vite(\.js)?';" ^
  "Get-CimInstance Win32_Process |" ^
  "Where-Object { $_.CommandLine -and $_.CommandLine -match $regex } |" ^
  "ForEach-Object { try { Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop; Write-Output ('  stopped PID ' + $_.ProcessId) } catch {} }"

timeout /t 2 /nobreak >nul

echo.
echo [2/4] Initializing database...
python scripts\init_db.py
if errorlevel 1 (
  echo init_db failed
  exit /b 1
)

echo.
echo [3/4] Starting services...
start "work-agent-api" /min cmd /c "cd /d ""%ROOT%"" && python -u -m uvicorn apps.api.main:app --host 127.0.0.1 --port 8000 > ""%LOG_DIR%\api.log"" 2>&1"
start "work-agent-worker" /min cmd /c "cd /d ""%ROOT%"" && python -u -m apps.worker.feishu_worker > ""%LOG_DIR%\worker.log"" 2>&1"
start "work-agent-scheduler" /min cmd /c "cd /d ""%ROOT%"" && python -u -m apps.worker.scheduler > ""%LOG_DIR%\scheduler.log"" 2>&1"
start "work-agent-frontend" /min cmd /c "cd /d ""%ROOT%\apps\admin-ui"" && npm.cmd run dev -- --host 127.0.0.1 --port 5173 > ""%LOG_DIR%\frontend.log"" 2>&1"

echo.
echo [4/4] Waiting for services...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ok = $false;" ^
  "for ($i = 0; $i -lt 30; $i++) {" ^
  "  try { Invoke-WebRequest -UseBasicParsing 'http://127.0.0.1:8000/health' | Out-Null; $ok = $true; break } catch { Start-Sleep -Seconds 1 }" ^
  "}" ^
  "if (-not $ok) { Write-Error 'API health check failed'; exit 1 }"
if errorlevel 1 exit /b 1

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ok = $false;" ^
  "for ($i = 0; $i -lt 30; $i++) {" ^
  "  try { Invoke-WebRequest -UseBasicParsing 'http://127.0.0.1:5173' | Out-Null; $ok = $true; break } catch { Start-Sleep -Seconds 1 }" ^
  "}" ^
  "if (-not $ok) { Write-Error 'Frontend health check failed'; exit 1 }"
if errorlevel 1 exit /b 1

echo.
echo === Services restarted ===
echo API:      http://127.0.0.1:8000
echo Frontend: http://127.0.0.1:5173
echo.
echo Log files:
echo   %LOG_DIR%\api.log
echo   %LOG_DIR%\worker.log
echo   %LOG_DIR%\scheduler.log
echo   %LOG_DIR%\frontend.log

endlocal
