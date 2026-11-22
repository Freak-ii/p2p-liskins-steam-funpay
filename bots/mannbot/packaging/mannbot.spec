# PyInstaller spec: один файл MannBot(.exe) со встроенными шаблонами интерфейса.
# Сборка: pyinstaller packaging/mannbot.spec   (запускать из корня проекта)
import sys
from pathlib import Path

root = Path(SPECPATH).parent
a = Analysis(
    [str(root / "MannBot.py")],
    pathex=[str(root)],
    datas=[(str(root / "mannbot" / "web" / "templates"), "mannbot/web/templates"),
           (str(root / "config.example.yaml"), ".")],
    hiddenimports=["mannbot.demo", "mannbot.funpay.client", "mannbot.steam.client", "lxml.etree", "lxml._elementpath", "anthropic"],
    excludes=["tkinter", "pytest", "PIL", "numpy"],
)
pyz = PYZ(a.pure)
icon = str(root / "packaging" / "mannbot.ico") if (root / "packaging" / "mannbot.ico").exists() else None
exe = EXE(
    pyz, a.scripts, a.binaries, a.datas, [],
    name="MannBot",
    console=True,          # окно консоли = индикатор «программа работает»; закрыть окно = выйти
    icon=icon,
    upx=False,
)
