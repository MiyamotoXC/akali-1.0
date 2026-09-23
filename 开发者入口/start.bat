@echo off
cd /d "%~dp0"

echo ========================================
echo   Developer Console - Launcher
echo ========================================
echo.

REM Check if port 8501 is in use
netstat -ano | findstr ":8501 " >nul 2>&1
if %errorlevel%==0 (
    echo Port 8501 is in use. Attempting to free it...
    for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8501 " ^| findstr "LISTENING"') do (
        taskkill /F /PID %%a >nul 2>&1
    )
    timeout /t 2 /nobreak >nul
)

python -m streamlit run app.py --server.headless true --server.port 8501

if errorlevel 1 (
    echo.
    echo Failed to start. Please check Python and streamlit installation.
)
echo.
pause