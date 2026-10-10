@echo off
cd /d "%~dp0"
start "" wscript.exe //nologo "%~dp0scripts\webscarpper.vbs"
exit
