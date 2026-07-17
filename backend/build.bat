@echo off
title MediaTrack — Build Software
echo ============================================
echo   Building MediaTrack.exe  (one-time step)
echo   This takes 2-5 minutes. Please wait...
echo ============================================
echo.

pip install -r requirements.txt pyinstaller

pyinstaller --noconfirm --onefile --name MediaTrack ^
  --add-data "..\frontend;frontend" ^
  --collect-all uvicorn --collect-all openpyxl ^
  launcher.py

echo.
if exist dist\MediaTrack.exe (
  echo ============================================
  echo   DONE!  Your software is ready:
  echo   backend\dist\MediaTrack.exe
  echo.
  echo   Copy MediaTrack.exe anywhere you like.
  echo   Double-click it to start the dashboard.
  echo ============================================
) else (
  echo   Build failed — send a screenshot of the
  echo   messages above to get it fixed.
)
pause
