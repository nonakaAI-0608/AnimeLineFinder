@echo off
setlocal EnableExtensions
cd /d "%~dp0"
chcp 65001 >nul
set "PYTHON_EXE="
py -3.11 -c "import sys" >nul 2>nul
if not errorlevel 1 set "PYTHON_EXE=py -3.11"
if not defined PYTHON_EXE (
    py -3.12 -c "import sys" >nul 2>nul
    if not errorlevel 1 set "PYTHON_EXE=py -3.12"
)
if not defined PYTHON_EXE (
    py -3.10 -c "import sys" >nul 2>nul
    if not errorlevel 1 set "PYTHON_EXE=py -3.10"
)
if not defined PYTHON_EXE (
    python -c "import sys; assert (3,10) <= sys.version_info[:2] <= (3,12)" >nul 2>nul
    if not errorlevel 1 set "PYTHON_EXE=python"
)
if not defined PYTHON_EXE (
    echo.
    echo Python 3.10 - 3.12 was not found.
    echo Please install Python 3.11 x64 from https://www.python.org/downloads/
    echo Then run this file again.
    pause
    exit /b 1
)
rem New installs live in persistent LocalAppData to survive future ZIP upgrades.
set "VENV=%LOCALAPPDATA%\AnimeLineFinder\.venv_animewhisper"
if exist "%~dp0.venv_animewhisper\Scripts\python.exe" set "VENV=%~dp0.venv_animewhisper"
if not exist "%LOCALAPPDATA%\AnimeLineFinder" mkdir "%LOCALAPPDATA%\AnimeLineFinder"
if not exist "%VENV%\Scripts\python.exe" (
    echo Creating an isolated Anime Whisper environment ...
    %PYTHON_EXE% -m venv "%VENV%"
    if errorlevel 1 goto failed
)
set "PY=%VENV%\Scripts\python.exe"
echo.
echo Installing GPU-enabled PyTorch (CUDA 12.6)...
echo The first installation is large and requires internet access.
"%PY%" -m pip install --disable-pip-version-check --upgrade pip
if errorlevel 1 goto failed
"%PY%" -m pip install --disable-pip-version-check "torch==2.7.1" --index-url https://download.pytorch.org/whl/cu126
if errorlevel 1 goto failed
"%PY%" -m pip install --disable-pip-version-check "transformers==4.57.3" "accelerate>=1.0,<2" "numpy>=1.26,<3" "imageio-ffmpeg>=0.6,<1" "huggingface-hub>=0.34,<1" "safetensors>=0.4"
if errorlevel 1 goto failed
"%PY%" -m pip install --disable-pip-version-check "silero-vad>=6.0,<7.0"
if errorlevel 1 goto failed
"%PY%" -c "import torch,transformers,imageio_ffmpeg,silero_vad; print('OK Torch:',torch.__version__);print('CUDA:',torch.cuda.is_available());print('Transformers:',transformers.__version__); print('Silero VAD: OK')"
if errorlevel 1 goto failed
echo.
echo Finished. Launch Anime Line Finder, select Anime Whisper, then start transcribing.
echo The 3GB original model will be downloaded automatically at first use.
echo If CUDA is False, it will use CPU and take much longer.
pause
exit /b 0
:failed
echo.
echo Installation failed. Check internet connection, Python version and free disk space.
echo The environment is safe to retry: just run this file again.
pause
exit /b 1
