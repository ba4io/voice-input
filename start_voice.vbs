Set fso = CreateObject("Scripting.FileSystemObject")
d = fso.GetParentFolderName(WScript.ScriptFullName)
exe = d & "\.venv\Scripts\pythonw.exe"
If Not fso.FileExists(exe) Then exe = "pythonw.exe"
Set sh = CreateObject("WScript.Shell")
sh.Run """" & exe & """ """ & d & "\voice_input.py""", 0, False
