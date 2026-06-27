@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion

echo ============================================
echo   PuddingClaw - 一键启动脚本
echo ============================================
echo.

cd /d "%~dp0\.."

if not defined BACKEND_HOST set BACKEND_HOST=0.0.0.0
if not defined BACKEND_PORT set BACKEND_PORT=8002
if not defined FRONTEND_PORT set FRONTEND_PORT=3000
set BACKEND_URL=http://127.0.0.1:%BACKEND_PORT%

where python >nul 2>&1
if errorlevel 1 (
    echo [错误] 未找到 Python，请先安装 Python 3.10+
    pause
    exit /b 1
)

where node >nul 2>&1
if errorlevel 1 (
    echo [错误] 未找到 Node.js，请先安装 Node.js 18+
    pause
    exit /b 1
)

echo [信息] Python 版本:
python --version
echo [信息] Node.js 版本:
node --version
echo.

echo [步骤 1/5] 检查 Python 虚拟环境...
if not exist "backend\.venv" (
    echo [信息] 创建虚拟环境 backend\.venv ...
    python -m venv backend\.venv
    if errorlevel 1 (
        echo [错误] 创建虚拟环境失败
        pause
        exit /b 1
    )
    echo [成功] 虚拟环境创建完成
) else (
    echo [信息] 虚拟环境已存在
)

echo.
echo [步骤 2/5] 安装后端依赖...
backend\.venv\Scripts\python.exe -m pip install -r backend\requirements.txt
if errorlevel 1 (
    echo [错误] 安装后端依赖失败
    pause
    exit /b 1
)
echo [成功] 后端依赖安装完成

echo.
echo [步骤 3/5] 检查环境变量配置...
if not exist "backend\.env" (
    if exist "backend\.env.example" (
        echo [警告] 未找到 backend\.env，已从 .env.example 复制模板
        copy backend\.env.example backend\.env >nul
        echo [提示] 请编辑 backend\.env 填写 API Key 后重新启动
    ) else (
        echo [警告] 未找到 backend\.env，请手动创建并配置 API Key
    )
) else (
    echo [成功] 环境变量配置文件已存在
)

echo.
echo [步骤 4/5] 安装前端依赖...
cd frontend
if not exist "node_modules" (
    echo [信息] 首次安装，运行 npm install...
    call npm install
    if errorlevel 1 (
        echo [错误] 安装前端依赖失败
        pause
        exit /b 1
    )
) else (
    echo [信息] node_modules 已存在，跳过安装
)
cd ..
echo [成功] 前端依赖检查完成
echo.

echo [步骤 5/5] 启动服务...
echo.
echo   后端 API:  %BACKEND_URL%
echo   前端界面:  http://127.0.0.1:%FRONTEND_PORT%
echo   API 文档:  %BACKEND_URL%/docs
echo.
echo ============================================
echo   按 Ctrl+C 停止所有服务
echo ============================================
echo.

cd backend
start /b "" cmd /c ".venv\Scripts\python.exe -m uvicorn app:app --host %BACKEND_HOST% --port %BACKEND_PORT% --reload"
cd ..

echo [信息] 等待后端启动...
timeout /t 3 /nobreak >nul

set BACKEND_PID=
for /f "tokens=2" %%a in ('tasklist /fi "imagename eq python.exe" /fo list 2^>nul ^| findstr /i "PID"') do (
    set BACKEND_PID=%%a
)

echo [信息] 启动前端服务...
cd frontend
if exist ".next" (
    echo [信息] 清理前端 dev 缓存 .next，避免旧 chunks 造成 404...
    rmdir /s /q ".next"
)
call npm run dev -- --host 0.0.0.0 --port %FRONTEND_PORT%

echo.
echo [信息] 正在停止后端服务...
if defined BACKEND_PID (
    taskkill /f /t /pid !BACKEND_PID! >nul 2>&1
)
for /f "tokens=5" %%p in ('netstat -ano 2^>nul ^| findstr ":%BACKEND_PORT%" ^| findstr "LISTENING"') do (
    taskkill /f /pid %%p >nul 2>&1
)
echo [成功] 所有服务已停止
pause
