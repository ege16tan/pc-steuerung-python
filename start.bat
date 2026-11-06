@echo off
cd /d "%~dp0"
if "%PC_AGENT_TOKEN%"=="" (
  echo Hinweis: PC_AGENT_TOKEN ist nicht gesetzt - der Agent laeuft ohne Token.
)
echo PC-Agent startet auf http://0.0.0.0:8000  (Simulation=%PC_AGENT_SIMULATE%)
python -m uvicorn main:app --host 0.0.0.0 --port 8000
pause
