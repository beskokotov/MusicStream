import json, os
os.environ['MUSICSTREAM_DIAGNOSTICS']='true'
from fastapi.testclient import TestClient
from musicstream.main import app
with TestClient(app) as client:
 r=client.get('/api/audio/resolve',params={'title':'Sonne','artist':'Rammstein','duration_seconds':272,'provider':'auto','allow_youtube':'false'})
 data=r.json(); hit=next((x for x in data.get('sources',[]) if x.get('source')=='hitmos'),{})
 print(json.dumps({'http_status':r.status_code,'status':data.get('status'),'reason':data.get('reason'),'hitmos_status':hit.get('status'),'hitmos_reason':hit.get('reason'),'diagnostics':data.get('diagnostics')},ensure_ascii=False))
