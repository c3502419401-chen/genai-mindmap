@echo off
REM ---------------------------------------------------------------------------
REM Ensure Neo4j is running. Written in plain ASCII on purpose: this machine's
REM cmd.exe reads .bat files in code page 936, so UTF-8 comments get split into
REM half-characters that cmd then tries to execute ("'<junk>' is not recognized
REM as an internal or external command"). Same rule applies to run-neo4j.bat.
REM
REM Why this exists: Neo4j does NOT start itself here. `neo4j.bat start` goes
REM through the Windows service, the service is not installed, and installing it
REM needs admin. schtasks /create /sc onlogon is denied too (Access is denied).
REM So "start the backend" is made to also mean "make sure the graph is up":
REM run-backend.bat calls this script once, before uvicorn.
REM
REM Idempotent: if 7687 is already listening this returns immediately.
REM Log: .visioncheck\ensure-neo4j.log (Neo4j's own output: .visioncheck\neo4j.log)
REM ---------------------------------------------------------------------------
set "PORT=7687"
set "LOG=%~dp0.visioncheck\ensure-neo4j.log"
if not exist "%~dp0.visioncheck" mkdir "%~dp0.visioncheck"

netstat -ano | findstr ":%PORT%" | findstr "LISTENING" >nul 2>&1
if not errorlevel 1 (
    echo [ensure-neo4j] port %PORT% already listening, nothing to do.
    exit /b 0
)

echo [ensure-neo4j] port %PORT% is free, starting Neo4j ...
>>"%LOG%" echo [%DATE% %TIME%] starting via run-neo4j.bat
REM `>nul 2>&1` on the start line is not cosmetic: without it the child inherits
REM this script's stdout pipe, and a caller that captures output (agent shell,
REM CI) then blocks until Neo4j exits -- the "listening after 7 s" line arrives
REM but the command never returns. run-neo4j.bat already logs to its own file.
start "neo4j" /min cmd /c "%~dp0run-neo4j.bat" >nul 2>&1

for /L %%i in (1,1,40) do (
    REM Delay: ping, not `timeout /t`. timeout.exe refuses to run when stdin is
    REM redirected ("Input redirection is not supported") -- which is exactly how
    REM an agent/CI shell invokes this, and then the loop burns through all 40
    REM iterations in a second and reports "not listening".
    ping -n 2 127.0.0.1 >nul
    netstat -ano | findstr ":%PORT%" | findstr "LISTENING" >nul 2>&1
    if not errorlevel 1 (
        echo [ensure-neo4j] listening after %%i s.
        exit /b 0
    )
)
echo [ensure-neo4j] not listening after 40s, starting the backend anyway.
echo   The graph stages will say so (GRAPH_OUTLINE_UNAVAILABLE), not fail silently.
>>"%LOG%" echo [%DATE% %TIME%] FAILED to listen within 40s
exit /b 0
