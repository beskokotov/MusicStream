import ctypes, json, os, struct, sys
import subprocess
from ctypes import wintypes
PID = 17760
kernel = ctypes.WinDLL('kernel32', use_last_error=True)
ntdll = ctypes.WinDLL('ntdll')
PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010
OpenProcess = kernel.OpenProcess
OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
OpenProcess.restype = wintypes.HANDLE
ReadProcessMemory = kernel.ReadProcessMemory
ReadProcessMemory.argtypes = [wintypes.HANDLE, wintypes.LPCVOID, wintypes.LPVOID, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
ReadProcessMemory.restype = wintypes.BOOL
class PBI(ctypes.Structure):
    _fields_ = [('reserved1', ctypes.c_void_p), ('peb', ctypes.c_void_p),
                ('reserved2', ctypes.c_void_p * 2), ('pid', ctypes.c_void_p), ('reserved3', ctypes.c_void_p)]
query = ntdll.NtQueryInformationProcess
query.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.ULONG, ctypes.POINTER(wintypes.ULONG)]
query.restype = wintypes.LONG
handle = OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, PID)
if not handle: raise OSError(ctypes.get_last_error(), 'OpenProcess failed')
def read(addr, size):
    b=ctypes.create_string_buffer(size); n=ctypes.c_size_t()
    if not ReadProcessMemory(handle, ctypes.c_void_p(addr), b, size, ctypes.byref(n)) or n.value != size:
        raise OSError(ctypes.get_last_error(), 'ReadProcessMemory failed')
    return b.raw
try:
    pbi=PBI(); ret=wintypes.ULONG()
    status=query(handle,0,ctypes.byref(pbi),ctypes.sizeof(pbi),ctypes.byref(ret))
    if status != 0 or not pbi.peb: raise OSError(f'NtQueryInformationProcess status {status}')
    params=struct.unpack('<Q',read(pbi.peb+0x20,8))[0]
    envptr=struct.unpack('<Q',read(params+0x80,8))[0]
    raw=bytearray()
    for offset in range(0, 1_048_576, 4096):
        block=read(envptr+offset,4096); raw.extend(block)
        end=next((i for i in range(0, len(raw)-3, 2) if raw[i:i+4] == b'\0\0\0\0'), -1)
        if end >= 0:
            raw=raw[:end+2]; break
    else: raise RuntimeError('Environment block exceeds the safe inspection limit')
    text=bytes(raw).decode('utf-16le',errors='strict')
    target_env={}
    for entry in text.split('\0'):
        if '=' in entry and not entry.startswith('='):
            k,v=entry.split('=',1); target_env[k]=v
    if not target_env: raise RuntimeError('Could not read target environment')
    names=['MUSICSTREAM_TORRENT_HTTP_ENABLED','MUSICSTREAM_TORRENT_AUTO_ENABLED','MUSICSTREAM_TORRENT_NETWORK_MODE','MUSICSTREAM_TORRENT_PUBLIC_APPROVED_INFO_HASHES','MUSICSTREAM_TORRENT_APPROVED_SOURCE_IDS','MUSICSTREAM_TORRENT_INDEXER_PRIORITY','MUSICSTREAM_PROWLARR_API_KEY']
    safe={k: bool(target_env.get(k)) for k in names}
    safe['torrent_http_enabled']=target_env.get('MUSICSTREAM_TORRENT_HTTP_ENABLED') == 'true'
    safe['torrent_auto_enabled']=target_env.get('MUSICSTREAM_TORRENT_AUTO_ENABLED') == 'true'
    safe['diagnostics_enabled']=target_env.get('MUSICSTREAM_DIAGNOSTICS') == 'true'
    safe['browser_override_names']=[key for key in target_env if key.startswith('PLAYWRIGHT_') or key in ('CHROME_BIN','CHROME_PATH')]
    safe['environment_variables_preserved']=len(target_env)
    run=subprocess.run([os.path.join(os.path.abspath('.'),'.venv','Scripts','python.exe'),'.test_tmp/probe_hitmos_reader.py'],cwd=os.path.abspath('.'),env=target_env,capture_output=True,text=True,timeout=25,creationflags=0x08000000)
    try: probe=json.loads(run.stdout.strip())
    except Exception: probe={'result':'probe_failed','exit_code':run.returncode}
    safe['same_environment_probe']=probe
    print(json.dumps(safe))
finally:
    kernel.CloseHandle(handle)

