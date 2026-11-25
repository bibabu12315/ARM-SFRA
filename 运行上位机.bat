@echo off
setlocal
cd /d "%~dp0"

REM =====================================================================
REM  SFRA Studio launcher
REM
REM  MUST use .venv python, NOT the one on PATH:
REM  system python usually lacks PyQt5 -> ModuleNotFoundError.
REM  First run creates the venv and installs deps (needs network).
REM
REM  [ENCODING RULE - DO NOT CHANGE]
REM  Keep this file PURE ASCII (7-bit) with CRLF line endings.
REM  Reason: cmd.exe parses .bat using the system ANSI code page
REM  (GBK on zh-CN) and switches code page only AFTER the parser has
REM  already read ahead. Non-ASCII comments therefore get mangled and
REM  their bytes may be executed as commands, producing a burst of
REM  "'xx' is not recognized as an internal or external command".
REM  A UTF-8 BOM does not help either: cmd echoes it as part of the
REM  first command. Pure ASCII is the only reliable option.
REM =====================================================================

set "PY=.venv\Scripts\python.exe"

if not exist "%PY%" goto make_venv
goto run

:make_venv
echo [1/2] Creating virtual environment .venv ...
where py >nul 2>&1
if %errorlevel%==0 (
    py -3 -m venv .venv
) else (
    python -m venv .venv
)
if not exist "%PY%" (
    echo.
    echo [ERROR] Failed to create venv.
    echo         Install Python 3.9+ and make sure python or py is on PATH.
    pause
    exit /b 1
)
echo [2/2] Installing dependencies (first run takes 1-2 min) ...
"%PY%" -m pip install --upgrade pip
"%PY%" -m pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo [ERROR] Dependency install failed. Check network and retry.
    pause
    exit /b 1
)

:run
echo Starting SFRA Studio ...

REM =====================================================================
REM  Why pythonw.exe (no console) and not python.exe:
REM
REM  With python.exe, cmd.exe opens a console window first and the Qt
REM  window sits ON TOP of it. Both belong to the same process, but the
REM  taskbar groups them under ONE button whose "primary" window is the
REM  console. Clicking that taskbar button focuses the console, and the
REM  Qt window gets minimized back immediately -- the user sees the window
REM  flash up and vanish, and it looks exactly like a crash. It is not.
REM
REM  pythonw.exe has no console at all, so the Qt window is the only
REM  window of the process and taskbar activation works normally.
REM
REM  Trade-off: stdout/stderr are gone, so a Python traceback would be
REM  invisible. That is covered by src/main.py installing sys.excepthook,
REM  which writes sfra_crash.log next to this script and pops a dialog.
REM =====================================================================
set "PYW=.venv\Scripts\pythonw.exe"
if not exist "%PYW%" set "PYW=%PY%"

REM The launcher does not need to wait for the GUI, but keeping it in the
REM foreground makes the console usable for a quick "did it start" check.
"%PYW%" src\main.py
if errorlevel 1 (
    echo.
    echo [ERROR] Program exited with an error.
    echo         See sfra_crash.log in this folder for details.
    if exist "sfra_crash.log" (
        echo.
        echo ---------- sfra_crash.log ^(tail^) ----------
        powershell -NoProfile -Command "Get-Content 'sfra_crash.log' -Tail 40"
        echo ---------------------------------------------
    )
    pause
)
endlocal
