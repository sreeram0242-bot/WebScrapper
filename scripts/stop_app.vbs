Option Explicit
Dim WshShell
Set WshShell = CreateObject("WScript.Shell")
' Free port 5000 and stop any running scraper instances
WshShell.Run "cmd /c for /f ""tokens=5"" %a in ('netstat -aon ^| findstr :5000 ^| findstr LISTENING') do taskkill /f /pid %a >nul 2>&1", 0, True
