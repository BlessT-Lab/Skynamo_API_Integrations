@echo off
REM Build a standalone SkynamoGeo.exe from gui.py using PyInstaller.
REM Output: dist\SkynamoGeo.exe  (single double-clickable file, no console)
REM
REM First time only:  py -m pip install -r requirements.txt -r requirements-build.txt
REM Then run:         build.bat

setlocal
echo Building SkynamoGeo.exe ...

py -m PyInstaller --noconfirm --windowed --onefile --name SkynamoGeo ^
  --collect-all customtkinter ^
  --collect-submodules keyring.backends ^
  gui.py

if errorlevel 1 (
  echo.
  echo BUILD FAILED - see the PyInstaller output above.
  exit /b 1
)

echo.
echo Done. The app is at: dist\SkynamoGeo.exe
echo Note: one-file exes can trip antivirus heuristics on first run;
echo you may need to allow it in your AV / SmartScreen.
endlocal
