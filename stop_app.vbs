Option Explicit
Dim WshShell
Set WshShell = CreateObject("WScript.Shell")
WshShell.Run "cmd /c for /f ""tokens=5"" %a in ('netstat -aon ^| findstr :5000 ^| findstr LISTENING') do taskkill /f /pid %a >nul 2>&1", 0, True
