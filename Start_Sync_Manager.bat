@echo off
title Antigravity & Git Sync Studio
cd /d "%~dp0"
start "" pythonw "%~dp0agy_git_sync.py"
if errorlevel 1 (
    python "%~dp0agy_git_sync.py"
)
