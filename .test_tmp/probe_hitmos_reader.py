import asyncio, json
from musicstream.audio_providers.hitmos_browser import HitmosBrowserReader
from musicstream.audio_providers.transport import SourceFailure
async def main():
 try:
  result=await HitmosBrowserReader().read_search('Rammstein Sonne')
  print(json.dumps({'result':'ok','status':result.diagnostics.metadata_status,'elapsed_ms':result.diagnostics.elapsed_ms}))
 except SourceFailure as e:
  cause=e.__cause__
  print(json.dumps({'result':e.reason,'stage':e.stage,'cause_type':type(cause).__name__ if cause else None,'elapsed_ms':e.diagnostics.elapsed_ms if e.diagnostics else None}))
asyncio.run(main())
