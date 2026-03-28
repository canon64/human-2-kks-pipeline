@echo off
setlocal
cd /d "%~dp0"

set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "PYTHONLEGACYWINDOWSSTDIO=0"
set "PY_EXE=venv\Scripts\python.exe"

if not exist "%PY_EXE%" (
    echo [ERROR] %PY_EXE% not found.
    echo Create or repair venv, then install requirements.
    pause
    exit /b 1
)

"%PY_EXE%" "%~dp0human_2_KKS_pipeline.py" %*
if errorlevel 1 (
    echo.
    echo [ERROR] Exited with error.
    pause
)
