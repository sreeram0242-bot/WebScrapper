@echo off
cd /d "%~dp0"
start "" wscript.exe //nologo "%~dp0stop_app.vbs"
exit
