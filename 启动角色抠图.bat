@echo off
rem ============================================================
rem  Character cutout web UI - double-click launcher
rem  Starts the local web interface on port 8001 and opens it.
rem
rem  Port 8001 on purpose: port 8000 belongs to the video interface
rem  (04_video.py), so both can run at the same time.
rem
rem  Pure ASCII on purpose: cmd.exe reads .bat files using the console
rem  code page (GBK on a Chinese Windows), so Chinese text written here
rem  would come out as mojibake. Chinese messages are printed by Python.
rem ============================================================
setlocal EnableExtensions EnableDelayedExpansion
title Character Cutout
color 0B

cd /d "%~dp0"
set "SCRIPT=character_web.py"
set "PAGE=05_character.html"
set "SERVER_BAT=character-window.bat"
set "PORT=8001"
set "URL=http://127.0.0.1:%PORT%/"

echo.
echo   ============================================================
echo     Character Cutout  -  keep the character, blank background
echo   ============================================================
echo.
echo   Preparing the local web interface... please keep this window open.
echo.

rem ---------- 1. required files ----------
if not exist "%SCRIPT%" (
    echo   [X] %SCRIPT% is missing; it must sit next to this launcher.
    goto :hold
)
if not exist "05_character.py" (
    echo   [X] 05_character.py is missing; the server needs it.
    goto :hold
)
if not exist "%PAGE%" (
    echo   [X] %PAGE% is missing; the web interface needs it.
    goto :hold
)
if not exist "%SERVER_BAT%" (
    echo   [X] %SERVER_BAT% is missing; it must sit next to this launcher.
    goto :hold
)

rem ---------- 2. key must be filled in .env ----------
set "KEYLINE="
if exist ".env" for /f "usebackq tokens=1,* delims==" %%A in (".env") do (
    if /i "%%~A"=="ARK_API_KEY" if not defined KEYLINE set "KEYLINE=%%B"
)
if not defined KEYLINE (
    echo   [X] ARK_API_KEY is not set in .env . Fill in your key first.
    goto :hold
)

rem ---------- 3. find python ----------
set "PY="
where py >nul 2>nul
if not errorlevel 1 set "PY=py"
if not defined PY (
    where python >nul 2>nul
    if not errorlevel 1 set "PY=python"
)
if not defined PY (
    py -3 --version 1>nul 2>nul
    if not errorlevel 1 set "PY=py -3"
)
if not defined PY (
    python --version 1>nul 2>nul
    if not errorlevel 1 set "PY=python"
)
if not defined PY (
    echo   [X] Python was not found.
    echo       Install Python 3 from python.org and tick "Add python.exe to PATH".
    goto :hold
)
echo   [OK] Python: %PY%

rem ---------- 4. already running? ----------
set "RUNNING="
for /f "usebackq delims=" %%R in (`powershell -NoProfile -Command "try { Invoke-WebRequest -Uri '%URL%api/info' -UseBasicParsing -TimeoutSec 3 | Out-Null; 'OK' } catch { 'NO' }"`) do set "RUNNING=%%R"
if /i "%RUNNING%"=="OK" (
    echo   [i] The interface is already running; opening the browser.
    echo       To stop it, close that other window.
    start "" "%URL%"
    goto :eof
)

rem ---------- 5. start the server, then wait until it answers ----------
rem The helper must be started through "cmd /c" with the batch path double
rem quoted: "start title some.bat" makes Windows treat the .bat as a document
rem and silently do nothing.
echo   [..] Starting the server, this may take a few seconds...
start "Character Cutout Server" cmd /c ""%SERVER_BAT%" %PY% %PORT%"

set /a TRIES=0
:waitloop
set /a TRIES+=1
set "READY="
for /f "usebackq delims=" %%R in (`powershell -NoProfile -Command "try { Invoke-WebRequest -Uri '%URL%api/info' -UseBasicParsing -TimeoutSec 2 | Out-Null; 'OK' } catch { 'NO' }"`) do set "READY=%%R"
if /i "%READY%"=="OK" goto :ready
if %TRIES% GEQ 25 goto :timeout
rem Start-Sleep instead of "timeout /t": timeout refuses to run when stdin is
rem redirected, which would abort this loop and leave the server unverified
powershell -NoProfile -Command "Start-Sleep -Milliseconds 700" >nul 2>nul
goto :waitloop

:ready
echo   [OK] Server is ready; the browser will open now.
echo.
echo   Only "Analyze" and "Generate" call the paid API; uploading and
echo   crop previews are free.
echo   You may minimise this window. The server runs in the other window.
powershell -NoProfile -Command "Start-Sleep -Seconds 5" >nul 2>nul
goto :eof

:timeout
echo   [!] The server did not answer in time.
echo       Check the "Character Cutout Server" window for the error message.
goto :hold

:hold
echo.
echo   Press any key to close this window.
pause >nul
goto :eof
