Set WshShell = CreateObject("WScript.Shell")
WshShell.CurrentDirectory = "c:\Users\ckane\Desktop\goldflow1"
WshShell.Run """C:\Users\ckane\AppData\Local\Programs\Python\Python314\pythonw.exe"" supervisor_all_ports.py", 0, False
