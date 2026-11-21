@echo off
chcp 65001 >nul
set F=%~dp0whisper.txt
if not exist "%F%" (echo 0)>"%F%"
set /p V=<"%F%"
if "%V%"=="1" (
  (echo 0)>"%F%"
  echo 耳语模式：关
) else (
  (echo 1)>"%F%"
  echo 耳语模式：开（小声说话也能识别）
)
ping -n 3 127.0.0.1 >nul
