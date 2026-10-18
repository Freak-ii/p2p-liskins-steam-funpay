"""Windows-специфичное через ctypes, без лишних пакетов: DPAPI для секретов, Job Object, чтобы процессы ботов
не оставались жить после аварийного закрытия центра, и обработчик закрытия консольного окна.
На других системах функции ничего не делают (или выбрасывают OSError для DPAPI).
"""
from __future__ import annotations

import sys

IS_WINDOWS = sys.platform == "win32"

if IS_WINDOWS:
    import ctypes
    from ctypes import wintypes

    class _Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    _crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _crypt32.CryptProtectData.argtypes = [ctypes.POINTER(_Blob), wintypes.LPCWSTR, ctypes.POINTER(_Blob),
                                          ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob)]
    _crypt32.CryptUnprotectData.argtypes = [ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.POINTER(_Blob),
                                            ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob)]
    _kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    _kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    _kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    _kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    _ENTROPY = b"resale-center"
    _CRYPTPROTECT_UI_FORBIDDEN = 0x1

    def _blob(data: bytes) -> _Blob:
        buf = ctypes.create_string_buffer(data, len(data))
        b = _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
        b._buf = buf                      # держим буфер живым, пока жив blob
        return b

    def _call(fn, data: bytes, *, protect: bool) -> bytes:
        out = _Blob()
        entropy = _blob(_ENTROPY)
        args = (ctypes.byref(_blob(data)), "center" if protect else None, ctypes.byref(entropy), None, None,
                _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out))
        if not fn(*args):
            raise OSError(ctypes.get_last_error(), "DPAPI: " + ("шифрование" if protect else "расшифровка") + " не удалось")
        try:
            return ctypes.string_at(out.pbData, out.cbData)
        finally:
            _kernel32.LocalFree(out.pbData)

    def dpapi_protect(data: bytes) -> bytes:
        """Зашифровать для текущего пользователя Windows (как safeStorage в Electron-версии LisBot)."""
        return _call(_crypt32.CryptProtectData, data, protect=True)

    def dpapi_unprotect(data: bytes) -> bytes:
        return _call(_crypt32.CryptUnprotectData, data, protect=False)

    # ---------------------------------------------------------------- Job Object
    class _BasicLimits(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class _IoCounters(ctypes.Structure):
        _fields_ = [(n, ctypes.c_uint64) for n in ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                                                   "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class _ExtLimits(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", _BasicLimits), ("IoInfo", _IoCounters),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
    _JobObjectExtendedLimitInformation = 9
    _PROCESS_SET_QUOTA, _PROCESS_TERMINATE = 0x0100, 0x0001
    _job = None

    def _job_handle():
        global _job
        if _job is None:
            h = _kernel32.CreateJobObjectW(None, None)
            if not h:
                return None
            info = _ExtLimits()
            info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not _kernel32.SetInformationJobObject(h, _JobObjectExtendedLimitInformation, ctypes.byref(info),
                                                     ctypes.sizeof(info)):
                _kernel32.CloseHandle(h)
                return None
            _job = h                       # хэндл не закрываем: его закроет Windows при выходе центра
        return _job

    def bind_child(pid: int) -> bool:
        """Привязать процесс бота к центру: если центр закроют или он упадёт, Windows завершит и бота."""
        job = _job_handle()
        if not job:
            return False
        h = _kernel32.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, pid)
        if not h:
            return False
        try:
            return bool(_kernel32.AssignProcessToJobObject(job, h))
        finally:
            _kernel32.CloseHandle(h)

    # ---------------------------------------------------------------- закрытие консоли
    _HandlerRoutine = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)
    _handlers: list = []

    def on_console_close(callback) -> bool:
        """Вызвать callback(), когда пользователь закрывает окно консоли (крестик, выход из системы).
        Windows ждёт обработчик ~5 секунд, поэтому callback должен лишь запустить мягкую остановку и подождать."""
        def handler(ctrl_type: int) -> bool:
            if ctrl_type in (2, 5, 6):            # CTRL_CLOSE_EVENT, CTRL_LOGOFF_EVENT, CTRL_SHUTDOWN_EVENT
                callback()
                return True
            return False                          # Ctrl+C обрабатывает Python сам
        fn = _HandlerRoutine(handler)
        _handlers.append(fn)
        return bool(_kernel32.SetConsoleCtrlHandler(fn, True))

else:
    def dpapi_protect(data: bytes) -> bytes:
        raise OSError("DPAPI есть только в Windows")

    def dpapi_unprotect(data: bytes) -> bytes:
        raise OSError("DPAPI есть только в Windows")

    def bind_child(pid: int) -> bool:
        return False

    def on_console_close(callback) -> bool:
        return False
