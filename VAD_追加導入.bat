@echo off
setlocal EnableExtensions
cd /d "%~dp0"
chcp 65001 >nul
set "VENV=%LOCALAPPDATA%\AnimeLineFinder\.venv_animewhisper"
if exist "%~dp0.venv_animewhisper\Scripts\python.exe" set "VENV=%~dp0.venv_animewhisper"
if not exist "%VENV%\Scripts\python.exe" (
  echo Anime Whisper environment not found.
  echo First use the "Anime Whisper environment installer" button.
  pause
  exit /b 1
)
set "PY=%VENV%\Scripts\python.exe"
echo Installing Silero VAD into the existing Anime Whisper environment...
"%PY%" -m pip install --disable-pip-version-check "silero-vad>=6.0,<7.0"
if errorlevel 1 goto failed
"%PY%" -c "from silero_vad import load_silero_vad,get_speech_timestamps; model=load_silero_vad(onnx=False); print('Silero VAD installation: OK')"
if errorlevel 1 goto failed
echo.
echo Completed. The original Anime Whisper model is unchanged.
pause
exit /b 0
:failed
echo.
echo Installation failed. Check internet connection and disk space.
pause
exit /b 1
