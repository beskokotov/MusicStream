import asyncio, json, socket, ipaddress, time
from urllib.parse import urlsplit
from musicstream.audio_providers.hitmos import HitmosProvider, CDN_HOSTS
from musicstream.audio_providers.hitmos_browser import HitmosBrowserReader
from musicstream.audio_providers.base import RecordingReference
from musicstream.audio_providers.service import SourceSelectionService

TRACKS = [
    RecordingReference(title='Sonne', artist='Rammstein', duration_seconds=272),
    RecordingReference(title='Mutter', artist='Rammstein', duration_seconds=268),
    RecordingReference(title='Группа крови', artist='Кино', duration_seconds=218),
]
async def main():
    provider=HitmosProvider(HitmosBrowserReader())
    service=SourceSelectionService({'hitmos': provider})
    for ref in TRACKS:
        t=time.monotonic(); found=await service.discover(ref)
        row={'track':ref.title, 'search_s':round(time.monotonic()-t,2),'search_status':found.status,'reason':found.reason,'selected_id':found.selected_id,'match':found.candidates[0].match.status if found.candidates else None}
        if found.selected_id and found.selected_source=='hitmos':
            t=time.monotonic(); result=await service.prepare(ref,'hitmos',found.selected_id)
            c=result.candidates[0] if result.candidates else None
            row.update(resolve_s=round(time.monotonic()-t,2),stream_status=result.status,stream_reason=result.reason,stream_method=c.method if c else None)
            if c and c.stream_url:
                host=urlsplit(c.stream_url).hostname
                row['stream_host_allowlisted']=host in CDN_HOSTS
                try:
                    infos=await asyncio.to_thread(socket.getaddrinfo,host,None,0,socket.SOCK_STREAM)
                    ips=sorted({item[4][0] for item in infos})
                    row['dns_public']=bool(ips) and all(ipaddress.ip_address(ip).is_global for ip in ips)
                    row['dns_ip_count']=len(ips)
                except Exception:
                    row['dns_public']=False
        print(json.dumps(row,ensure_ascii=False))
asyncio.run(main())
