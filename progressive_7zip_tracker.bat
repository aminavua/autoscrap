@echo off
setlocal enabledelayedexpansion

REM ============================================================
REM  Progressive 7-Zip Archiver with Deletion + TRACKER
REM  Compresses each subfolder/file into individual .7z archives
REM  and deletes originals one-by-one to progressively free space.
REM  Archives are saved IN-PLACE (same folder as source).
REM  Maintains a tracker file to skip already-processed items.
REM ============================================================

REM ---------- CONFIGURATION ----------
set "SOURCE=E:\ROC_Files\SiteFolder"
set "OUTPUT=C:\Users\%USERNAME%\OneDrive\SiteFolder_Backup"
set "SEVENZIP=C:\Program Files\7-Zip\7z.exe"
set "COMPRESSION=0"
set "LOGFILE=E:\ROC_Files\compress_log.txt"
set "TRACKER=E:\ROC_Files\progress_tracker.txt"
REM ---------- END CONFIGURATION ----------

echo.
echo ============================================================
echo  Progressive 7-Zip Archiver (with Tracker)
echo  Source:  %SOURCE%
echo  Output:  %OUTPUT% (OneDrive)
echo  Compression: STORE (Level 0 - fastest for PDFs)
echo  Tracker: %TRACKER%
echo ============================================================
echo.

if not exist "%SEVENZIP%" (
    echo [ERROR] 7-Zip not found at: %SEVENZIP%
    echo         Please install 7-Zip or update the SEVENZIP variable.
    pause
    exit /b 1
)

if not exist "%SOURCE%" (
    echo [ERROR] Source directory not found: %SOURCE%
    pause
    exit /b 1
)

if not exist "%OUTPUT%" mkdir "%OUTPUT%"

if not exist "%TRACKER%" (
    echo # Progressive 7-Zip Tracker > "%TRACKER%"
    echo # Created: %date% %time% >> "%TRACKER%"
    echo # Format: TYPE^|NAME^|TIMESTAMP^|STATUS >> "%TRACKER%"
    echo # ============================================ >> "%TRACKER%"
)

echo [%date% %time%] === Compression session started === >> "%LOGFILE%"

set /a TOTAL_FOLDERS=0
set /a DONE_FOLDERS=0
set /a SKIP_FOLDERS=0
set /a FAIL_FOLDERS=0
set /a TOTAL_FILES=0
set /a DONE_FILES=0
set /a SKIP_FILES=0
set /a FAIL_FILES=0

echo Loading tracker database...
set /a TRACKER_COUNT=0
for /f "skip=4 usebackq" %%A in ("%TRACKER%") do set /a TRACKER_COUNT+=1
echo Tracker loaded: %TRACKER_COUNT% existing entries.
echo.

echo [PHASE 1] Compressing subfolders...
echo.

for /d %%F in ("%SOURCE%\*") do set /a TOTAL_FOLDERS+=1
echo Found %TOTAL_FOLDERS% subfolders to process.
echo.

