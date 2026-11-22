@echo off
REM ---------------------------------------------------------------------------
REM Local Neo4j launcher (console mode). Plain ASCII on purpose: cmd.exe here
REM reads .bat in code page 936, and UTF-8 comments get split into bytes that cmd
REM then tries to run as commands.
REM
REM Why not `neo4j.bat start`: that goes through the Windows service. The service
REM is not installed on this machine ("The service 'Neo4j' is not installed") and
REM installing it needs admin rights. Console mode needs none.
REM
REM Why this script instead of a one-liner: JAVA_HOME is its only prerequisite
REM (Neo4j 5.26 wants JDK 17/21; this box has C:\tools\jdk-21.0.12.1+1) and an
REM agent shell does not inherit user-level environment variables, so every hand
REM typed start has to remember to set it first. Baking it in leaves one command.
REM
REM Logs: backend in .visioncheck\backend.log, this one in .visioncheck\neo4j.log.
REM
REM This script does NOT survive a reboot by itself (no service): run it again, or
REM just run run-backend.bat / ensure-neo4j.bat which call it. See docs/NEO4J-LOCAL.md.
REM ---------------------------------------------------------------------------
set "JAVA_HOME=C:\tools\jdk-21.0.12.1+1"
set "NEO4J_HOME=D:\neo4j\neo4j-community-5.26.0"
set "LOG_DIR=%~dp0.visioncheck"

if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"
echo [%DATE% %TIME%] starting Neo4j console >> "%LOG_DIR%\neo4j.log"
call "%NEO4J_HOME%\bin\neo4j.bat" console >> "%LOG_DIR%\neo4j.log" 2>&1
echo [%DATE% %TIME%] Neo4j console exited with %ERRORLEVEL% >> "%LOG_DIR%\neo4j.log"
