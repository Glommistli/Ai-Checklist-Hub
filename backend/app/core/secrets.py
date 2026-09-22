from __future__ import annotations

import base64
import ctypes
import json
import os
from ctypes import wintypes
from pathlib import Path


APP_NAME = "AI Checklist Hub"
CRYPTPROTECT_UI_FORBIDDEN = 0x01


class DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _data_dir() -> Path:
    configured = os.getenv("AI_CHECKLIST_DATA_DIR")
    if configured:
        path = Path(configured)
    else:
        local_app_data = os.getenv("LOCALAPPDATA")
        root = Path(local_app_data) if local_app_data else Path.home() / "AppData" / "Local"
        path = root / APP_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def _secret_file() -> Path:
    return _data_dir() / "secrets.json"


def _blob(data: bytes) -> tuple[DATA_BLOB, ctypes.Array]:
    buffer = ctypes.create_string_buffer(data)
    return DATA_BLOB(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte))), buffer


def _protect(value: str) -> str:
    if os.name != "nt":
        raise RuntimeError("Secure API key storage requires Windows")
    source, source_buffer = _blob(value.encode("utf-8"))
    encrypted = DATA_BLOB()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    if not crypt32.CryptProtectData(
        ctypes.byref(source), APP_NAME, None, None, None,
        CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(encrypted),
    ):
        raise ctypes.WinError()
    try:
        raw = ctypes.string_at(encrypted.pbData, encrypted.cbData)
        return base64.b64encode(raw).decode("ascii")
    finally:
        kernel32.LocalFree(encrypted.pbData)
        del source_buffer


def _unprotect(value: str) -> str:
    if os.name != "nt":
        raise RuntimeError("Secure API key storage requires Windows")
    source, source_buffer = _blob(base64.b64decode(value))
    decrypted = DATA_BLOB()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    if not crypt32.CryptUnprotectData(
        ctypes.byref(source), None, None, None, None,
        CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(decrypted),
    ):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(decrypted.pbData, decrypted.cbData).decode("utf-8")
    finally:
        kernel32.LocalFree(decrypted.pbData)
        del source_buffer


def _load() -> dict[str, str]:
    path = _secret_file()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(data: dict[str, str]) -> None:
    path = _secret_file()
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def set_secret(name: str, value: str) -> None:
    data = _load()
    data[name] = _protect(value)
    _save(data)


def get_secret(name: str) -> str | None:
    encrypted = _load().get(name)
    if not encrypted:
        return None
    try:
        return _unprotect(encrypted)
    except (OSError, ValueError, ctypes.ArgumentError):
        return None


def delete_secret(name: str) -> None:
    data = _load()
    if name in data:
        del data[name]
        _save(data)


def provider_secret_name(provider_id: int) -> str:
    return f"provider:{provider_id}"

