@echo off
setlocal
cd /d "%~dp0"

set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "PYTHONLEGACYWINDOWSSTDIO=0"

if not exist "python\python.exe" (
    echo [ERROR] python\python.exe not found.
    echo Run setup.bat first.
    pause
    exit /b 1
)

"python\python.exe" "%~dp0human_2_KKS_pipeline.py" %*
if errorlevel 1 (
    echo.
    echo [ERROR] Exited with error.
    pause
)
