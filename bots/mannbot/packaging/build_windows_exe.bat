@echo off
rem Сборка одного файла MannBot.exe на Windows (нужен установленный Python 3.11+ с python.org).
rem Запускать из корня проекта:  packaging\build_windows_exe.bat
chcp 65001 >nul
python -m venv .venv-build || goto :err
call .venv-build\Scripts\activate.bat
python -m pip install --upgrade pip
pip install aiohttp beautifulsoup4 lxml PyYAML Jinja2 cryptography pyinstaller || goto :err
pyinstaller --noconfirm --clean --distpath dist\windows --workpath build\windows packaging\mannbot.spec || goto :err
echo.
echo Готово: dist\windows\MannBot.exe
pause
exit /b 0
:err
echo Сборка не удалась
pause
exit /b 1
