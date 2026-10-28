@echo off
rem 导出坏图启动器：双击直接运行（使用 export_bad_photos.py 顶部默认的输入/输出目录），
rem 也可带参覆盖，如：export_bad_photos.bat -i D:\data -o D:\qc_bad --dry-run
rem 本文件与 export_bad_photos.py、rootbox_qc\ 放在同一目录即可独立使用。
chcp 65001 >nul
cd /d "%~dp0"

rem 按机器实际环境改这行；也可留 python（前提是已在 PATH 上）
set "PY=D:\Anaconda3\envs\root-cleaner\python.exe"
if not exist "%PY%" set "PY=python"

"%PY%" export_bad_photos.py %*
echo.
pause
