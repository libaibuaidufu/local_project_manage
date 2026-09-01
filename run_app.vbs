' 创建一个Shell对象
Set objShell = CreateObject("Wscript.Shell")

' 切换到项目目录
objShell.CurrentDirectory = "D:\Python\Project\HTML\python_manage"

' 运行虚拟环境中的pythonw，0表示隐藏窗口
objShell.Run """D:\Python\Project\HTML\python_manage\.venv\Scripts\pythonw.exe"" ""app.py""", 0, True