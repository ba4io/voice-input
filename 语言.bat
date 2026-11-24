@echo off
chcp 65001 >nul
set F=%~dp0language.txt
if not exist "%F%" (echo zh)>"%F%"
set /p V=<"%F%"
if /i "%V%"=="zh" (
  (echo en)>"%F%"
  echo 语言：英文（强制英文输出）
) else if /i "%V%"=="en" (
  (echo tr)>"%F%"
  echo 语言：翻译（说中文，出英文）
) else if /i "%V%"=="tr" (
  (echo auto)>"%F%"
  echo 语言：自动（说什么出什么）
) else (
  (echo zh)>"%F%"
  echo 语言：中文（默认）
)
ping -n 3 127.0.0.1 >nul
