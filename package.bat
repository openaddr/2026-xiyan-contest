@echo off
rem Build contest ZIP (Windows entry). Real logic lives in package.py:
rem it forces LF endings on start.sh and stores the unix exec bit in the zip.
cd /d "%~dp0"
py -3 package.py 2>nul
if errorlevel 1 python package.py
if errorlevel 1 (
  echo package FAILED - make sure Python is installed
  exit /b 1
)
pause
