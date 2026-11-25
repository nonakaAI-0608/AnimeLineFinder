@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if %ERRORLEVEL%==0 (set "PYCMD=py -3") else (set "PYCMD=python")
%PYCMD% -m pip install --user --upgrade pyinstaller tkinterdnd2
if ERRORLEVEL 1 goto error
%PYCMD% -m PyInstaller --noconfirm --clean --onedir --windowed --name AnimeLineFinder --collect-all tkinterdnd2 AnimeLineFinder.py
if ERRORLEVEL 1 goto error
copy /Y "anime_whisper_worker.py" "dist\AnimeLineFinder\anime_whisper_worker.py" >nul
copy /Y "core.py" "dist\AnimeLineFinder\core.py" >nul
copy /Y "AnimeWhisper_導入.bat" "dist\AnimeLineFinder\AnimeWhisper_導入.bat" >nul
copy /Y "qwen_asr_worker.py" "dist\AnimeLineFinder\qwen_asr_worker.py" >nul
copy /Y "Qwen3ASR_導入.bat" "dist\AnimeLineFinder\Qwen3ASR_導入.bat" >nul
copy /Y "VAD_追加導入.bat" "dist\AnimeLineFinder\VAD_追加導入.bat" >nul
echo.
echo Done: dist\AnimeLineFinder\AnimeLineFinder.exe
echo Run this EXE (the entire dist\AnimeLineFinder folder is required).
pause
exit /b 0
:error
echo.
echo Packaging failed. Check Python and pip.
pause
exit /b 1
