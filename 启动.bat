@echo off
cd /d D:\Python\Project\HTML\python_manage
if errorlevel 1 (
    echo 目录切换失败，请检查路径！
    pause
    exit /b
)
uv run app.py  
pause