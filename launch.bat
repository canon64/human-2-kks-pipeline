@echo off
if /i not "%H2KKS_MINIMIZED%"=="1" (
    set "H2KKS_MINIMIZED=1"
    start "" /min cmd /c "\"%~f0\" %*"
    exit /b
)
setlocal
cd /d "%~dp0"

set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "PYTHONLEGACYWINDOWSSTDIO=0"

if not exist "python\python.exe" (
    echo Python not found. Running setup...
    echo.
    call "%~dp0setup.bat" --no-pause
    if not exist "python\python.exe" (
        echo [ERROR] Setup failed.
        pause
        exit /b 1
    )
)

"python\python.exe" "%~dp0human_2_KKS_pipeline.py" %*
if errorlevel 1 (
    echo.
    echo [ERROR] Exited with error.
    pause
)
