@echo off
rem ============================================================
rem  Route selfcheck: registry catalog + deterministic baseline
rem    1) agent.route_registry --selfcheck  (catalog sanity)
rem    2) agent.route_cases    --check      (no behavior change)
rem  Usage: double click, or run in CI.
rem ============================================================
cd /d "%~dp0通用工具"
echo ==== 1/2 registry selfcheck ====
python -m agent.route_registry --selfcheck
if errorlevel 1 goto fail
echo.
echo ==== 2/2 route baseline check ====
python -m agent.route_cases --check
if errorlevel 1 goto fail
echo.
echo ALL PASS
pause
exit /b 0

:fail
echo.
echo FAILED - see output above
pause
exit /b 1
