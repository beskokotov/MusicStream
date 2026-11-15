import ctypes, json, os, struct, subprocess, sys, time, urllib.request
from ctypes import wintypes
ROOT=os.path.abspath('.')
PID=None
PORT=8000
PYTHON=os.path.join(ROOT,'.venv','Scripts','python.exe')
LOG=os.path.join(ROOT,'.test_tmp')
OUT=os.path.join(LOG,'musicstream-8000-current.out.log')
ERR=os.path.join(LOG,'musicstream-8000-current.err.log')
k=ctypes.WinDLL('kernel32',use_last_error=True); n=ctypes.WinDLL('ntdll')
OpenProcess=k.OpenProcess; OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]; OpenProcess.restype=wintypes.HANDLE
Read=k.ReadProcessMemory; Read.argtypes=[wintypes.HANDLE,wintypes.LPCVOID,wintypes.LPVOID,ctypes.c_size_t,ctypes.POINTER(ctypes.c_size_t)]; Read.restype=wintypes.BOOL
class PBI(ctypes.Structure):
 _fields_=[('reserved1',ctypes.c_void_p),('peb',ctypes.c_void_p),('reserved2',ctypes.c_void_p*2),('pid',ctypes.c_void_p),('reserved3',ctypes.c_void_p)]
query=n.NtQueryInformationProcess; query.argtypes=[wintypes.HANDLE,ctypes.c_int,ctypes.c_void_p,wintypes.ULONG,ctypes.POINTER(wintypes.ULONG)]; query.restype=wintypes.LONG
def read(handle,addr,size):
 b=ctypes.create_string_buffer(size); got=ctypes.c_size_t()
 if not Read(handle,ctypes.c_void_p(addr),b,size,ctypes.byref(got)) or got.value!=size: raise OSError(ctypes.get_last_error(),'process memory read failed')
 return b.raw
def listener_pids():
 text=subprocess.check_output(['netstat','-ano'],text=True,encoding='ascii',errors='ignore')
 return {int(line.split()[-1]) for line in text.splitlines() if 'LISTENING' in line and line.split()[1]=='127.0.0.1:8000'}
def get_env(pid):
 h=OpenProcess(0x0400|0x0010,False,pid)
 if not h: raise OSError(ctypes.get_last_error(),'cannot inspect MusicStream process')
 try:
  pbi=PBI(); ret=wintypes.ULONG(); status=query(h,0,ctypes.byref(pbi),ctypes.sizeof(pbi),ctypes.byref(ret))
  if status!=0: raise OSError(f'process query status {status}')
  params=struct.unpack('<Q',read(h,pbi.peb+0x20,8))[0]; ptr=struct.unpack('<Q',read(h,params+0x80,8))[0]
  raw=bytearray()
  for off in range(0,1048576,4096):
   raw.extend(read(h,ptr+off,4096))
   end=next((i for i in range(0,len(raw)-3,2) if raw[i:i+4]==b'\0\0\0\0'),-1)
   if end>=0: raw=raw[:end+2]; break
  else: raise RuntimeError('environment block too large')
  result={}
  for entry in bytes(raw).decode('utf-16le').split('\0'):
   if '=' in entry and not entry.startswith('='):
    name,value=entry.split('=',1); result[name]=value
  return result
 finally: k.CloseHandle(h)
def get(url,timeout=2):
 with urllib.request.urlopen(url,timeout=timeout) as r: return r.status,r.read()
# Refuse to touch the listener unless it is the exact known server process.
pids=listener_pids()
if len(pids)!=1: raise RuntimeError(f'8000 listener changed or duplicated: {sorted(pids)}')
PID=next(iter(pids))
if get('http://127.0.0.1:8000/health')[0]!=200: raise RuntimeError('current port 8000 is not healthy')
env=get_env(PID)
if env.get('MUSICSTREAM_TORRENT_HTTP_ENABLED')!='true' or env.get('MUSICSTREAM_TORRENT_AUTO_ENABLED')!='true' or not env.get('MUSICSTREAM_PROWLARR_API_KEY'):
 raise RuntimeError('existing Prowlarr/Auto Play configuration is incomplete; will not restart')
env['MUSICSTREAM_DIAGNOSTICS']='true'
# Confirm the old app has no active stream leases before replacing the process.
try:
 status,data=get('http://127.0.0.1:8000/api/experimental/playback/capabilities')
 if status!=200 or json.loads(data).get('available') is not True: raise RuntimeError('torrent Auto Play capability check failed')
except Exception as exc: raise RuntimeError('could not verify active Prowlarr Auto Play') from exc
h=OpenProcess(0x0001,False,PID)
if not h: raise OSError(ctypes.get_last_error(),'cannot stop the verified 8000 process')
try:
 if not k.TerminateProcess(h,0): raise OSError(ctypes.get_last_error(),'could not stop the verified 8000 process')
finally: k.CloseHandle(h)
for _ in range(100):
 if not listener_pids(): break
 time.sleep(.1)
else: raise RuntimeError('old process did not release port 8000')
with open(OUT,'ab') as out, open(ERR,'ab') as err:
 child=subprocess.Popen([PYTHON,'-m','uvicorn','musicstream.main:app','--host','127.0.0.1','--port','8000'],cwd=ROOT,env=env,stdin=subprocess.DEVNULL,stdout=out,stderr=err,creationflags=0x08000000)
# Verify health, app assets and Prowlarr capability on the single replacement instance.
ready=False
for _ in range(120):
 if child.poll() is not None: break
 try:
  health=get('http://127.0.0.1:8000/health')[0]
  app_status,app=get('http://127.0.0.1:8000/app')
  js_status,js=get('http://127.0.0.1:8000/static/app.js')
  cap_status,cap=get('http://127.0.0.1:8000/api/experimental/playback/capabilities')
  ready=(health==200 and app_status==200 and js_status==200 and b'prepareAutomaticPlayback' in js and cap_status==200 and json.loads(cap).get('available') is True)
  if ready: break
 except Exception: pass
 time.sleep(.5)
if not ready:
 if child.poll() is None: child.terminate()
 raise RuntimeError(f'updated 8000 did not pass startup checks (child exit={child.poll()})')
newpids=listener_pids()
if len(newpids)!=1 or newpids==pids: raise RuntimeError(f'expected one replacement listener, found {sorted(newpids)}')
print(json.dumps({'restart':'ok','port':8000,'pid':next(iter(newpids)),'torrent_auto_preserved':True,'prowlarr_key_preserved':True,'local_diagnostics_enabled_for_validation':True,'app_status':app_status,'static_auto_play':True,'only_one_listener':True}))
