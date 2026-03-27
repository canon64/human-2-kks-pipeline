@echo off
setlocal

cd /d "%~dp0"

echo === human_2_KKS_pipeline setup ===
echo.

set "PY_VER=3.12.8"
set "PY_DIR=%~dp0python"
set "PY_EXE=%PY_DIR%\python.exe"
set "PIP_EXE=%PY_DIR%\Scripts\pip.exe"
set "PY_ZIP=python-%PY_VER%-embed-amd64.zip"
set "PY_URL=https://www.python.org/ftp/python/%PY_VER%/%PY_ZIP%"
set "GET_PIP_URL=https://bootstrap.pypa.io/get-pip.py"

:: Check / download portable Python
if not exist "%PY_EXE%" (
    echo Python not found. Downloading portable version...
    echo.

    if not exist "%PY_DIR%" mkdir "%PY_DIR%"

    echo Downloading %PY_URL% ...
    powershell -Command "Invoke-WebRequest -Uri '%PY_URL%' -OutFile '%PY_DIR%\%PY_ZIP%'"
    if errorlevel 1 (
        echo [ERROR] Failed to download Python.
        pause
        exit /b 1
    )

    echo Extracting...
    powershell -Command "Expand-Archive -Path '%PY_DIR%\%PY_ZIP%' -DestinationPath '%PY_DIR%' -Force"
    del "%PY_DIR%\%PY_ZIP%"

    :: Enable pip in embeddable Python
    for %%f in ("%PY_DIR%\python*._pth") do (
        powershell -Command "(Get-Content '%%f') -replace '#import site','import site' | Set-Content '%%f'"
    )

    :: Install pip
    echo Installing pip...
    powershell -Command "Invoke-WebRequest -Uri '%GET_PIP_URL%' -OutFile '%PY_DIR%\get-pip.py'"
    "%PY_EXE%" "%PY_DIR%\get-pip.py" --no-warn-script-location
    del "%PY_DIR%\get-pip.py"
)

echo Python: %PY_EXE%
echo.

:: Upgrade pip
echo Upgrading pip...
"%PY_EXE%" -m pip install --upgrade pip
if errorlevel 1 (
    echo [ERROR] Failed to upgrade pip.
    pause
    exit /b 1
)

:: Install build tools (required for some packages)
echo Installing build tools...
"%PY_EXE%" -m pip install --upgrade setuptools wheel
if errorlevel 1 (
    echo [ERROR] Failed to install build tools.
    pause
    exit /b 1
)

:: Install bundled undetected-chromedriver wheel first (offline preferred)
set "WHEEL_DIR=%~dp0vendor\wheels"
if exist "%WHEEL_DIR%\undetected_chromedriver-3.5.5-py3-none-any.whl" (
    echo Installing bundled undetected-chromedriver wheel...
    "%PY_EXE%" -m pip install --no-index --find-links "%WHEEL_DIR%" --no-deps undetected-chromedriver==3.5.5
    if errorlevel 1 (
        echo [ERROR] Failed to install bundled undetected-chromedriver wheel.
        pause
        exit /b 1
    )
) else (
    echo [WARN] Bundled undetected-chromedriver wheel not found: %WHEEL_DIR%
)

:: Install requirements
echo.
echo Installing packages...
"%PIP_EXE%" install -r requirements.txt
if errorlevel 1 (
    echo [ERROR] Failed to install packages.
    pause
    exit /b 1
)

echo.
echo === Setup complete ===

:: Direct launch: no pause needed. Double-click: pause to show result.
if "%~1"=="--no-pause" exit /b 0
pause
