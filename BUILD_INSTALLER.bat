@echo off
setlocal
cd /d "%~dp0"
echo Auto-Editor PRO Studio - Windows Installer Builder
echo.
echo This will build the installer on this Windows PC.
echo The first run may download Python, Node, FFmpeg, npm packages and Remotion's browser.
echo.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\build_studio_windows.ps1"
if errorlevel 1 (
  echo.
  echo BUILD FAILED. Scroll up to see the reason.
  pause
  exit /b 1
)
echo.
echo SUCCESS: release\Auto-Editor-PRO-Studio-Setup.exe
start "" explorer.exe /select,"%~dp0release\Auto-Editor-PRO-Studio-Setup.exe"
pause
