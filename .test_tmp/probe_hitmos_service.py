import asyncio,json
from musicstream.audio_providers.base import RecordingReference
from musicstream.audio_providers.hitmos import HitmosProvider
from musicstream.audio_providers.hitmos_browser import HitmosBrowserReader
from musicstream.audio_providers.service import SourceSelectionService
async def main():
 p=HitmosProvider(HitmosBrowserReader()); s=SourceSelectionService({'hitmos':p})
 r=await s.discover(RecordingReference(title='Sonne',artist='Rammstein',duration_seconds=272))
 print(json.dumps({'status':r.status,'reason':r.reason,'selection':r.selection,'selected_id':r.selected_id,'hitmos':r.sources[0].status,'hitmos_reason':r.sources[0].reason,'diagnostics':r.diagnostics.model_dump() if r.diagnostics else None}))
asyncio.run(main())
