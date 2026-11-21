@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ==========================================
echo   本地语音输入 - 安装
echo ==========================================
echo.

python --version >nul 2>nul
if errorlevel 1 goto nopython

python -c "import sys; sys.exit(0 if sys.version_info[:2] in ((3,10),(3,11)) else 1)" >nul 2>nul
if errorlevel 1 (
  echo 警告：推荐 Python 3.10 或 3.11，当前版本可能装不上 keyboard 库。
  python --version
  echo.
  choice /c YN /m "继续安装吗"
  if errorlevel 2 exit /b 1
)

echo [1/3] 创建虚拟环境...
python -m venv .venv
if errorlevel 1 goto failed

call .venv\Scripts\activate.bat

echo [2/3] 升级 pip...
python -m pip install --upgrade pip

echo [3/3] 安装依赖，几分钟...
pip install -r requirements.txt
if errorlevel 1 goto failed

echo.
echo 装完了。
echo   双击 start_voice.vbs 启动
echo   第一次启动要下载模型，请耐心等
echo.
pause
exit /b 0

:nopython
echo 没找到 Python。
echo 请到 https://www.python.org/downloads/ 装 3.10 或 3.11，
echo 安装时一定要勾选 "Add python.exe to PATH"。
echo.
pause
exit /b 1

:failed
echo.
echo 安装失败，把上面的报错发出来看看。
echo.
pause
exit /b 1
