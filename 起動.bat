@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul
where py >nul 2>nul
if not errorlevel 1 (
    set "PYCMD=py -3"
    set "PYWCMD=pyw -3"
) else (
    where python >nul 2>nul
    if errorlevel 1 (
        echo Python 3.10 or newer is required.
        echo Install from https://www.python.org/downloads/windows/
        echo Check Add Python to PATH during installation.
        pause
        exit /b 1
    )
    set "PYCMD=python"
    set "PYWCMD=pythonw"
)
%PYCMD% -c "import sys;sys.exit(0 if sys.version_info >= (3,10) else 1)"
if errorlevel 1 (
    echo Python 3.10 or newer is required.
    pause
    exit /b 1
)
%PYCMD% -c "import tkinterdnd2" >nul 2>nul
if errorlevel 1 (
    echo Installing small drag-and-drop support module, tkinterdnd2 ...
    %PYCMD% -m pip install --user tkinterdnd2
    if errorlevel 1 echo Drag and drop may be unavailable. File/folder buttons will still work.
)
rem Launch the GUI without a console window; this window closes immediately.
start "" %PYWCMD% AnimeLineFinder.py
endlocal
