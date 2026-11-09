@echo off
rem Creates a local virtualenv on first run, then forwards all arguments to btmm.py
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
	py -3 -m venv .venv || goto :fail
	".venv\Scripts\python.exe" -m pip install -r requirements.txt || goto :fail
)
".venv\Scripts\python.exe" btmm.py %*
exit /b %errorlevel%
:fail
echo Setup failed. Install Python 3.9+ from python.org and retry.
exit /b 1
Stop-Process -Id 16560
.\run_btmm.bat --controlstart EURUSD,GBPUSD,USDJPY
status
stop
quit