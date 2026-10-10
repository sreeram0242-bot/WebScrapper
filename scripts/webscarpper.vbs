Option Explicit

Dim WshShell, fso, strDir
Set WshShell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

' 1. Resolve current directory (handle execution from root or scripts/)
strDir = fso.GetParentFolderName(WScript.ScriptFullName)
If LCase(fso.GetFileName(strDir)) = "scripts" Then
    strDir = fso.GetParentFolderName(strDir)
End If
WshShell.CurrentDirectory = strDir

' 2. Cleanly free port 5000 if an old instance is still running
On Error Resume Next
WshShell.Run "cmd /c for /f ""tokens=5"" %a in ('netstat -aon ^| findstr :5000 ^| findstr LISTENING') do taskkill /f /pid %a >nul 2>&1", 0, True
On Error GoTo 0

' 3. Python detection helper
Function FindPython()
    Dim candidates, p, res
    candidates = Array("py -3", "python", "python3")
    For Each p In candidates
        On Error Resume Next
        res = WshShell.Run("cmd /c " & p & " -c ""import sys; sys.exit(0)""", 0, True)
        If Err.Number = 0 And res = 0 Then
            FindPython = p
            Exit Function
        End If
        On Error GoTo 0
    Next

    Dim appData, progFiles, pf86
    appData = WshShell.ExpandEnvironmentStrings("%LOCALAPPDATA%")
    progFiles = WshShell.ExpandEnvironmentStrings("%ProgramFiles%")
    pf86 = WshShell.ExpandEnvironmentStrings("%ProgramFiles(x86)%")

    Dim searchFolders, sf, subF, pyPath
    searchFolders = Array( _
        appData & "\Programs\Python", _
        appData & "\Python", _
        progFiles, _
        pf86, _
        "C:\" _
    )

    For Each sf In searchFolders
        If fso.FolderExists(sf) Then
            For Each subF In fso.GetFolder(sf).SubFolders
                If InStr(LCase(subF.Name), "python") > 0 Then
                    pyPath = subF.Path & "\python.exe"
                    If fso.FileExists(pyPath) Then
                        On Error Resume Next
                        res = WshShell.Run("""" & pyPath & """ -c ""import sys; sys.exit(0)""", 0, True)
                        If Err.Number = 0 And res = 0 Then
                            FindPython = """" & pyPath & """"
                            Exit Function
                        End If
                        On Error GoTo 0
                    End If
                End If
            Next
        End If
    Next

    FindPython = ""
End Function

' 4. Verify or create virtual environment
Dim venvDir, venvPy, sysPython, needRebuild, testRes
venvDir = strDir & "\venv"
venvPy = venvDir & "\Scripts\python.exe"
needRebuild = False

If fso.FileExists(venvPy) Then
    On Error Resume Next
    testRes = WshShell.Run("""" & venvPy & """ -c ""import sys; sys.exit(0)""", 0, True)
    If Err.Number <> 0 Or testRes <> 0 Then
        needRebuild = True
    End If
    On Error GoTo 0
Else
    needRebuild = True
End If

If needRebuild Then
    sysPython = FindPython()
    If sysPython = "" Then
        MsgBox "Python 3 was not detected on this computer." & vbCrLf & vbCrLf & _
               "Google Maps Scraper requires Python 3.9 or higher." & vbCrLf & _
               "Opening official Python download page in your browser...", _
               vbExclamation, "Python Required - Google Maps Scraper"
        WshShell.Run "https://www.python.org/downloads/"
        WScript.Quit 1
    End If

    If fso.FolderExists(venvDir) Then
        On Error Resume Next
        fso.DeleteFolder venvDir, True
        On Error GoTo 0
    End If

    ' Create isolated virtual environment silently
    WshShell.Run sysPython & " -m venv """ & venvDir & """", 0, True
End If

' 5. Verify dependencies silently
Dim reqFile, depRes
reqFile = strDir & "\requirements.txt"
On Error Resume Next
depRes = WshShell.Run("""" & venvPy & """ -c ""import bs4, selenium, webdriver_manager, pandas, flask, flask_cors""", 0, True)
If Err.Number <> 0 Or depRes <> 0 Then
    If fso.FileExists(reqFile) Then
        WshShell.Run """" & venvPy & """ -m pip install -r """ & reqFile & """", 0, True
    End If
End If
On Error GoTo 0

' 6. Launch Flask web server completely hidden in background
Dim appPy, logFile, launchCmd
appPy = strDir & "\app.py"
logFile = strDir & "\output\scraper_server.log"
launchCmd = "cmd /c """"" & venvPy & """ """ & appPy & """ > """ & logFile & """ 2>&1"""
WshShell.Run launchCmd, 0, False

' 7. Wait for server to become responsive
Dim http, isReady, attempts
isReady = False
attempts = 0

Do While (Not isReady) And (attempts < 20)
    WScript.Sleep 500
    attempts = attempts + 1
    On Error Resume Next
    Set http = CreateObject("MSXML2.ServerXMLHTTP.6.0")
    http.Open "GET", "http://127.0.0.1:5000", False
    http.setTimeouts 500, 500, 500, 500
    http.Send
    If Err.Number = 0 Then
        If http.Status = 200 Then
            isReady = True
        End If
    End If
    Set http = Nothing
    On Error GoTo 0
Loop

' 8. Open default browser directly to the Glassmorphic Dashboard
WshShell.Run "http://127.0.0.1:5000"
