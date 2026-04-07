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
)

:: Check / install pip (python.exeがあってもpipがなければ再導入)
if not exist "%PIP_EXE%" (
    echo pip not found. Installing pip...
    powershell -Command "Invoke-WebRequest -Uri '%GET_PIP_URL%' -OutFile '%PY_DIR%\get-pip.py'"
    "%PY_EXE%" "%PY_DIR%\get-pip.py" --no-warn-script-location
    del "%PY_DIR%\get-pip.py"
    if not exist "%PIP_EXE%" (
        echo [ERROR] Failed to install pip.
        pause
        exit /b 1
    )
)

echo Python: %PY_EXE%
echo.

:: Upgrade pip and install build tools
echo Upgrading pip...
"%PY_EXE%" -m pip install --upgrade pip
echo Installing build tools...
"%PY_EXE%" -m pip install "setuptools<71" wheel
if errorlevel 1 (
    echo [ERROR] Failed to install setuptools/wheel.
    pause
    exit /b 1
)

:: Download and extract CUDA DLLs (cuBLAS + cuDNN for ctranslate2 3.x)
set "CUDA_DLLS_DIR=%~dp0python\cuda_dlls"
set "CUDA_MARKER=%CUDA_DLLS_DIR%\cublas64_12.dll"
set "SEVEN_ZIP=%~dp0_tools\7za.exe"
set "CUDA_7Z=%~dp0_tools\cuBLAS_cuDNN_CUDA12_v1.7z"
set "CUDA_URL=https://github.com/Purfview/whisper-standalone-win/releases/download/libs/cuBLAS.and.cuDNN_CUDA12_win_v1.7z"
if not exist "%CUDA_MARKER%" (
    echo.
    echo Downloading CUDA runtime DLLs (cuBLAS + cuDNN, ~447MB^) ...
    echo [DEBUG] CUDA_URL=%CUDA_URL%
    echo [DEBUG] CUDA_7Z=%CUDA_7Z%
    curl -L "%CUDA_URL%" -o "%CUDA_7Z%"
    if errorlevel 1 (
        echo [ERROR] Failed to download CUDA DLLs.
        pause
        exit /b 1
    )
    echo Extracting CUDA DLLs...
    if not exist "%CUDA_DLLS_DIR%" mkdir "%CUDA_DLLS_DIR%"
    "%SEVEN_ZIP%" e "%CUDA_7Z%" -o"%CUDA_DLLS_DIR%" -y
    if errorlevel 1 (
        echo [ERROR] Failed to extract CUDA DLLs.
        pause
        exit /b 1
    )
    del "%CUDA_7Z%"
    echo CUDA DLLs installed.
) else (
    echo CUDA DLLs already present. Skipping.
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

:: Install faster-whisper and its dependencies manually
:: faster-whisper 0.10.1 requires av==10.* which has no Python 3.12 wheel,
:: so we install with --no-deps and provide compatible versions separately.
echo.
echo Installing faster-whisper (CUDA 11/12 compatible)...
"%PIP_EXE%" install faster-whisper==0.10.1 --no-deps
if errorlevel 1 (
    echo [ERROR] Failed to install faster-whisper.
    pause
    exit /b 1
)
"%PIP_EXE%" install "av>=12,<13" "ctranslate2==3.24.0"
if errorlevel 1 (
    echo [ERROR] Failed to install av/ctranslate2.
    pause
    exit /b 1
)

echo.
echo === Setup complete ===

:: Direct launch: no pause needed. Double-click: pause to show result.
if "%~1"=="--no-pause" exit /b 0
pause