for /d %%F in ("%SOURCE%\*") do (
    set "FOLDER_NAME=%%~nxF"
    set "FOLDER_PATH=%%F"
    set "ARCHIVE=%OUTPUT%\!FOLDER_NAME!.7z"
    
    set "IS_TRACKED=0"
    findstr /c:"FOLDER|!FOLDER_NAME!|" "%TRACKER%" >nul 2>&1 && set "IS_TRACKED=1"
    
    if "!IS_TRACKED!"=="1" (
        echo [SKIP] !FOLDER_NAME! - already in tracker
        set /a SKIP_FOLDERS+=1
    ) else if exist "!ARCHIVE!" (
        echo [RECOVER] !FOLDER_NAME! - archive exists, verifying...
        
        "%SEVENZIP%" t "!ARCHIVE!" >nul 2>&1
        if !errorlevel! equ 0 (
            if exist "!FOLDER_PATH!" (
                rmdir /s /q "!FOLDER_PATH!"
                if !errorlevel! equ 0 (
                    echo   [OK] Archive verified and source deleted
                    echo FOLDER^|!FOLDER_NAME!^|%date% %time%^|RECOVERED >> "%TRACKER%"
                    set /a DONE_FOLDERS+=1
                    echo [%date% %time%] [RECOVERED] !FOLDER_NAME! >> "%LOGFILE%"
                ) else (
                    echo   [WARN] Archive verified but deletion failed
                    echo FOLDER^|!FOLDER_NAME!^|%date% %time%^|DELETE_FAILED >> "%TRACKER%"
                    set /a FAIL_FOLDERS+=1
                    echo [%date% %time%] [DELETE_FAIL] !FOLDER_NAME! >> "%LOGFILE%"
                )
            ) else (
                echo   [OK] Archive verified, source already deleted
                echo FOLDER^|!FOLDER_NAME!^|%date% %time%^|ALREADY_DELETED >> "%TRACKER%"
                set /a SKIP_FOLDERS+=1
            )
        ) else (
            echo   [FAIL] Archive verification failed - NOT deleting source
            set /a FAIL_FOLDERS+=1
            echo [%date% %time%] [VERIFY_FAIL] !FOLDER_NAME! >> "%LOGFILE%"
        )
    ) else (
        echo [COMPRESSING] !FOLDER_NAME! ...
        
        "%SEVENZIP%" a -t7z -mx=%COMPRESSION% -mmt=on "!ARCHIVE!" "!FOLDER_PATH!\*" >nul 2>&1
        
        if !errorlevel! equ 0 (
            if exist "!ARCHIVE!" (
                "%SEVENZIP%" t "!ARCHIVE!" >nul 2>&1
                if !errorlevel! equ 0 (
                    echo   [OK] Archive created and verified. Deleting original folder...
                    rmdir /s /q "!FOLDER_PATH!"
                    
                    if !errorlevel! equ 0 (
                        echo   [DELETED] !FOLDER_NAME!
                        echo FOLDER^|!FOLDER_NAME!^|%date% %time%^|SUCCESS >> "%TRACKER%"
                        set /a DONE_FOLDERS+=1
                        echo [%date% %time%] [OK] !FOLDER_NAME! >> "%LOGFILE%"
                    ) else (
                        echo   [WARN] Archive verified but deletion failed for !FOLDER_NAME!
                        echo FOLDER^|!FOLDER_NAME!^|%date% %time%^|DELETE_FAILED >> "%TRACKER%"
                        set /a FAIL_FOLDERS+=1
                        echo [%date% %time%] [DELETE_FAIL] !FOLDER_NAME! >> "%LOGFILE%"
                    )
                ) else (
                    echo   [FAIL] Archive created but verification failed - keeping source
                    set /a FAIL_FOLDERS+=1
                    echo [%date% %time%] [VERIFY_FAIL] !FOLDER_NAME! >> "%LOGFILE%"
                    del /f /q "!ARCHIVE!" >nul 2>&1
                )
            ) else (
                echo   [FAIL] Archive not created for !FOLDER_NAME!
                set /a FAIL_FOLDERS+=1
                echo [%date% %time%] [FAIL] !FOLDER_NAME! >> "%LOGFILE%"
            )
        ) else (
            echo   [FAIL] 7-Zip error for !FOLDER_NAME!
            set /a FAIL_FOLDERS+=1
            echo [%date% %time%] [FAIL] !FOLDER_NAME! >> "%LOGFILE%"
            if exist "!ARCHIVE!" del /f /q "!ARCHIVE!" >nul 2>&1
        )
    )
    
    set /a _COUNT=DONE_FOLDERS+SKIP_FOLDERS+FAIL_FOLDERS
    set /a _MOD=_COUNT %% 100
    if !_MOD! equ 0 (
        if !_COUNT! gtr 0 (
            echo --- Progress: !_COUNT!/%TOTAL_FOLDERS% folders processed ---
        )
    )
)

echo.
echo [PHASE 1 COMPLETE] Folders - Done: %DONE_FOLDERS%, Skipped: %SKIP_FOLDERS%, Failed: %FAIL_FOLDERS%
echo.

echo [PHASE 2] Compressing loose files in root...
echo.

for %%F in ("%SOURCE%\*.*") do (
    if /i not "%%~xF"==".7z" (
        if /i not "%%~nxF"=="desktop.ini" (
            set /a TOTAL_FILES+=1
        )
    )
)
echo Found %TOTAL_FILES% loose files to process.
echo.

