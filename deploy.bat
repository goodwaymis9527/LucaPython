@echo off
rem deploy.bat = dispatcher only. Logic lives in aws/ops.py.
rem Keep this file under 1024 bytes: cmd reads a .bat in 1024-byte blocks, and a line that
rem straddles a block boundary can be cut in half ("not recognized"). Chinese messages are
rem printed by aws/ops.py (Python writes UTF-8), so nothing here needs to be multi-byte.
chcp 65001 >nul
cd /d "%~dp0" || exit /b 1
if "%~1"=="" (
    python aws\ops.py deploy
) else (
    python aws\ops.py %*
)

exit /b %ERRORLEVEL%
