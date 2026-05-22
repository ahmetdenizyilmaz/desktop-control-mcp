@echo off
setlocal enabledelayedexpansion

rem Run in the folder this .bat is located in
pushd "%~dp0"

rem Enable Agent Teams
set "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1"

rem Claude executable (prefer your known path, fallback to PATH)
set "CLAUDE_EXE=%USERPROFILE%\.local\bin\claude.exe"
if not exist "%CLAUDE_EXE%" set "CLAUDE_EXE=claude"

rem Allow everything (no permission prompts) + forward any extra args
"%CLAUDE_EXE%" --dangerously-skip-permissions %*

popd
endlocal
