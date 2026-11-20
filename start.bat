@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Setup has not been run yet. Running setup.bat first...
  call setup.bat
  if errorlevel 1 exit /b 1
)
if "%PORT%"=="" set PORT=8010
echo ClipStudio is starting (first choice: port %PORT%; the next free port is used if it is busy). Close this window to stop it.
".venv\Scripts\python.exe" -m clipstudio.cli serve --host 127.0.0.1 --port %PORT% --open
pause
