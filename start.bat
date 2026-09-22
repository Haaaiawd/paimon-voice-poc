@echo off
rem ============================================
rem  paimon-voice-poc one-click dev startup
rem  backend  : python -m runtime.ws_gateway  (8766)
rem  frontend : vite dev                     (5173, ws mode)
rem  usage    : double-click start.bat, or run in cmd/pwsh
rem ============================================
cd /d %~dp0

if not exist .env (
  echo [paimon] ERROR: .env not found. Copy .env.example and fill keys first.
  pause
  exit /b 1
)

echo [paimon] starting backend  ws://127.0.0.1:8766/ws/chat
start "paimon-backend" cmd /k python -m runtime.ws_gateway --port 8766

timeout /t 3 /nobreak >nul

cd frontend
if not exist node_modules (
  echo [paimon] first run: npm ci
  call npm ci
)
echo [paimon] starting frontend http://localhost:5173
start "paimon-frontend" cmd /k "set VITE_BACKEND=ws& set VITE_WS_URL=ws://127.0.0.1:8766/ws/chat& npm run dev -- --host"
cd /d %~dp0

echo.
echo Backend  : ws://127.0.0.1:8766/ws/chat
echo Frontend : http://localhost:5173  -- open in browser
echo            (--host on: LAN devices can use http://^<this-pc-ip^>:5173)
echo Two windows were spawned; close them to stop the services.
echo.
pause