for %%F in ("%SOURCE%\*.*") do (
    if /i not "%%~xF"==".7z" (
        if /i not "%%~nxF"=="desktop.ini" (
            set "FILE_NAME=%%~nxF"
            set "FILE_PATH=%%F"
            set "ARCHIVE=%OUTPUT%\!FILE_NAME!.7z"
            
            set "IS_TRACKED=0"
            findstr /c:"FILE|!FILE_NAME!|" "%TRACKER%" >nul 2>&1 && set "IS_TRACKED=1"
            
            if "!IS_TRACKED!"=="1" (
                echo [SKIP] !FILE_NAME! - already in tracker
                set /a SKIP_FILES+=1
            ) else if exist "!ARCHIVE!" (
                echo [RECOVER] !FILE_NAME! - archive exists, verifying...
                
                "%SEVENZIP%" t "!ARCHIVE!" >nul 2>&1
                if !errorlevel! equ 0 (
                    if exist "!FILE_PATH!" (
                        del /f /q "!FILE_PATH!"
                        if !errorlevel! equ 0 (
                            echo   [OK] Archive verified and source deleted
                            echo FILE^|!FILE_NAME!^|%date% %time%^|RECOVERED >> "%TRACKER%"
                            set /a DONE_FILES+=1
                            echo [%date% %time%] [RECOVERED] !FILE_NAME! >> "%LOGFILE%"
                        ) else (
                            echo   [WARN] Archive verified but deletion failed
                            echo FILE^|!FILE_NAME!^|%date% %time%^|DELETE_FAILED >> "%TRACKER%"
                            set /a FAIL_FILES+=1
                            echo [%date% %time%] [DELETE_FAIL] !FILE_NAME! >> "%LOGFILE%"
                        )
                    ) else (
                        echo   [OK] Archive verified, source already deleted
                        echo FILE^|!FILE_NAME!^|%date% %time%^|ALREADY_DELETED >> "%TRACKER%"
                        set /a SKIP_FILES+=1
                    )
                ) else (
                    echo   [FAIL] Archive verification failed - NOT deleting source
                    set /a FAIL_FILES+=1
                    echo [%date% %time%] [VERIFY_FAIL] !FILE_NAME! >> "%LOGFILE%"
                )
            ) else (
                echo [COMPRESSING] !FILE_NAME! ...
                
                "%SEVENZIP%" a -t7z -mx=%COMPRESSION% -mmt=on "!ARCHIVE!" "!FILE_PATH!" >nul 2>&1
                
                if !errorlevel! equ 0 (
                    if exist "!ARCHIVE!" (
                        "%SEVENZIP%" t "!ARCHIVE!" >nul 2>&1
                        if !errorlevel! equ 0 (
                            echo   [OK] Archive created and verified. Deleting original file...
                            del /f /q "!FILE_PATH!"
                            
                            if !errorlevel! equ 0 (
                                echo   [DELETED] !FILE_NAME!
                                echo FILE^|!FILE_NAME!^|%date% %time%^|SUCCESS >> "%TRACKER%"
                                set /a DONE_FILES+=1
                                echo [%date% %time%] [OK] !FILE_NAME! >> "%LOGFILE%"
                            ) else (
                                echo   [WARN] Archive verified but deletion failed for !FILE_NAME!
                                echo FILE^|!FILE_NAME!^|%date% %time%^|DELETE_FAILED >> "%TRACKER%"
                                set /a FAIL_FILES+=1
                                echo [%date% %time%] [DELETE_FAIL] !FILE_NAME! >> "%LOGFILE%"
                            )
                        ) else (
                            echo   [FAIL] Archive created but verification failed - keeping source
                            set /a FAIL_FILES+=1
                            echo [%date% %time%] [VERIFY_FAIL] !FILE_NAME! >> "%LOGFILE%"
                            del /f /q "!ARCHIVE!" >nul 2>&1
                        )
                    ) else (
                        echo   [FAIL] Archive not created for !FILE_NAME!
                        set /a FAIL_FILES+=1
                        echo [%date% %time%] [FAIL] !FILE_NAME! >> "%LOGFILE%"
                    )
                ) else (
                    echo   [FAIL] 7-Zip error for !FILE_NAME!
                    set /a FAIL_FILES+=1
                    echo [%date% %time%] [FAIL] !FILE_NAME! >> "%LOGFILE%"
                    if exist "!ARCHIVE!" del /f /q "!ARCHIVE!" >nul 2>&1
                )
            )
        )
    )
)

echo.
echo [PHASE 2 COMPLETE] Files - Done: %DONE_FILES%, Skipped: %SKIP_FILES%, Failed: %FAIL_FILES%

echo.
echo ============================================================
echo  SUMMARY
echo ============================================================
echo  Folders compressed:  %DONE_FOLDERS% / %TOTAL_FOLDERS%
echo  Folders skipped:     %SKIP_FOLDERS%
echo  Folders failed:      %FAIL_FOLDERS%
echo  Files compressed:    %DONE_FILES% / %TOTAL_FILES%
echo  Files skipped:       %SKIP_FILES%
echo  Files failed:        %FAIL_FILES%
echo.
echo  Tracker: %TRACKER%
echo  Log:     %LOGFILE%
echo ============================================================
echo.

echo [%date% %time%] === Compression session finished === >> "%LOGFILE%"
echo Done! Press any key to exit.
pause >nul
