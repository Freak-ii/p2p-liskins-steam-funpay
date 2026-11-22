"""Зашифрованное хранилище секретов центра: center-data/secrets.dat.

Ключи LIS-SKINS и Steam, golden_key FunPay, токены Telegram больше не лежат открытым текстом.
* Windows: DPAPI текущего пользователя (как safeStorage в Electron-версии LisBot) — файл расшифрует только
  этот пользователь на этом компьютере, ключ хранить не нужно.
* Linux / Docker / macOS: Fernet (AES-128-CBC + HMAC). Ключ берётся из переменной CENTER_SECRET_KEY
  (любая строка-пароль), иначе создаётся файл center-data/secret.key с правами 600.

Значения адресуются строками вида "lisbot.LIS_API_KEY" или "mannbot.funpay.golden_key".
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets as _secrets
from pathlib import Path

from center import winutil

log = logging.getLogger("center.secrets")


class SecretStoreError(RuntimeError):
    pass


class _Fernet:
    def __init__(self, key: bytes):
        from cryptography.fernet import Fernet
        self.f = Fernet(key)

    def protect(self, data: bytes) -> bytes:
        return self.f.encrypt(data)

    def unprotect(self, data: bytes) -> bytes:
        from cryptography.fernet import InvalidToken
        try:
            return self.f.decrypt(data)
        except InvalidToken as e:
            raise SecretStoreError("secrets.dat не расшифровывается этим ключом (CENTER_SECRET_KEY или secret.key "
                                   "изменился)") from e


class _Dpapi:
    def protect(self, data: bytes) -> bytes:
        return winutil.dpapi_protect(data)

    def unprotect(self, data: bytes) -> bytes:
        try:
            return winutil.dpapi_unprotect(data)
        except OSError as e:
            raise SecretStoreError("secrets.dat зашифрован другим пользователем Windows или на другом компьютере: "
                                   "введите ключи заново") from e


def _fernet_key(home: Path) -> bytes:
    env = os.environ.get("CENTER_SECRET_KEY", "")
    if env:
        return base64.urlsafe_b64encode(hashlib.sha256(env.encode("utf-8")).digest())
    path = home / "secret.key"
    if path.exists():
        return path.read_bytes().strip()
    key = base64.urlsafe_b64encode(_secrets.token_bytes(32))
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(key)
    os.replace(tmp, path)
    return key


class SecretStore:
    def __init__(self, home: Path, *, backend: str | None = None):
        self.home = Path(home)
        self.path = self.home / "secrets.dat"
        self.backend = backend or ("dpapi" if winutil.IS_WINDOWS else "fernet")
        self._cipher = None
        self._data: dict[str, str] | None = None
        self.error = ""

    def _c(self):
        if self._cipher is None:
            self._cipher = _Dpapi() if self.backend == "dpapi" else _Fernet(_fernet_key(self.home))
        return self._cipher

    def _load(self) -> dict[str, str]:
        if self._data is not None:
            return self._data
        self._data = {}
        if self.path.exists():
            try:
                wrapper = json.loads(self.path.read_text(encoding="utf-8"))
                if wrapper.get("backend") != self.backend:
                    raise SecretStoreError(f"secrets.dat создан другим способом шифрования ({wrapper.get('backend')})")
                raw = self._c().unprotect(base64.b64decode(wrapper["data"]))
                self._data = json.loads(raw.decode("utf-8"))
                self.error = ""
            except (SecretStoreError, ValueError, KeyError) as e:
                self.error = str(e)
                log.error("Секреты центра не прочитаны: %s", e)
                raise SecretStoreError(self.error) from e
        return self._data

    def _save(self) -> None:
        data = json.dumps(self._data or {}, ensure_ascii=False).encode("utf-8")
        wrapper = {"v": 1, "backend": self.backend, "data": base64.b64encode(self._c().protect(data)).decode("ascii")}
        self.home.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(wrapper), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, self.path)

    # ------------------------------------------------------------------ API
    def get(self, key: str, default: str = "") -> str:
        return self._load().get(key, default)

    def prefix(self, prefix: str) -> dict[str, str]:
        """Все значения с префиксом "lisbot." → {"LIS_API_KEY": ...}."""
        p = prefix.rstrip(".") + "."
        return {k[len(p):]: v for k, v in self._load().items() if k.startswith(p)}

    def update(self, values: dict[str, str | None]) -> None:
        """Записать несколько значений разом; None удаляет ключ."""
        data = self._load()
        changed = False
        for k, v in values.items():
            if v is None:
                changed |= data.pop(k, None) is not None
            elif data.get(k) != v:
                data[k] = v
                changed = True
        if changed:
            self._save()

    def set(self, key: str, value: str | None) -> None:
        self.update({key: value})
