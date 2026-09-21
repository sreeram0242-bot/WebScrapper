@echo off
echo Stopping any running Google Maps Scraper background processes...
for /f "tokens=5" %%a in ('netstat -aon ^| findstr :5000') do (
    taskkill /f /pid %%a >nul 2>&1
)
echo Done! Background scraper stopped.
pause
