Set WshShell = CreateObject("WScript.Shell")
' Get directory of this script
strDir = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)

' Run pythonw.exe completely invisible (0 = hidden window)
WshShell.CurrentDirectory = strDir
WshShell.Run "pythonw.exe app.py", 0, False

' Brief pause to allow Flask server to initialize
WScript.Sleep 1500

' Open the Glassmorphic Dashboard in user's default browser
WshShell.Run "http://127.0.0.1:5000"
