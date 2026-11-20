@echo off
setlocal
cd /d "%~dp0"
echo === ClipStudio setup ===

where uv >nul 2>nul
if errorlevel 1 (
  echo Installing uv...
  python -m pip install --user uv >nul 2>nul
  where uv >nul 2>nul
  if errorlevel 1 python -m uv --version >nul 2>nul
  if errorlevel 1 (
    powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://astral.sh/uv/install.ps1 | iex"
    set "PATH=%USERPROFILE%\.local\bin;%PATH%"
  )
)
set "UV=uv"
where uv >nul 2>nul
if errorlevel 1 set "UV=python -m uv"

echo Installing Python 3.12 and creating .venv...
%UV% python install 3.12 || goto :fail
%UV% venv -p 3.12 .venv || goto :fail
echo Installing Python packages...
%UV% pip install --python ".venv\Scripts\python.exe" -e . || goto :fail

where npm >nul 2>nul
if errorlevel 1 (
  echo Node.js was not found. Install Node 20+ from https://nodejs.org and run setup.bat again.
  goto :fail
)
echo Building the web interface...
pushd frontend
call npm ci --no-audit --no-fund || (popd & goto :fail)
call npm run build || (popd & goto :fail)
popd

where ffmpeg >nul 2>nul
if errorlevel 1 echo WARNING: ffmpeg was not found. Install it: winget install Gyan.FFmpeg

if not exist ".env" (
  copy ".env.example" ".env" >nul
  echo Created .env - open it and paste your NVIDIA_API_KEY and GROQ_API_KEY.
)
echo.
echo Setup finished. Now run start.bat
exit /b 0

:fail
echo.
echo Setup FAILED. Read the messages above.
exit /b 1
