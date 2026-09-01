@echo off
REM ============================================================================
REM  Установка выгрузчика Confluence в закрытом контуре (без интернета).
REM  Требуется установленный Python 3.12 (x64). Интернет НЕ нужен -
REM  все зависимости берутся из каталога wheels\.
REM ============================================================================
setlocal
cd /d "%~dp0"

echo [1/3] Поиск Python 3.12...
set "PY="
py -3.12 --version >nul 2>&1 && set "PY=py -3.12"
if not defined PY (
    python --version 2>&1 | findstr /c:"3.12" >nul && set "PY=python"
)
if not defined PY (
    echo.
    echo ОШИБКА: не найден Python 3.12. Установите Python 3.12 x64 и повторите.
    echo Проверьте: "py -3.12 --version"  или  "python --version"
    exit /b 1
)
echo     Использую: %PY%

echo [2/3] Создание виртуального окружения .venv...
%PY% -m venv .venv
if errorlevel 1 (
    echo ОШИБКА: не удалось создать виртуальное окружение.
    exit /b 1
)

echo [3/3] Установка зависимостей из wheels\ (offline)...
".venv\Scripts\python.exe" -m pip install --no-index --find-links wheels -r requirements.txt
if errorlevel 1 (
    echo ОШИБКА: не удалось установить зависимости из wheels\.
    exit /b 1
)

echo.
echo Готово. Дальше:
echo   1) Скопируйте .env.example в .env и заполните доступ к Confluence:
echo        copy .env.example .env
echo   2) Запустите выгрузку дерева:
echo        run-tree.bat ^<page_id^> ^<service_code^> ^<subdir^> [source] [--http] [--all] [--with-images]
echo.
endlocal
