@echo off
rem ============================================================
rem  MiniMax H3 video generator - double-click launcher
rem  Double-click this file to open the local web interface.
rem
rem  This file is deliberately pure ASCII: cmd.exe reads .bat files
rem  using the console code page (GBK on a Chinese Windows), so any
rem  Chinese text written here would show up as mojibake. All Chinese
rem  messages are printed by the Python script instead, which writes
rem  UTF-8 to the console correctly.
rem ============================================================
setlocal EnableExtensions EnableDelayedExpansion
title MiniMax H3 Video
color 0B

rem switch to this file's own folder, so double-clicking works from anywhere
cd /d "%~dp0"
set "SCRIPT=04_video.py"
set "PAGE=04_video.html"
set "PORT=8000"
set "URL=http://127.0.0.1:%PORT%/"

echo.
echo   ============================================================
echo     MiniMax H3 Video Generator
echo   ============================================================
echo.
echo   Preparing the local web interface... please keep this window open.
echo.

rem ---------- 1. the python script must sit next to this file ----------
if not exist "%SCRIPT%" (
    echo   [X] %SCRIPT% is missing.
    echo       Put this launcher in the same folder as the script.
    goto :hold
)

rem ---------- 2. the page file is required by the server ----------
if not exist "%PAGE%" (
    echo   [X] %PAGE% is missing; the web interface needs it.
    goto :hold
)

rem ---------- 3. the key must be filled in .env ----------
rem grab the value of the first line starting with MINIMAX_API_KEY= ; quotes are left
rem in place on purpose, so a placeholder like "YOUR_KEY" is passed through and the
rem python script rejects it with a clear message
set "KEYLINE="
if exist ".env" for /f "usebackq tokens=1,* delims==" %%A in (".env") do (
    if /i "%%~A"=="MINIMAX_API_KEY" if not defined KEYLINE set "KEYLINE=%%B"
)
if not defined KEYLINE (
    echo   [X] MINIMAX_API_KEY is not set in .env . Fill in your key first.
    goto :hold
)

rem ---------- 4. find a usable python ----------
rem the "1>nul" is deliberate: it swallows python's version text, 2^>nul swallows
rem the error text, and errorlevel is tested without delayed expansion
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

rem the server runs in its own window via this helper batch; see the comment there
rem for why the "start ... cmd /k python script.py" one-liner does not work.
rem The helper name MUST stay ASCII: cmd.exe reads this file using the OEM code
rem page, so a Chinese name written here turns into mojibake and is never found
rem ("is missing") even though the file is sitting right next to this one.
set "SERVER_BAT=server-window.bat"
if not exist "%SERVER_BAT%" (
    echo   [X] %SERVER_BAT% is missing; it must sit next to this launcher.
    goto :hold
)

rem ---------- 5. is the interface already running on this port? ----------
rem "OK" means something already answers our own API on this port
set "RUNNING="
for /f "usebackq delims=" %%R in (`powershell -NoProfile -Command "try { Invoke-WebRequest -Uri '%URL%api/info' -UseBasicParsing -TimeoutSec 3 | Out-Null; 'OK' } catch { 'NO' }"`) do set "RUNNING=%%R"
if /i "%RUNNING%"=="OK" (
    echo   [i] The interface is already running; opening the browser.
    echo       To stop it, close that other window.
    start "" "%URL%"
    goto :eof
)

rem ---------- 6. start the server, then wait until it really answers ----------
rem --open inside the helper lets the python script open the browser itself once the
rem port answers, so we never race the server and land on a blank page.
rem
rem The command must be run through "cmd /c" with the batch path double-quoted:
rem   start "title" "some.bat"        <- Windows treats the .bat as a DOCUMENT and
rem                                      silently does nothing (no window, no error)
rem   start "title" cmd /c ""some.bat""  <- runs it properly
rem So do not "simplify" this line back to a bare batch path.
echo   [..] Starting the server, this may take a few seconds...
start "MiniMax H3 Server" cmd /c ""%SERVER_BAT%" %PY% "%SCRIPT%" %PORT%"

set /a TRIES=0
:waitloop
set /a TRIES+=1
set "READY="
for /f "usebackq delims=" %%R in (`powershell -NoProfile -Command "try { Invoke-WebRequest -Uri '%URL%api/info' -UseBasicParsing -TimeoutSec 2 | Out-Null; 'OK' } catch { 'NO' }"`) do set "READY=%%R"
if /i "%READY%"=="OK" goto :ready
if %TRIES% GEQ 20 goto :timeout
rem Start-Sleep instead of "timeout /t": timeout refuses to run when stdin is
rem redirected, which would abort this loop and leave the server unverified
powershell -NoProfile -Command "Start-Sleep -Milliseconds 700" >nul 2>nul
goto :waitloop

:ready
echo   [OK] Server is ready; the browser will open now.
echo.
echo   Only "Generate" costs money; "Preview" is free.
echo   You may minimise this window. The server runs in the other window.
powershell -NoProfile -Command "Start-Sleep -Seconds 5" >nul 2>nul
goto :eof

:timeout
echo   [!] The server did not answer in time.
echo       Check the "MiniMax H3 Server" window for the error message.
goto :hold

:hold
echo.
echo   Press any key to close this window.
pause >nul
goto :eof
