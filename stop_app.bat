@echo off
cd /d "%~dp0"
start "" wscript.exe //nologo "%~dp0scripts\stop_app.vbs"
exit
