@echo off
setlocal
cd /d "%~dp0"
if errorlevel 1 goto setup_failed
if exist "%~dp0.venv\Scripts\python.exe" goto validate_existing
where py >nul 2>nul
if errorlevel 1 goto use_python
py -3 -B -c "import sys; sys.exit(sys.version_info < (3, 10))" >nul 2>nul
if errorlevel 1 goto use_python
py -3 -B -c "from pathlib import Path; from data.uploader_queue import load_upload_plan; load_upload_plan(Path.cwd())" >nul 2>nul
if errorlevel 1 goto plan_invalid
echo Preparing the uploader's local Python environment...
py -3 -m venv "%~dp0.venv"
if not errorlevel 1 goto check_dependencies
:use_python
where python >nul 2>nul
if errorlevel 1 goto python_missing
python -B -c "import sys; sys.exit(sys.version_info < (3, 10))" >nul 2>nul
if errorlevel 1 goto python_missing
python -B -c "from pathlib import Path; from data.uploader_queue import load_upload_plan; load_upload_plan(Path.cwd())" >nul 2>nul
if errorlevel 1 goto plan_invalid
echo Preparing the uploader's local Python environment...
python -m venv "%~dp0.venv"
if errorlevel 1 goto setup_failed
goto check_dependencies
:validate_existing
"%~dp0.venv\Scripts\python.exe" -B -c "from pathlib import Path; from data.uploader_queue import load_upload_plan; load_upload_plan(Path.cwd())" >nul 2>nul
if errorlevel 1 goto plan_invalid
:check_dependencies
"%~dp0.venv\Scripts\python.exe" -B -c "import google_auth_oauthlib.flow; import google.auth.transport.requests; import requests" >nul 2>nul
if not errorlevel 1 goto run_uploader
echo Installing the uploader's required Python packages...
"%~dp0.venv\Scripts\python.exe" -B -m pip install --no-cache-dir --disable-pip-version-check -r "%~dp0data\requirements.txt"
if errorlevel 1 goto setup_failed
"%~dp0.venv\Scripts\python.exe" -B -c "import google_auth_oauthlib.flow; import google.auth.transport.requests; import requests" >nul 2>nul
if errorlevel 1 goto setup_failed
:run_uploader
"%~dp0.venv\Scripts\python.exe" -B -m data.upload_videos %*
set "upload_result=%ERRORLEVEL%"
if "%upload_result%"=="0" exit /b 0
echo.
pause
exit /b %upload_result%
:python_missing
echo Python 3.10 or newer was not found. Install Python with the launcher or add python to PATH.
echo Run start.cmd again after installing Python.
pause
exit /b 1
:plan_invalid
echo.
echo The upload plan could not be validated. Check upload-order.txt and the uploader folder location.
echo Keep the uploader folder and source-video folder separate, then run start.cmd again.
pause
exit /b 1
:setup_failed
echo.
echo Uploader setup did not complete. Review the message above, then run start.cmd again.
pause
exit /b 1
