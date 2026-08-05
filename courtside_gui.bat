@echo off
rem Windows launcher for the Courtside roster editor.
rem Double-click this file, or drag a ROM onto it.
rem
rem It finds a working Python itself, so it does not matter whether "python3"
rem on your machine points at Windows' Microsoft Store placeholder.

cd /d "%~dp0"
set "PYCMD="

call :probe py -3
if defined PYCMD goto run
call :probe python
if defined PYCMD goto run
call :probe python3
if defined PYCMD goto run
goto nopython

:probe
if defined PYCMD goto :eof
%* -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if errorlevel 1 goto :eof
set "PYCMD=%*"
goto :eof

:run
echo Starting the Courtside roster editor with: %PYCMD%
%PYCMD% "%~dp0courtside_gui.py" %*
set "RC=%errorlevel%"
if not "%RC%"=="0" (
    echo.
    echo The editor exited with an error. A copy of the message is in
    echo courtside-error.log next to this file.
    pause
)
exit /b %RC%

:nopython
echo.
echo ============================================================
echo  Python 3.10 or newer was not found on this PC.
echo ============================================================
echo.
echo If you just saw a message about the Microsoft Store, that is
echo Windows' placeholder for a Python that is not installed yet.
echo.
echo Install Python from:
echo.
echo     https://www.python.org/downloads/windows/
echo.
echo IMPORTANT: on the first page of the installer, tick
echo.
echo     [x] Add python.exe to PATH
echo.
echo That version bundles Tkinter, which draws the editor's window.
echo When it has finished, run this file again.
echo.
pause
exit /b 1
