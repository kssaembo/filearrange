"""Windows DPAPI, per-user encrypted secret. Non-Windows: memory only."""
import base64
import ctypes
import os
from ctypes import wintypes


class Blob(ctypes.Structure):
    _fields_ = [('cbData', wintypes.DWORD), ('pbData', ctypes.POINTER(ctypes.c_byte))]


def crypt(data, decrypt=False):
    if os.name != 'nt':
        raise RuntimeError('API 키의 영구 저장은 Windows에서만 지원합니다.')
    buffer = ctypes.create_string_buffer(data)
    incoming = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)))
    outgoing = Blob()
    api = ctypes.WinDLL('crypt32', use_last_error=True)
    fn = api.CryptUnprotectData if decrypt else api.CryptProtectData
    if not fn(ctypes.byref(incoming), None, None, None, None, 1, ctypes.byref(outgoing)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(outgoing.pbData, outgoing.cbData)
    finally:
        free = ctypes.WinDLL('kernel32').LocalFree
        free.argtypes = [ctypes.c_void_p]
        free.restype = ctypes.c_void_p
        free(outgoing.pbData)


def save_key(store, key):
    store.put('api_key_dpapi', base64.b64encode(crypt(key.encode())).decode())


def load_key(store):
    value = store.get('api_key_dpapi', '')
    return crypt(base64.b64decode(value), True).decode() if value else ''
