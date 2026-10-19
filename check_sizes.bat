@echo off
setlocal enabledelayedexpansion

REM ============================================================
REM  Folder Size Survey - Run this FIRST before compressing
REM  Shows which folders are largest so you can plan accordingly.
REM ============================================================

set "SOURCE=E:\ROC_Files\SiteFolder"
set "REPORT=E:\ROC_Files\folder_sizes.txt"

echo Scanning folder sizes in %SOURCE%...
echo This may take a while with 20k folders...
echo.

echo Folder Size Report - %date% %time% > "%REPORT%"
echo Source: %SOURCE% >> "%REPORT%"
echo ============================================= >> "%REPORT%"

set /a COUNT=0
set /a OVER_500MB=0
set /a OVER_1GB=0

for /d %%F in ("%SOURCE%\*") do (
    set "FOLDER=%%~nxF"
    set "PATH=%%F"
    
    REM Get folder size using dir
    set "SIZE=0"
    for /f "tokens=3" %%S in ('dir /s /a "%%F" 2^>nul ^| findstr /c:"File(s)"') do (
        set "SIZE=%%S"
    )
    
    REM Remove commas from size
    set "SIZE=!SIZE:,=!"
    
    REM Check thresholds (in bytes)
    if !SIZE! gtr 524288000 set /a OVER_500MB+=1
    if !SIZE! gtr 1073741824 set /a OVER_1GB+=1
    
    echo !SIZE!    !FOLDER! >> "%REPORT%"
    
    set /a COUNT+=1
    set /a _MOD=COUNT %% 500
    if !_MOD! equ 0 echo ... scanned !COUNT! folders ...
)

echo. >> "%REPORT%"
echo ============================================= >> "%REPORT%"
echo Total folders scanned: %COUNT% >> "%REPORT%"
echo Folders over 500MB: %OVER_500MB% >> "%REPORT%"
echo Folders over 1GB:   %OVER_1GB% >> "%REPORT%"

echo.
echo Done! Results saved to: %REPORT%
echo.
echo WARNING: You have ~1GB free space.
if %OVER_1GB% gtr 0 (
    echo *** %OVER_1GB% folders are LARGER than 1GB! ***
    echo These will FAIL to compress in-place. Consider:
    echo   1. Use OUTPUT on a different drive with more space
    echo   2. Set COMPRESSION=0 (store mode) to minimize temp space
    echo   3. Manually handle these large folders first
) else (
    echo All folders appear to be under 1GB - you should be fine.
)
echo.
pause
