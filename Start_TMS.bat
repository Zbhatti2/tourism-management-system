@echo off
setlocal

REM ============================================================
REM  Tourism Management System - Start script
REM
REM  Double-click this file (or run it from a Command Prompt) to
REM  start the app. It opens automatically at:
REM      http://127.0.0.1:%PORT%
REM
REM  PORT is set below. 5000 and 8000 are already used by PIMS and
REM  Book-to-Movie on this machine, so this defaults to 5050 --
REM  change the number on the next line any time you need a
REM  different one.
REM ============================================================

set PORT=5050

REM Always run from this script's own folder, no matter where it
REM was launched from (double-click, shortcut, another folder, etc).
cd /d "%~dp0"

if not exist venv (
    echo Creating a virtual environment in .\venv ...
    python -m venv venv
    if errorlevel 1 (
        echo.
        echo Could not create the virtual environment.
        echo Make sure Python 3.10+ is installed and on your PATH, then try again.
        pause
        exit /b 1
    )
)

call venv\Scripts\activate.bat

echo Checking dependencies...
pip install -r requirements.txt --quiet
if errorlevel 1 (
    echo.
    echo Failed to install dependencies. Check the messages above.
    pause
    exit /b 1
)

echo.
echo ============================================================
echo  Starting Tourism Management System on http://127.0.0.1:%PORT%
echo ============================================================
echo.

REM Run the server in its own window so it keeps running (and its
REM log is visible) after this setup window closes. This window
REM re-activates the same venv since a spawned window doesn't
REM always inherit it.
start "Tourism Management System - Server (close this window, or press CTRL+C in it, to stop the app)" cmd /k "call venv\Scripts\activate.bat && flask --app app run --debug --port %PORT%"

echo Waiting for the server to start...
timeout /t 3 /nobreak >nul

start "" http://127.0.0.1:%PORT%

echo.
echo The app should now be open in your browser.
echo If a browser tab didn't open, go to this address manually:
echo     http://127.0.0.1:%PORT%
echo.
echo The server itself keeps running in the other window titled
echo "Tourism Management System - Server". Close that window (or
echo press CTRL+C in it) whenever you want to stop the app.
echo.
pause
