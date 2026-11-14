@echo off
rem Inner helper launched by the launcher in its own window.
rem
rem The name is deliberately ASCII. cmd.exe reads this launcher's own text using
rem the OEM code page, so a non-ASCII batch name written inside another .bat turns
rem into mojibake and can never be found ("is missing"), even though the file is
rem right there. Keep this filename ASCII, and do not rename it to Chinese.
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"

rem %1 = python launcher (py / python), %2 = script name, %3 = port
set "PY=%~1"
set "SCRIPT=%~2"
set "PORT=%~3"

echo.
echo   ============================================================
echo     MiniMax H3 Server
echo   ============================================================
echo.
echo   Close this window to stop the web interface.
echo.

%PY% "%SCRIPT%" serve --port %PORT% --open
set "CODE=%errorlevel%"

echo.
echo   The server stopped (exit code %CODE%).
echo   Press any key to close this window.
pause >nul
