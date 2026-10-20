@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo ========================================
echo   CapCut PC Course - Starting Server
echo ========================================
echo.

REM ---- ตรวจว่ามี Python ----
where py >nul 2>nul
if %errorlevel%==0 (
    set "PY=py"
) else (
    where python >nul 2>nul
    if %errorlevel%==0 (
        set "PY=python"
    ) else (
        echo [ERROR] ไม่พบ Python ในเครื่อง กรุณาติดตั้งก่อน
        echo ดาวน์โหลด: https://www.python.org/downloads/
        pause
        exit /b 1
    )
)

REM ---- สร้าง virtualenv ถ้ายังไม่มี ----
if not exist ".venv\Scripts\python.exe" (
    echo [1/4] กำลังสร้าง virtual environment...
    %PY% -m venv .venv
    if errorlevel 1 (
        echo [ERROR] สร้าง venv ไม่สำเร็จ
        pause
        exit /b 1
    )
) else (
    echo [1/4] พบ virtual environment แล้ว
)

REM ---- ใช้ python ใน venv ----
set "VENV_PY=.venv\Scripts\python.exe"

REM ---- ติดตั้ง dependencies ถ้ายังไม่มี flask ----
%VENV_PY% -c "import flask" >nul 2>nul
if errorlevel 1 (
    echo [2/4] กำลังติดตั้ง dependencies...
    %VENV_PY% -m pip install --upgrade pip >nul
    %VENV_PY% -m pip install -r requirements.txt
    if errorlevel 1 (
        echo [ERROR] ติดตั้ง dependencies ไม่สำเร็จ
        pause
        exit /b 1
    )
) else (
    echo [2/4] dependencies พร้อมแล้ว
)

REM ---- สร้าง .env จาก env.example ถ้ายังไม่มี ----
if not exist ".env" (
    if exist "env.example" (
        echo [3/4] ยังไม่มี .env กำลังคัดลอกจาก env.example...
        copy "env.example" ".env" >nul
        echo.
        echo   *** สำคัญ ***
        echo   กรุณาเปิดไฟล์ .env แล้วใส่ค่า PROMPTPAY_PHONE และ SECRET_KEY
        echo   กดปุ่มใดก็ได้เพื่อเปิด .env ใน Notepad...
        pause >nul
        start notepad ".env"
    ) else (
        echo [WARN] ไม่พบ env.example ข้ามการสร้าง .env
    )
) else (
    echo [3/4] พบไฟล์ .env แล้ว
)

REM ---- seed ข้อมูลครั้งแรก ----
%VENV_PY% -c "from app import Lesson; import sys" >nul 2>nul
echo [4/4] กำลังเริ่มเซิร์ฟเวอร์...
echo.
echo   เปิดเบราว์เซอร์ที่: http://127.0.0.1:5000
echo   กด CTRL+C เพื่อหยุดเซิร์ฟเวอร์
echo.

start "" http://127.0.0.1:5000
%VENV_PY% app.py

echo.
echo เซิร์ฟเวอร์หยุดทำงานแล้ว
pause