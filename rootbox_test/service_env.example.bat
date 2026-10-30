@echo off
rem ============================================================================
rem 现场配置示例（部署到生产机时用）
rem
rem 用法：把本文件复制成 service_env.bat，按目标机实际路径填好。
rem   copy service_env.example.bat service_env.bat
rem
rem service_env.bat 已在 .gitignore 里，不会被提交，也不会互相覆盖。
rem run_service.bat 启动时会自动 call 它。
rem ============================================================================

rem 落图目录：小车把 <批次>\<编号>\1.png + 2.png 落到这里
set "ROOTBOX_QC_WATCH_DIR=D:\qc\in"

rem 输出目录：报告 JSON、重拍队列、服务状态、日志都在这下面
set "ROOTBOX_QC_OUTPUT_DIR=D:\qc\out"

rem 拼接成品根目录：必须是“成品的父目录”，服务会按批次建子目录
rem   例：设成 D:\qc\save，则成品落在 D:\qc\save\<批次>\<编号>.png
set "ROOTBOX_QC_STITCH_DIR=D:\qc\save"

rem 拼接脚本：需提供 stitch_from_qc(img_a, img_b, out_path)
set "ROOTBOX_QC_STITCH_SCRIPT=D:\qc\stitch_script.py"

rem Python 解释器（该机器上装了 cv2/numpy 的那个）
set "ROOTBOX_QC_PYTHON=python"

rem 健康检查监听地址/端口（/health /metrics）
rem set "ROOTBOX_QC_HEALTH_PORT=8090"

rem 可选：HTTP 回调（不给就只写反馈文件）
rem set "ROOTBOX_QC_CALLBACK_URL=http://<小车IP>:<端口>/qc/recapture"

rem 可选：强制 shadow（只记录不拦车）。观察期结束后把这行删掉/注释掉
rem set "ROOTBOX_QC_SHADOW=1"
