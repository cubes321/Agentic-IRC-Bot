@echo off
REM Run the bot using the venv's Python directly. No `activate` needed —
REM calling the venv's python.exe makes that python pick up its own
REM site-packages without modifying the current shell's environment.
REM
REM If you ever recreate the venv with a different name, update the path.
REM If you ever skip the venv entirely (not recommended), replace the line
REM below with just:    python -m bot.main config.toml
"%~dp0.venv\Scripts\python.exe" -m bot.main config.toml
