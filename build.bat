@echo off
chcp 65001 >nul
echo ============================================
echo   HARVEST - Сборка EXE
echo ============================================
echo.

:: Проверяем Python
python --version >nul 2>&1
if errorlevel 1 (
    echo [ОШИБКА] Python не найден!
    pause
    exit /b 1
)

:: Проверяем PyInstaller
pip show pyinstaller >nul 2>&1
if errorlevel 1 (
    echo Устанавливаю PyInstaller...
    pip install pyinstaller
)

echo.
echo [1/3] Установка зависимостей...
pip install -r requirements.txt --quiet

echo.
echo [2/3] Сборка harvest.exe...
pyinstaller harvest.spec --noconfirm --clean 2>&1

if errorlevel 1 (
    echo.
    echo [ОШИБКА] Сборка не удалась!
    pause
    exit /b 1
)

echo.
echo [3/3] Проверка результата...
if exist "dist\harvest\harvest.exe" (
    echo Снимаю метку Zone.Identifier (защита от блокировки Device Guard)...
    powershell -NoProfile -Command "Unblock-File -LiteralPath 'dist\harvest\harvest.exe'"
    echo.
    echo ============================================
    echo   ГОТОВО: dist\harvest\harvest.exe
    echo ============================================
    echo.
    echo Запуск:
    echo   dist\harvest\harvest.exe
    echo   dist\harvest\harvest.exe --config config_flop.yaml
    echo   dist\harvest\harvest.exe --config config_arc.yaml
    echo.
    echo Что где:
    echo   dist\harvest\            - копируйте ВСЮ папку целиком
    echo   harvest.exe              - запускаемый файл
    echo   config.yaml              - ВАШ боевой конфиг: положите рядом с exe
    echo   config_*.yaml            - скопируйте нужный из каталога проекта:
    echo                                config_robinhood.yaml (Robinhood 46630)
    echo                                config_flop.yaml     (Flop Labs 99999)
    echo                                config_arc.yaml      (Arc 5042002)
    echo                                (переключение сетей - клавиша S)
    echo   _internal\abi\           - ABI контрактов (уже в бандле)
    echo   _internal\config.example.yaml - шаблон конфига (уже в бандле)
    echo.
    echo ВНИМАНИЕ: в exe бандлятся только abi + config.example.yaml.
    echo Секреты (RPC-ключи, пароли) - через env или внешний config.yaml.
    echo.
    echo Если Windows пишет "заблокировано в параметрах Device Guard":
    echo   ПКМ по harvest.exe - Свойства - Разблокировать (или Unblock-File).
) else (
    echo [ОШИБКА] harvest.exe не найден!
    pause
    exit /b 1
)

pause
