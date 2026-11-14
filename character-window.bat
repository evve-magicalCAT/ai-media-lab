@echo off
rem Inner helper launched by the character launcher in its own window.
rem Name is deliberately ASCII: cmd.exe reads .bat text using the OEM code page,
rem so a non-ASCII filename written inside another .bat turns into mojibake and
rem is never found. Do not rename this file to a non-ASCII name.
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"

rem %1 = python launcher (py / python), %2 = port
set "PY=%~1"
set "PORT=%~2"

echo.
echo   ============================================================
echo     Character Cutout Server  (port %PORT%)
echo   ============================================================
echo.
echo   Close this window to stop the web interface.
echo.

%PY% character_web.py serve --port %PORT% --open
set "CODE=%errorlevel%"

echo.
echo   The server stopped (exit code %CODE%).
echo   Press any key to close this window.
pause >nul
