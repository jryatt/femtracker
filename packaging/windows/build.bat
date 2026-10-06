@echo off
REM Build FEM Tracker for Windows. Double-click, or run from a command prompt.
REM Produces in the dist folder:
REM   FEMTracker-<version>-setup.exe      installer (needs Inno Setup 6: https://jrsoftware.org/isdl.php)
REM   FEMTracker-<version>-portable.zip   unzip anywhere and run FEMTracker.exe
REM Needs Python 3.10 or newer from python.org (tick "Add python.exe to PATH" or use the py launcher).
setlocal EnableExtensions
pushd "%~dp0..\.." || goto :fail

set "PY="
where py >nul 2>&1 && set "PY=py -3"
if not defined PY (where python >nul 2>&1 && set "PY=python")
if not defined PY goto :nopython
%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1 || goto :nopython

echo === Preparing the build environment (build\venv)
if not exist "build\venv\Scripts\python.exe" (
  %PY% -m venv build\venv || goto :fail
)
"build\venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q --upgrade pip || goto :fail
"build\venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r packaging\windows\requirements-build.txt || goto :fail

"build\venv\Scripts\python.exe" packaging\windows\build.py %* || goto :fail
popd
echo.
echo Done. The files are in the dist folder.
if "%~1"=="" pause
exit /b 0

:nopython
echo [ERROR] Python 3.10 or newer was not found.
echo Install it from https://www.python.org/downloads/ and tick "Add python.exe to PATH".
:fail
echo.
echo Build failed; see the messages above.
popd
if "%~1"=="" pause
exit /b 1
