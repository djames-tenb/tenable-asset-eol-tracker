@echo off
cd /d "%~dp0"

if not exist venv (
    echo Creating virtual environment...
    python -m venv venv
)

echo Installing dependencies...
venv\Scripts\pip install --quiet --upgrade -r requirements.txt

echo.
echo ======================================
echo   Tenable Asset EOL Portal
echo   http://localhost:5555
echo ======================================
echo.

set PORT=5555
venv\Scripts\python app.py
pause
