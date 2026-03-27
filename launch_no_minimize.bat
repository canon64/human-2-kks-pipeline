@echo off
setlocal
cd /d "%~dp0"

rem Disable self-minimize behavior in launch/setup for this execution.
set "H2KKS_MINIMIZED=1"

call "%~dp0launch.bat" %*
exit /b %errorlevel%
