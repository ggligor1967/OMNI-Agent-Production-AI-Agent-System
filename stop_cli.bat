@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\stop_omni_mode.ps1" -Mode cli
endlocal
