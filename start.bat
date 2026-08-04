@echo off
rem ASCII only, CRLF line endings. Do not add non-ASCII text to this file:
rem cmd.exe parses .bat bytes using the console codepage that is active when
rem the file is opened, so UTF-8 Chinese here gets mis-decoded and the parser
rem splits lines mid-command. All user-facing Chinese lives in scripts/launch.py.
chcp 65001 >nul
cd /d "%~dp0"
title Music Downloader

rem Optional user settings (library paths, MusicBrainz contact, port).
rem Not tracked by git, so updates never clobber them.
rem Use %~dp0 rather than a bare name: cmd's CALL does not resolve a
rem quoted bare filename against the current directory, so the settings
rem file would silently never load.
if exist "%~dp0settings.local.cmd" call "%~dp0settings.local.cmd"

if not exist ".venv\Scripts\python.exe" (
    echo First run: creating the virtual environment and installing dependencies.
    echo This happens once and takes a few minutes.
    echo.
    py -3.12 -m venv .venv 2>nul
    if not exist ".venv\Scripts\python.exe" python -m venv .venv
    if not exist ".venv\Scripts\python.exe" goto :no_venv
    ".venv\Scripts\python.exe" -m pip install --upgrade pip
    ".venv\Scripts\python.exe" -m pip install -e "."
    echo.
)

".venv\Scripts\python.exe" "scripts\launch.py"
if errorlevel 1 pause
exit /b

:no_venv
echo.
echo ============================================================
echo Could not create the virtual environment.
echo Install Python 3.12 or newer and make sure it is on PATH.
echo ============================================================
echo.
pause
exit /b 1
