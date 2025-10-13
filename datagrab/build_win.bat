@echo off
setlocal enableextensions
rem ==== go to script dir ====
cd /d "%~dp0"

rem ==== settings ====
set "VENV_DIR=.venv"
set "RELEASE_DIR=release"

echo.
echo [1/6] Create/activate venv...
rem Try Python launcher first, then fallback to python
set "PYLAUNCH=py -3"
py -3 -V >nul 2>nul || set "PYLAUNCH=python"

if not exist "%VENV_DIR%\Scripts\python.exe" (
    %PYLAUNCH% -m venv "%VENV_DIR%"
    if errorlevel 1 goto :fail
)

set "PY=%VENV_DIR%\Scripts\python.exe"

echo.
echo [2/6] Upgrade pip/setuptools/wheel...
"%PY%" -m pip install -U pip setuptools wheel
if errorlevel 1 goto :fail

echo.
echo [3/6] Install project requirements (if any)...
if exist "requirements.txt" (
    "%PY%" -m pip install -r requirements.txt
    if errorlevel 1 goto :fail
) else (
    echo requirements.txt not found - skipping
)

echo.
echo [4/6] Install PyInstaller...
"%PY%" -m pip install pyinstaller
if errorlevel 1 goto :fail

echo.
echo [5/6] Build datagrabber_69.exe (onefile, no console)...
"%PY%" -m PyInstaller "datagrabber_69.py" ^
  --name datagrabber_69 ^
  --onefile ^
  --noconsole ^
  --clean ^
  --noconfirm ^
  --collect-all PIL ^
  --collect-all mss ^
  --hidden-import pynput.keyboard._win32 ^
  --hidden-import pynput.mouse._win32
if errorlevel 1 goto :fail

echo.
echo [6/6] Build DatasetRecorder.exe (onefile, GUI)...
"%PY%" -m PyInstaller "tk_dataset_recorder.py" ^
  --name DatasetRecorder ^
  --onefile ^
  --windowed ^
  --clean ^
  --noconfirm
if errorlevel 1 goto :fail

echo.
echo Packaging release folder...
if not exist "%RELEASE_DIR%" mkdir "%RELEASE_DIR%"
copy /Y ".\dist\datagrabber_69.exe" "%RELEASE_DIR%\">nul
copy /Y ".\dist\DatasetRecorder.exe" "%RELEASE_DIR%\">nul

echo.
echo ===================== DONE =====================
echo EXEs are here: "%RELEASE_DIR%\"
echo Run: "%RELEASE_DIR%\DatasetRecorder.exe"
echo.
goto :eof

:fail
echo.
echo ************ BUILD FAILED ************
echo Check the messages above. Common fixes:
echo  - Ensure Python 3.x is installed and available as "py" or "python"
echo  - Remove old "build\" / "dist\" / "__pycache__\" and try again
echo  - Verify requirements.txt installs without errors
echo.
exit /b 1
