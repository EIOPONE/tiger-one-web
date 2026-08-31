@echo off
setlocal

cd /d "%~dp0"

echo ============================================
echo  Tiger One Web
echo ============================================
echo.

REM --- Find a working Python launcher (py first, then python) ---
where py >nul 2>nul
if %errorlevel%==0 (
    set "PYLAUNCH=py"
) else (
    where python >nul 2>nul
    if %errorlevel%==0 (
        set "PYLAUNCH=python"
    ) else (
        echo Python was not found on this PC.
        echo.
        echo Install it from https://www.python.org/downloads/
        echo IMPORTANT: on the first setup screen, tick "Add python.exe to PATH"
        echo before clicking Install. Then run this file again.
        echo.
        pause
        exit /b 1
    )
)

echo Using: %PYLAUNCH%
%PYLAUNCH% --version
echo.

REM --- Create the virtual environment on first run only ---
if not exist "venv\Scripts\python.exe" (
    echo First time setup - creating a Python environment for this app...
    %PYLAUNCH% -m venv venv
    if not exist "venv\Scripts\python.exe" (
        echo.
        echo Could not create the virtual environment. See the error above.
        pause
        exit /b 1
    )
)

REM --- Install/update dependencies (fast no-op if already installed) ---
echo Checking dependencies...
"venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
"venv\Scripts\python.exe" -m pip install --quiet -r requirements.txt
if errorlevel 1 (
    echo.
    echo Installing dependencies failed. See the error above.
    pause
    exit /b 1
)

echo.
echo ============================================
echo  Starting Tiger One Web
echo  Leave this window open while you use it.
echo  Open this in your browser: http://127.0.0.1:8000/docs
echo  Press CTRL+C in this window to stop it.
echo ============================================
echo.

"venv\Scripts\python.exe" -m uvicorn app.main:app --reload

pause
