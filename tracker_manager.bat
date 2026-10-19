@echo off
setlocal enabledelayedexpansion

REM ============================================================
REM  Tracker Management Utility
REM  View, search, or reset the progress tracker
REM ============================================================

set "TRACKER=E:\ROC_Files\progress_tracker.txt"
set "SOURCE=E:\ROC_Files\SiteFolder"

:MENU
cls
echo ============================================================
echo  Progress Tracker Management
echo ============================================================
echo.
echo  Tracker file: %TRACKER%
echo.
if not exist "%TRACKER%" (
    echo  [No tracker file found - run progressive_7zip_tracker.bat first]
    echo.
    echo  Press any key to exit...
    pause >nul
    exit /b 0
)

echo  [1] Show tracker statistics
echo  [2] View full tracker contents
echo  [3] Search for a specific folder/file in tracker
echo  [4] Show recently processed items (last 50)
echo  [5] Show failed items
echo  [6] Reset tracker (DANGEROUS - will reprocess everything)
echo  [7] Exit
echo.
set /p CHOICE="Select an option (1-7): "

if "%CHOICE%"=="1" goto STATS
if "%CHOICE%"=="2" goto VIEW_ALL
if "%CHOICE%"=="3" goto SEARCH
if "%CHOICE%"=="4" goto RECENT
if "%CHOICE%"=="5" goto FAILED
if "%CHOICE%"=="6" goto RESET
if "%CHOICE%"=="7" exit /b 0
goto MENU

:STATS
cls
echo ============================================================
echo  Tracker Statistics
echo ============================================================
echo.

set /a TOTAL=0
set /a FOLDERS=0
set /a FILES=0
set /a SUCCESS=0
set /a RECOVERED=0
set /a FAILED=0
set /a DELETE_FAILED=0

for /f "skip=4 tokens=1,2,4 delims=|" %%A in ('type "%TRACKER%"') do (
    set /a TOTAL+=1
    if "%%A"=="FOLDER" set /a FOLDERS+=1
    if "%%A"=="FILE" set /a FILES+=1
    if "%%C"=="SUCCESS" set /a SUCCESS+=1
    if "%%C"=="RECOVERED" set /a RECOVERED+=1
    if "%%C"=="DELETE_FAILED" set /a DELETE_FAILED+=1
    if "%%C"=="ALREADY_DELETED" set /a RECOVERED+=1
)

echo  Total tracked items:     %TOTAL%
echo  Folders:                 %FOLDERS%
echo  Files:                   %FILES%
echo.
echo  Successful:              %SUCCESS%
echo  Recovered:               %RECOVERED%
echo  Delete failed:           %DELETE_FAILED%
echo.
echo  Tracker file size:       %~zTRACKER% bytes
echo.
pause
goto MENU

:VIEW_ALL
cls
echo ============================================================
echo  Full Tracker Contents
echo ============================================================
echo.
type "%TRACKER%"
echo.
echo ============================================================
pause
goto MENU

:SEARCH
cls
echo ============================================================
echo  Search Tracker
echo ============================================================
echo.
set /p SEARCH_TERM="Enter folder/file name to search: "
echo.
echo Search results for: %SEARCH_TERM%
echo ------------------------------------------------------------
findstr /i "%SEARCH_TERM%" "%TRACKER%"
echo ------------------------------------------------------------
echo.
pause
goto MENU

:RECENT
cls
echo ============================================================
echo  Recently Processed Items (Last 50)
echo ============================================================
echo.

REM Count total lines
set /a LINE_COUNT=0
for /f %%A in ('type "%TRACKER%"') do set /a LINE_COUNT+=1

set /a SKIP_LINES=LINE_COUNT-50
if %SKIP_LINES% lss 5 set /a SKIP_LINES=5

more +%SKIP_LINES% "%TRACKER%"
echo.
pause
goto MENU

:FAILED
cls
echo ============================================================
echo  Failed Items
echo ============================================================
echo.
echo Items with DELETE_FAILED status:
echo ------------------------------------------------------------
findstr "DELETE_FAILED" "%TRACKER%"
echo ------------------------------------------------------------
echo.
pause
goto MENU

:RESET
cls
echo ============================================================
echo  WARNING: Reset Tracker
echo ============================================================
echo.
echo This will DELETE the tracker file and cause all items
echo to be reprocessed on the next run.
echo.
echo THIS ACTION CANNOT BE UNDONE!
echo.
set /p CONFIRM="Are you absolutely sure? Type YES to confirm: "
if /i not "%CONFIRM%"=="YES" (
    echo.
    echo Reset cancelled.
    pause
    goto MENU
)

echo.
echo Creating backup of tracker...
copy "%TRACKER%" "%TRACKER%.backup.%date:/=-%.%time::=-%" >nul

echo Deleting tracker...
del /f /q "%TRACKER%"

echo.
echo Tracker has been reset. A backup was created.
echo.
pause
goto MENU
