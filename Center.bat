@echo off
chcp 65001 >nul
cd /d "%~dp0"
where python >nul 2>&1 || (echo Нужен Python 3.11+ с python.org ^(галочка "Add python.exe to PATH"^). & pause & exit /b 1)
where node >nul 2>&1 || echo Внимание: Node.js не найден, LisBot не запустится ^(https://nodejs.org^).
python -c "import aiohttp, yaml, jinja2, bs4, lxml" 2>nul || python -m pip install -r requirements.txt
if exist bots\lisbot\package.json if not exist bots\lisbot\node_modules (pushd bots\lisbot & call npm ci --omit=dev & popd)
python -m center %*
pause
