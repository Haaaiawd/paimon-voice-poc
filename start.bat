@echo off
rem ============================================
rem  paimon-voice-poc one-click dev startup
rem  backend  : python -m runtime.ws_gateway  (8766)
rem  frontend : vite dev                     (5173)
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
echo [paimon] starting frontend http://127.0.0.1:5173
start "paimon-frontend" cmd /k "set VITE_BACKEND=ws& set VITE_WS_URL=ws://localhost:8766/ws/chat& npm run dev"
cd /d %~dp0

echo.
echo Backend  : ws://127.0.0.1:8766/ws/chat
echo Frontend : http://127.0.0.1:5173  -- open this in browser
echo Two windows were spawned; close them to stop the services.
echo.
pause
