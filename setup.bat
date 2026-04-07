@echo off
setlocal

cd /d "%~dp0"

set "SETUP_LOG=%~dp0setup_log.txt"
echo === setup started %date% %time% === > "%SETUP_LOG%"

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
echo [step] pip upgrade >> "%SETUP_LOG%"
"%PY_EXE%" -m pip install --upgrade pip >> "%SETUP_LOG%" 2>&1
echo Installing build tools (setuptools)...
echo [step] setuptools install >> "%SETUP_LOG%"
"%PY_EXE%" -m pip install setuptools==70.3.0 --no-warn-script-location >> "%SETUP_LOG%" 2>&1
if errorlevel 1 (
    echo [ERROR] Failed to install setuptools. See setup_log.txt
    pause
    exit /b 1
)
echo Installing build tools (wheel)...
echo [step] wheel install >> "%SETUP_LOG%"
"%PY_EXE%" -m pip install wheel --no-warn-script-location >> "%SETUP_LOG%" 2>&1
if errorlevel 1 (
    echo [ERROR] Failed to install wheel. See setup_log.txt
    pause
    exit /b 1
)

:: Download and extract CUDA DLLs (cuBLAS + cuDNN for ctranslate2 3.x)
set "CUDA_DLLS_DIR=%~dp0python\cuda_dlls"
set "CUDA_MARKER=%CUDA_DLLS_DIR%\cublas64_11.dll"
set "SEVEN_ZIP=%~dp0_tools\7za.exe"
set "CUDA_7Z=%~dp0_tools\cuBLAS_cuDNN_CUDA11_v4.7z"
set "CUDA_URL=https://github.com/Purfview/whisper-standalone-win/releases/download/libs/cuBLAS.and.cuDNN_CUDA11_win_v4.7z"
echo [step] CUDA DLL check: SEVEN_ZIP=%SEVEN_ZIP% >> "%SETUP_LOG%"
echo [step] CUDA_MARKER=%CUDA_MARKER% >> "%SETUP_LOG%"
if not exist "%SEVEN_ZIP%" (
    echo [ERROR] _tools\7za.exe not found. >> "%SETUP_LOG%"
    echo [ERROR] _tools\7za.exe not found.
    pause
    exit /b 1
)
echo [step] 7za.exe found >> "%SETUP_LOG%"
:: CUDA12版が残っていたら削除（CUDA11版に切り替え）
echo [step] checking CUDA12 dll >> "%SETUP_LOG%"
if exist "%CUDA_DLLS_DIR%\cublas64_12.dll" (
    echo [step] removing old CUDA12 DLLs >> "%SETUP_LOG%"
    echo Removing old CUDA12 DLLs...
    rd /s /q "%CUDA_DLLS_DIR%"
    echo [step] CUDA12 removed >> "%SETUP_LOG%"
)
echo [step] checking CUDA_MARKER >> "%SETUP_LOG%"
if not exist "%CUDA_MARKER%" (
    echo.
    echo Downloading CUDA runtime DLLs (cuBLAS + cuDNN, ~432MB^) ...
    echo [step] CUDA download start >> "%SETUP_LOG%"
    curl -L "%CUDA_URL%" -o "%CUDA_7Z%" >> "%SETUP_LOG%" 2>&1
    if errorlevel 1 (
        echo [ERROR] Failed to download CUDA DLLs. See setup_log.txt >> "%SETUP_LOG%"
        echo [ERROR] Failed to download CUDA DLLs. See setup_log.txt
        pause
        exit /b 1
    )
    echo Extracting CUDA DLLs...
    echo [step] CUDA extract >> "%SETUP_LOG%"
    if not exist "%CUDA_DLLS_DIR%" mkdir "%CUDA_DLLS_DIR%"
    "%SEVEN_ZIP%" e "%CUDA_7Z%" -o"%CUDA_DLLS_DIR%" -y >> "%SETUP_LOG%" 2>&1
    if errorlevel 1 (
        echo [ERROR] Failed to extract CUDA DLLs. See setup_log.txt >> "%SETUP_LOG%"
        echo [ERROR] Failed to extract CUDA DLLs. See setup_log.txt
        pause
        exit /b 1
    )
    del "%CUDA_7Z%"
    :: cudart64_11.dll を pip パッケージから取得してコピー
    echo Downloading CUDA runtime (cudart64_11.dll) ...
    "%PIP_EXE%" install nvidia-cuda-runtime-cu11 --target "%~dp0python\_cudart_tmp" -q
    "%PY_EXE%" -c "import shutil,pathlib; src=list(pathlib.Path(r'%~dp0python\_cudart_tmp').rglob('cudart64_110.dll')); dst=pathlib.Path(r'%CUDA_DLLS_DIR%'/'cudart64_11.dll'); shutil.copy2(src[0],dst) if src else print('[WARN] cudart64_110.dll not found')"
    rd /s /q "%~dp0python\_cudart_tmp"
    echo CUDA DLLs installed.
) else (
    echo CUDA DLLs already present. Skipping.
)

:: Install requirements
echo.
echo Installing packages...
echo [step] requirements.txt >> "%SETUP_LOG%"
"%PIP_EXE%" install -r requirements.txt >> "%SETUP_LOG%" 2>&1
if errorlevel 1 (
    echo [ERROR] Failed to install packages. See setup_log.txt
    pause
    exit /b 1
)

:: Install faster-whisper and its dependencies manually
echo.
echo Installing faster-whisper (CUDA 11/12 compatible)...
echo [step] faster-whisper >> "%SETUP_LOG%"
"%PIP_EXE%" install faster-whisper==0.10.1 --no-deps >> "%SETUP_LOG%" 2>&1
if errorlevel 1 (
    echo [ERROR] Failed to install faster-whisper. See setup_log.txt
    pause
    exit /b 1
)
echo [step] av + ctranslate2 >> "%SETUP_LOG%"
"%PIP_EXE%" install "av>=12,<13" "ctranslate2==3.24.0" >> "%SETUP_LOG%" 2>&1
if errorlevel 1 (
    echo [ERROR] Failed to install av/ctranslate2. See setup_log.txt
    pause
    exit /b 1
)

echo.
echo === Setup complete ===
echo [step] setup complete >> "%SETUP_LOG%"

:: Direct launch: no pause needed. Double-click: pause to show result.
if "%~1"=="--no-pause" exit /b 0
pause
