@echo off
REM ==========================================================
REM  CatsSoft Downloader - suita de teste
REM  Ruleaza toate testele pe baza de date izolata.
REM ==========================================================
REM  Testele ruleaza in ambele forme ale aplicatiei:
REM    1. pachetul  (catssoft\)  - sursa de adevar
REM    2. fisierul unic          - artefactul de build
REM  Cele doua trebuie sa se comporte identic; daca difera,
REM  una dintre ele a fost modificata fara sa se regenereze.
REM ==========================================================
setlocal
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"

REM Folosim Portable Python daca exista: este interpreterul cu care ruleaza
REM aplicatia, iar unele erori Tk sunt specifice versiunii de Python.
set "PYEXE=python"
if exist "C:\Users\Andruska\Downloads\Portable Python\App\Python\python.exe" (
  set "PYEXE=C:\Users\Andruska\Downloads\Portable Python\App\Python\python.exe"
)
echo.
echo  ==========================================================
echo   CatsSoft Downloader v6.13 - teste
echo   Interpreter: %PYEXE%
echo  ==========================================================
echo.

set "RC=0"

echo  --- forma 1: pachetul (sursa de adevar) ---
set "CATSSoft_FORM=package"
"%PYEXE%" -m unittest discover -s tests -p "test_*.py" -v
if not "%ERRORLEVEL%"=="0" set "RC=%ERRORLEVEL%"
echo.

echo  --- forma 2: fisierul unic (artefact de build) ---
set "CATSSoft_FORM=single"
"%PYEXE%" -m unittest discover -s tests -p "test_*.py" -v
if not "%ERRORLEVEL%"=="0" set "RC=%ERRORLEVEL%"
set "CATSSoft_FORM="
echo.

REM Controlul negativ: reconstruiește comportamentul pre-fix si cere ca
REM testele de regresie sa pica. ruleaza doar daca forma de pachet exista.
if exist "catssoft\__init__.py" (
  echo  --- control negativ ---
  "%PYEXE%" tests\nnegative_control.py
  if not "%ERRORLEVEL%"=="0" set "RC=%ERRORLEVEL%"
  echo.
)

echo.
if "%RC%"=="0" (
  echo  REZULTAT: TOATE TESTELE AU TRECUT
) else (
  echo  REZULTAT: EXISTA TESTE ESUATE - cod %RC%
)

echo.
if "%NO_PAUSE%"=="" pause
exit /b %RC%
