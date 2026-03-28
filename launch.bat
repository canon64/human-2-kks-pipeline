@echo off
setlocal
cd /d "%~dp0"

set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "PYTHONLEGACYWINDOWSSTDIO=0"
set "_NO_PAUSE=0"

if /I "%~1"=="--no-pause" (
    set "_NO_PAUSE=1"
    shift
)

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

"python\python.exe" "%~dp0main.py" %*
set "_RC=%ERRORLEVEL%"

if not "%_NO_PAUSE%"=="1" (
    if not "%_RC%"=="0" (
        echo.
        echo [ERROR] Exited with error.
    )
    echo.
    echo Press any key to close...
    pause >nul
)

exit /b %_RC%
