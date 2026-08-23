Set objShell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
objShell.Run """" & scriptDir & "\start_skywatch.bat""", 0, True
