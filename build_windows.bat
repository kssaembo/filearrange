@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if errorlevel 1 (
  echo Install Python 3.12 x64 from python.org first, then run this file again.
  pause
  exit /b 1
)
if not exist .venv\Scripts\python.exe py -3.12 -m venv .venv
if errorlevel 1 goto failed
.venv\Scripts\python.exe -m pip install -r requirements-build.txt
if errorlevel 1 goto failed
.venv\Scripts\python.exe -m unittest discover -s tests -v
if errorlevel 1 goto failed
.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean SafeFileOrganizer.spec
if errorlevel 1 goto failed
echo.
echo SUCCESS: dist\SafeFileOrganizer.exe
pause
exit /b 0
:failed
echo.
echo Build failed. Please copy the error shown above.
pause
exit /b 1
