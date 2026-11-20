"""Visible, temporary-context VK Audio search reader (metadata only)."""

from __future__ import annotations

import asyncio
import http.client
import ipaddress
import json
import re
import socket
import ssl
import time
from collections import deque
from dataclasses import dataclass, field
from urllib.parse import parse_qs, quote, unquote, urljoin, urlsplit
from xml.etree import ElementTree

from musicstream.audio_providers.base import AudioProvider, DiscoveredRecording, DiscoveryBatch, RecordingReference, SourceResult
from musicstream.audio_providers.selection import rank_batch
from musicstream.audio_providers.matching import assess
from musicstream.audio_providers.vk import (VKAudioSearchResult, clean_recordings,
    VKAudioRecording, match_recordings, recording_id_from_url)


_DIAGNOSTICS_TTL_SECONDS = 300
_DIAGNOSTICS_MAX_MEDIA = 120
_MEDIA_SUFFIXES = (".mp3", ".m4a", ".aac", ".ogg", ".opus", ".flac", ".wav", ".m3u8", ".mpd", ".mp4")
_SAFE_MIME = re.compile(r"^[a-z0-9.+-]+/[a-z0-9.+-]+$")
_SAFE_HOST = re.compile(r"^[a-z0-9.-]{1,253}$")
_OWNER_AUDIO_ID = re.compile(r"^-?\d+_-?\d+$")


def _direct_vk_page_id(value: str) -> str | None:
    try:
        parsed = urlsplit(value)
        return recording_id_from_url(parsed._replace(query="", fragment="").geturl())
    except (TypeError, ValueError):
        return None


class VKReaderOperationFailure(RuntimeError):
    """Safe failure classification; raw Playwright messages may contain URLs or paths."""

    def __init__(self, stage: str, code: str, error_type: str):
        self.stage = stage
        self.code = code
        self.error_type = error_type if re.fullmatch(r"[A-Za-z0-9_]{1,64}", error_type) else "UnknownError"
        super().__init__(code)


def _reader_operation_failure(stage: str, exc: Exception) -> VKReaderOperationFailure:
    error_type = type(exc).__name__
    text = str(exc).casefold()
    if isinstance(exc, ImportError):
        code = "playwright_unavailable"
    elif "executable doesn't exist" in text or "executable does not exist" in text:
        code = "chrome_executable_missing"
    elif "failed to launch" in text or "process singleton" in text:
        code = "chrome_launch_failed"
    elif "access is denied" in text or "permission denied" in text:
        code = "chrome_permission_denied"
    elif "target page, context or browser has been closed" in text or error_type == "TargetClosedError":
        code = "chrome_closed"
    elif "timeout" in text or error_type == "TimeoutError":
        code = f"{stage}_timeout"
    elif stage == "browser_launch":
        code = "chrome_launch_failed"
    elif stage == "navigation":
        code = "vk_navigation_failed"
    elif stage == "search":
        code = "vk_search_failed"
    else:
        code = f"vk_{stage}_failed"
    return VKReaderOperationFailure(stage, code, error_type)


def _visible_search_recordings(payload, *, limit: int = 300) -> list[dict]:
    """Distill only explicit recording metadata from first-party UI JSON responses."""
    stack = [(payload, 0)]
    seen_nodes = 0
    rows: list[dict] = []
    seen_ids: set[str] = set()
    while stack and seen_nodes < 30000 and len(rows) < limit:
        value, depth = stack.pop()
        seen_nodes += 1
        if depth > 12:
            continue
        if isinstance(value, dict):
            owner = value.get("owner_id", value.get("ownerId"))
            audio_id = value.get("id", value.get("audio_id", value.get("audioId")))
            title = value.get("title", value.get("name"))
            if (isinstance(owner, (int, str)) and re.fullmatch(r"-?\d+", str(owner))
                    and isinstance(audio_id, (int, str)) and re.fullmatch(r"-?\d+", str(audio_id))
                    and isinstance(title, str) and title.strip()):
                identity = f"{int(owner)}_{int(audio_id)}"
                artist_value = value.get("artist", value.get("performer", value.get("performers", value.get("main_artists"))))
                if isinstance(artist_value, dict):
                    artist_value = artist_value.get("name", artist_value.get("title", ""))
                elif isinstance(artist_value, list):
                    artist_value = ", ".join(str(item.get("name", item.get("title", ""))) if isinstance(item, dict) else str(item)
                                              for item in artist_value if isinstance(item, (dict, str)))
                duration = value.get("duration", value.get("duration_seconds"))
                duration_text = str(int(duration)) if isinstance(duration, (int, float)) and not isinstance(duration, bool) and 0 <= duration <= 86400 else ""
                if identity not in seen_ids:
                    seen_ids.add(identity)
                    rows.append({"full_id": identity, "title": title, "artist": artist_value or "",
                                 "duration": duration_text, "text": "", "label": "", "class_name": ""})
            stack.extend((child, depth + 1) for child in reversed(list(value.values())) if isinstance(child, (dict, list)))
        elif isinstance(value, list):
            stack.extend((child, depth + 1) for child in reversed(value) if isinstance(child, (dict, list)))
    return rows


def _search_recording_visible_in_rows(recording: dict, rows: list[dict]) -> bool:
    """Require a distilled network record to match one rendered VK track card."""
    title = " ".join(str(recording.get("title") or "").casefold().split())
    artist = " ".join(str(recording.get("artist") or "").casefold().split())
    duration = recording.get("duration")
    seconds = int(duration) if isinstance(duration, str) and duration.isdigit() else None
    duration_pattern = (re.compile(rf"(?<!\d){seconds // 60}:({seconds % 60:02d})(?!\d)")
                        if seconds is not None and 0 < seconds <= 86400 else None)
    if not title:
        return False
    for row in rows:
        text = " ".join(str(row.get(key) or "") for key in ("text", "title", "artist", "duration")).casefold()
        normalized = " ".join(text.split())
        if title not in normalized or (artist and artist not in normalized):
            continue
        if duration_pattern and not duration_pattern.search(normalized):
            continue
        return True
    return False


def _cors_probe_sync(url: str, origin: str) -> tuple[bool, str]:
    """Issue a credential-free, DNS-pinned manifest GET with MusicStream Origin."""
    current = url
    for _ in range(4):
        try:
            parsed = urlsplit(current)
            host = (parsed.hostname or "").rstrip(".").lower()
            if (parsed.scheme != "https" or not host or parsed.port not in (None, 443)
                    or parsed.username or parsed.password or parsed.fragment or not _SAFE_HOST.fullmatch(host)):
                return False, "manifest_url_invalid"
            records = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
            addresses = list(dict.fromkeys(record[4][0] for record in records))
            if not addresses or len(addresses) > 16 or any(not ipaddress.ip_address(value).is_global for value in addresses):
                return False, "manifest_host_not_public"
            connection = _PinnedVKHTTPSConnection(host, addresses[0], timeout=6)
            path = parsed.path or "/"
            if parsed.query:
                path += "?" + parsed.query
            try:
                connection.request("GET", path, headers={"Origin": origin, "Accept": "application/dash+xml,*/*;q=0.8",
                    "Accept-Encoding": "identity", "User-Agent": "MusicStream/0.1.0", "Connection": "close"})
                response = connection.getresponse()
                status = response.status
                allow_origin = (response.getheader("Access-Control-Allow-Origin") or "").strip().lower()
                allow_credentials = (response.getheader("Access-Control-Allow-Credentials") or "").strip().lower() == "true"
                location = response.getheader("Location")
                if status in {301, 302, 303, 307, 308}:
                    if not location:
                        return False, "cors_probe_redirect_missing_location"
                    current = urljoin(current, location)
                    continue
                if status not in {200, 206}:
                    return False, f"manifest_http_{status}"
                if not (allow_origin == "*" or allow_origin == origin.lower()) or (allow_origin == "*" and allow_credentials):
                    return False, "cors_origin_denied" if allow_origin else "cors_header_missing"
                response.read(2048)
                return True, "cors_allowed"
            finally:
                connection.close()
        except (OSError, ValueError, ssl.SSLError, http.client.HTTPException):
            return False, "cors_probe_failed"
    return False, "cors_probe_redirect_limit"


class _PinnedVKHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, address: str, *, timeout: float):
        super().__init__(host, port=443, timeout=timeout, context=ssl.create_default_context())
        self._pinned_address = address

    def connect(self):
        raw = socket.create_connection((self._pinned_address, 443), self.timeout)
        try:
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except Exception:
            raw.close()
            raise


@dataclass(frozen=True)
class VKResourceDescriptor:
    """Process-memory-only descriptor for a resource proven to name one VK track."""

    vk_audio_id: str
    media_type: str
    codec: str | None
    manifest_url: str = field(repr=False)
    manifest_xml: str = field(repr=False, default="")
    media_url: str | None = field(repr=False, default=None)
    segment_origins: tuple[str, ...] = ()
    detected_at: float = 0.0
    expires_at: float = 0.0
    access_restricted: bool = False
    cors_test_origin: str | None = None
    musicstream_cors_allowed: bool = False

    def summary(self) -> dict:
        return {"vk_audio_id": self.vk_audio_id, "media_type": self.media_type,
                "codec": self.codec, "segment_origins": list(self.segment_origins),
                "detected_at": self.detected_at, "expires_at": self.expires_at,
                "access_restricted": self.access_restricted,
                "cors_test_origin": self.cors_test_origin,
                "musicstream_cors_allowed": self.musicstream_cors_allowed}


_INSTALL_RESOURCE_CAPTURE = r"""(targetId) => {
  const idOf = row => {
    const values = [row, ...row.querySelectorAll('[data-full-id],[data-audio],[data-audio-id]')];
    for (const el of values) {
      const full = el.getAttribute('data-full-id') || el.getAttribute('data-id');
      if (full && /^-?\d+_-?\d+$/.test(full.trim())) return full.trim();
      const audio = el.getAttribute('data-audio');
      if (audio) {
        try { const v = JSON.parse(audio); if (Array.isArray(v) && /^-?\d+$/.test(String(v[0])) && /^-?\d+$/.test(String(v[1]))) return `${String(v[0])}_${String(v[1])}`; }
        catch {}
      }
    }
    return '';
  };
  if (window.__msVkResourceCapture?.targetId === targetId) return true;
  const state = window.__msVkResourceCapture = {targetId, actionCount: 0, pendingSequence: null};
  const emit = data => { if (typeof window.__msVkResourceAction === 'function') window.__msVkResourceAction(JSON.stringify(data)); };
  const attach = root => {
    if (root.matches?.('audio,video') && !root.__msVkResourceAttached) {
      root.__msVkResourceAttached = true;
      root.addEventListener('playing', () => {
        if (state.pendingSequence === null) return;
        const rows = [...document.querySelectorAll('.audio_row,[data-full-id],[data-audio]')];
        const active = rows.find(row => idOf(row) === targetId &&
          (/playing|is_playing/i.test(String(row.className||'')) ||
           [...row.querySelectorAll('[aria-label],[title]')].some(el => /pause|пауза/i.test(`${el.getAttribute('aria-label')||''} ${el.getAttribute('title')||''}`))));
        if (active) emit({audioId: targetId, action: 'playing', sequence: state.pendingSequence});
      });
    }
    root.querySelectorAll?.('audio,video').forEach(attach);
  };
  attach(document);
  new MutationObserver(records => records.forEach(record => record.addedNodes.forEach(node => {
    if (node.nodeType === Node.ELEMENT_NODE) attach(node);
  }))).observe(document.documentElement, {childList: true, subtree: true});
  document.addEventListener('click', event => {
    const node = event.target instanceof Element ? event.target : null;
    const control = node?.closest('button,[role="button"],[aria-label],[title]');
    if (!control) return;
    const label = `${control.getAttribute('aria-label')||''} ${control.getAttribute('title')||''} ${control.className||''}`.toLowerCase();
    if (!/(play|воспроизвед|audio_row__play)/.test(label)) return;
    let row = control;
    for (let i=0; i<8 && row; i++, row=row.parentElement) {
      if (idOf(row) === targetId) {
        const eventData = {audioId: targetId, action: 'play', sequence: ++state.actionCount};
        state.pendingSequence = eventData.sequence;
        state.lastAction = eventData;
        emit(eventData);
        break;
      }
    }
  }, true);
  return true;
}"""


_INSTALL_MEDIA_DIAGNOSTICS = r"""() => {
  const state = window.__musicstreamVkMediaDiagnostics = {
    encrypted_media_events: 0, waiting_for_key_events: 0, key_systems_requested: []
  };
  const record = event => {
    if (event.type === 'encrypted') state.encrypted_media_events++;
    if (event.type === 'waitingforkey') state.waiting_for_key_events++;
  };
  const attach = root => {
    if (root.matches?.('audio,video') && !root.__msVkDiagAttached) {
      root.__msVkDiagAttached = true;
      root.addEventListener('encrypted', record);
      root.addEventListener('waitingforkey', record);
    }
    root.querySelectorAll?.('audio,video').forEach(attach);
  };
  attach(document);
  new MutationObserver(records => records.forEach(record => record.addedNodes.forEach(node => {
    if (node.nodeType === Node.ELEMENT_NODE) attach(node);
  }))).observe(document.documentElement, {childList: true, subtree: true});
  const proto = window.Navigator?.prototype;
  const original = proto?.requestMediaKeySystemAccess;
  if (original && !original.__msVkDiagWrapped) {
    const wrapped = function(keySystem, ...args) {
      if (typeof keySystem === 'string' && !state.key_systems_requested.includes(keySystem))
        state.key_systems_requested.push(keySystem.slice(0, 100));
      return original.call(this, keySystem, ...args);
    };
    wrapped.__msVkDiagWrapped = true;
    proto.requestMediaKeySystemAccess = wrapped;
  }
  return true;
}"""


_INSTALL_RESOURCE_OBSERVER = r"""(config) => {
  const targetId=typeof config==='string'?config:config?.targetId;
  const expectedTitle=String(config?.title||'').normalize('NFKC').toLocaleLowerCase().replace(/\s+/g,' ').trim();
  const expectedArtist=String(config?.artist||'').normalize('NFKC').toLocaleLowerCase().replace(/\s+/g,' ').trim();
  const seconds=Number(config?.duration_seconds);
  const expectedDuration=Number.isFinite(seconds)&&seconds>0?`${Math.floor(seconds/60)}:${String(seconds%60).padStart(2,'0')}`:'';
  const normalize = value => {
    if (typeof value !== 'string') return '';
    const text = value.trim();
    if (/^-?\d+_-?\d+$/.test(text)) return text;
    try {
      const data = JSON.parse(text);
      if (Array.isArray(data) && /^-?\d+$/.test(String(data[0])) && /^-?\d+$/.test(String(data[1]))) return `${data[0]}_${data[1]}`;
      if (data && typeof data === 'object') {
        const owner = data.owner_id ?? data.ownerId;
        const audio = data.id ?? data.audio_id ?? data.audioId;
        if (/^-?\d+$/.test(String(owner)) && /^-?\d+$/.test(String(audio))) return `${owner}_${audio}`;
      }
    } catch {}
    return '';
  };
  const idOf = root => {
    if (!(root instanceof Element)) return '';
    const nodes = [root, ...root.querySelectorAll('[data-full-id],[data-audio],[data-audio-id],[data-owner-id],[data-owner_id],a[href*="audio"]')];
    for (const node of nodes) {
      for (const attr of ['data-full-id','data-id','data-audio','data-audio-id']) {
        const id = normalize(node.getAttribute(attr));
        if (id) return id;
      }
      const owner = node.getAttribute('data-owner-id') || node.getAttribute('data-owner_id');
      const audio = node.getAttribute('data-audio-id') || node.getAttribute('data-id');
      if (/^-?\d+$/.test(owner||'') && /^-?\d+$/.test(audio||'')) return `${owner}_${audio}`;
    }
    return '';
  };
  let state = window.__msVkResourceCapture;
  if (!state) state = window.__msVkResourceCapture = {
    targetId, actionCount: 0, pendingSequence: null, clickDetected: false,
    playingDetected: false, activeAudioId: null, observerAttached: false
  };
  const emit = data => { if (typeof window.__msVkResourceAction === 'function') window.__msVkResourceAction(JSON.stringify(data)); };
  const activeAudioId = () => {
    const docs = [document];
    try { if (window.top !== window && window.top.document !== document) docs.push(window.top.document); } catch {}
    for (const doc of docs) {
      const rows = [...doc.querySelectorAll('.audio_row,[data-full-id],[data-audio],[data-audio-id]')];
      for (const row of rows) {
        const activeClass = /playing|is_playing|current|is_current/i.test(String(row.className||''));
        const activeControl = [...row.querySelectorAll('[aria-label],[title]')].some(node =>
          /pause/i.test(`${node.getAttribute('aria-label')||''} ${node.getAttribute('title')||''}`));
        if (activeClass || activeControl) { const id = idOf(row); if (id) return id; }
      }
      const player = doc.querySelector('.audio_page_player,[class*=audio_player],[data-testid*=player],[data-qa*=player]');
      if (player) { const id = idOf(player); if (id) return id; }
    }
    return null;
  };
  state.targetId = targetId;
  state.expectedTitle=expectedTitle||state.expectedTitle||'';
  state.expectedArtist=expectedArtist||state.expectedArtist||'';
  state.expectedDuration=expectedDuration||state.expectedDuration||'';
  if (state.observerAttached) {
    state.clickDetected = false;
    state.playingDetected = false;
    state.pendingSequence = null;
    state.lastClickAudioId = null;
    state.lastClickSequence = null;
    state.activeAudioId = activeAudioId();
    state.observedPageKind = /\/audio(?:-?\d+_-?\d+)?(?:\/|$)/i.test(location.pathname) ? 'vk_audio' : 'other';
    return true;
  }
  state.activeAudioId = activeAudioId();
  state.observerAttached = true;
  state.observedPageKind = /\/audio(?:-?\d+_-?\d+)?(?:\/|$)/i.test(location.pathname) ? 'vk_audio' : 'other';
  document.addEventListener('click', event => {
    const path = typeof event.composedPath === 'function' ? event.composedPath() : [event.target];
    const elements = path.filter(node => node instanceof Element);
    const control = elements.find(node => node.matches('button,[role="button"],[aria-label],[title],[class*=play],[class*=pause]'));
    const norm=value=>String(value||'').normalize('NFKC').toLocaleLowerCase().replace(/\s+/g,' ').trim();
    const matchingRows=[];
    if(state.expectedTitle&&state.expectedArtist) for(let node=event.target,index=0;node&&index<12;node=node.parentElement,index++) {
      if(!(node instanceof Element)) continue;
      const text=norm(node.innerText||node.textContent);
      if(text.length>=state.expectedTitle.length+state.expectedArtist.length&&text.length<=600
          &&text.includes(state.expectedTitle)&&text.includes(state.expectedArtist)
          &&(!state.expectedDuration||new RegExp(`(?:^|\\s)${state.expectedDuration.replace(':','\\:')}(?:$|\\s)`).test(text)))
        matchingRows.push({node,length:text.length});
    }
    const matchingRow=matchingRows.sort((a,b)=>a.length-b.length)[0]?.node;
    if (!control && !matchingRow) return;
    const pathHints = elements.map(node => `${node.className||''} ${node.getAttribute('aria-label')||''} ${node.getAttribute('title')||''}`).join(' ').toLowerCase();
    const label = `${pathHints} ${control.getAttribute('aria-label')||''} ${control.getAttribute('title')||''} ${control.className||''}`.toLowerCase();
    const playerArea = /audio_page_player|audio_player|player|audio_row/.test(pathHints);
    const playLike = /play|audio_row__play|audio_page_player__play/.test(label);
    if (!playLike && !playerArea && !matchingRow) return;
    let id = '';
    for (let row = matchingRow || control, index = 0; row && index < 12; row = row.parentElement, index++) { id = idOf(row); if (id) break; }
    if (!id && matchingRow) id = targetId;
    id = id || activeAudioId();
    state.clickDetected = true;
    state.activeAudioId = id || null;
    state.clickKind = playLike ? 'play_like' : 'player_control';
    state.clickControlHint = String(control.className||'').split(/\s+/).filter(v=>/play|pause|player|audio/i.test(v)).slice(0,4).join(' ').slice(0,100);
    const sequence = ++state.actionCount;
    state.lastClickAudioId = id || null;
    state.lastClickSequence = sequence;
    if (id === state.targetId) state.pendingSequence = sequence;
    emit({audioId: id || null, targetId: state.targetId, action: 'click', sequence});
  }, true);
  const attach = root => {
    if (root.matches?.('audio,video') && !root.__msVkResourceAttached) {
      root.__msVkResourceAttached = true;
      root.addEventListener('playing', () => {
        const id = activeAudioId();
        if (id) state.activeAudioId = id;
        if (id === state.targetId) {
          state.playingDetected = true;
          emit({audioId: id, targetId: state.targetId, action: 'playing', sequence: state.pendingSequence || state.actionCount});
        }
      });
    }
    root.querySelectorAll?.('audio,video').forEach(attach);
  };
  attach(document);
  if (state.activeAudioId === state.targetId && [...document.querySelectorAll('audio,video')].some(media => !media.paused && !media.ended)) {
    state.playingDetected = true;
    emit({audioId: state.targetId, targetId: state.targetId, action: 'playing', sequence: state.pendingSequence || state.actionCount});
  }
  new MutationObserver(records => records.forEach(record => record.addedNodes.forEach(node => {
    if (node.nodeType === Node.ELEMENT_NODE) attach(node);
  }))).observe(document.documentElement, {childList: true, subtree: true});
  return true;
}"""


_RESOURCE_CAPTURE_SNAPSHOT = r"""() => {
  const state = window.__msVkResourceCapture;
  return {observer_attached: !!state?.observerAttached,
    observed_page_kind: state?.observedPageKind || (/\/audio(?:-?\d+_-?\d+)?(?:\/|$)/i.test(location.pathname) ? 'vk_audio' : 'other'),
    click_detected: !!state?.clickDetected, active_audio_id: state?.activeAudioId || null,
    click_audio_id: state?.lastClickAudioId || null, click_sequence: state?.lastClickSequence || null,
    playing_detected: !!state?.playingDetected};
}"""


_MEDIA_SESSION_SNAPSHOT = r"""() => {
  const metadata=navigator.mediaSession?.metadata;
  return {playback_state:navigator.mediaSession?.playbackState||'none',
    title:typeof metadata?.title==='string'?metadata.title.slice(0,200):'',
    artist:typeof metadata?.artist==='string'?metadata.artist.slice(0,200):''};
}"""


_VK_PLAYER_MODEL_SNAPSHOT = r"""() => {
  const roots=[];
  for(const name of ['AudioPlayer','audioPlayer','audio_player','AudioPlaylist']) {
    try { const value=window[name]; if(value&&(typeof value==='object'||typeof value==='function')) roots.push(value); } catch {}
  }
  try { if(typeof window.getAudioPlayer==='function') { const value=window.getAudioPlayer(); if(value&&typeof value==='object') roots.push(value); } } catch {}
  const ids=new Set(), seen=new Set(); let playing=false, visited=0;
  const integer=value=> /^-?\d+$/.test(String(value??''));
  const walk=(value,depth=0)=>{
    if(!value||(typeof value!=='object'&&typeof value!=='function')||seen.has(value)||depth>5||visited>1200)return;
    seen.add(value); visited++;
    try {
      const owner=value.owner_id??value.ownerId??value.owner;
      const audio=value.audio_id??value.audioId??value.current_audio_id??value.currentAudioId;
      if(integer(owner)&&integer(audio)) ids.add(`${String(owner)}_${String(audio)}`);
      for(const key of Object.keys(value).slice(0,100)) {
        let child; try { child=value[key]; } catch { continue; }
        if(/^(?:owner_audio|audio_full_id|current_track_id|current_audio)$/i.test(key)
            && typeof child==='string'&&/^-?\d+_-?\d+$/.test(child)) ids.add(child);
        if(/playing|is_playing|isplaying/i.test(key)&&child===true) playing=true;
        if(/playback.?state|player.?state|status/i.test(key)&&typeof child==='string'
            && /^(playing|play|active)$/i.test(child.trim())) playing=true;
        if(child&&typeof child==='object') walk(child,depth+1);
      }
    } catch {}
  };
  roots.forEach(root=>walk(root));
  return {model_available:roots.length>0, active_audio_ids:[...ids].slice(0,20), playing};
}"""


_FIND_EXACT_PLAY_CONTROL = r"""(targetId) => {
  const norm = el => {
    for (const node of [el, ...el.querySelectorAll('[data-full-id],[data-audio],[data-audio-id],a[href]')]) {
      const full = node.getAttribute('data-full-id') || node.getAttribute('data-id');
      if (full && /^-?\d+_-?\d+$/.test(full.trim())) return full.trim();
      for (const raw of [node.getAttribute('data-audio'), node.getAttribute('data-audio-id')]) {
        if (!raw) continue;
        try {
          const v=JSON.parse(raw);
          const owner=Array.isArray(v)?v[0]:v?.owner_id??v?.ownerId;
          const audio=Array.isArray(v)?v[1]:v?.id??v?.audio_id??v?.audioId;
          if(/^-?\d+$/.test(String(owner))&&/^-?\d+$/.test(String(audio))) return `${String(owner)}_${String(audio)}`;
        } catch {}
      }
      const owner=node.getAttribute('data-owner-id')||node.getAttribute('data-owner_id');
      const audio=node.getAttribute('data-audio-id')||node.getAttribute('data-id');
      if(/^-?\d+$/.test(owner||'')&&/^-?\d+$/.test(audio||'')) return `${owner}_${audio}`;
      if(node.tagName==='A') {
        let source=node.pathname||'';
        try { const u=new URL(node.href,location.href); source += ' '+[...u.searchParams.values()].join(' '); } catch {}
        const match=source.match(/(?:^|[^a-z0-9])audio(-?\d+)_(-?\d+)(?:$|[^a-z0-9])/i);
        if(match) return `${match[1]}_${match[2]}`;
      }
    }
    return '';
  };
  const visible = el => { const r=el.getBoundingClientRect(),s=getComputedStyle(el); return r.width>0&&r.height>0&&s.display!=='none'&&s.visibility!=='hidden'; };
  const rows = [...document.querySelectorAll('.audio_row,[data-full-id],[data-audio],[data-audio-id],a[href*="audio"]')]
    .map(el => { if(el.tagName!=='A') return el; let row=el; for(let i=0;i<6&&row.parentElement;i++) {
      const c=String(row.className||'').toLowerCase();
      if(/audio|track|row/.test(c)||row.getAttribute('role')==='listitem') return row;
      row=row.parentElement;
    } return row; });
  const row = rows.find(el => norm(el) === targetId);
  if (!row) return {found:false, reason:'vk_track_row_not_found'};
  const controls = [...row.querySelectorAll('button,[role="button"],[aria-label],[title],[class*=play],[class*=pause]')]
    .filter(visible).filter(el => /play|pause/i.test(`${el.className||''} ${el.getAttribute('aria-label')||''} ${el.getAttribute('title')||''}`));
  const control = controls.find(el => /play/i.test(`${el.className||''} ${el.getAttribute('aria-label')||''} ${el.getAttribute('title')||''}`)) || null;
  if (!control) return {found:false, reason:'vk_track_play_control_not_found'};
  const r=control.getBoundingClientRect();
  const rowText=(row.innerText||'').replace(/\s+/g,' ').trim();
  return {found:true, x:r.left+r.width/2, y:r.top+r.height/2, row_text:rowText.slice(0,240), control_hint:String(control.className||'').slice(0,80)};
}"""


_FIND_DIRECT_PAGE_PLAY_CONTROL = r"""(target) => {
  const match=location.pathname.match(/^\/audio(-?\d+_-?\d+)\/?$/i);
  if(!match||match[1]!==target.targetId) return {found:false,reason:'vk_direct_page_identity_not_preserved'};
  const norm=value=>String(value||'').normalize('NFKC').toLocaleLowerCase().replace(/\s+/g,' ').trim();
  const visible=el=>{const r=el.getBoundingClientRect(),s=getComputedStyle(el);return r.width>0&&r.height>0&&s.display!=='none'&&s.visibility!=='hidden';};
  const title=norm(target.title),artist=norm(target.artist);
  const main=document.querySelector('main,[role="main"]')||document.body;
  const headings=[...main.querySelectorAll('h1,h2,h3')].filter(el=>visible(el)&&norm(el.innerText||el.textContent)===title);
  const controls=[];
  for(const heading of headings){
    for(let box=heading,index=0;box&&index<8&&box!==main;box=box.parentElement,index++){
      const boxText=norm(box.innerText||box.textContent);
      if(!boxText.includes(title)||!boxText.includes(artist)||boxText.length>900) continue;
      const candidates=[...box.querySelectorAll('button,[role="button"],[aria-label],[title]')].filter(visible);
      for(const control of candidates){
        const labels=[control.innerText||control.textContent||'',control.getAttribute('aria-label')||'',control.getAttribute('title')||''].map(norm);
        const action=labels.some(value=>/pause|\u043f\u0430\u0443\u0437|\u043f\u0440\u0438\u043e\u0441\u0442\u0430\u043d\u043e\u0432\u0438\u0442\u044c/.test(value))?'pause'
          :labels.some(value=>/play|\u0432\u043e\u0441\u043f\u0440\u043e\u0438\u0437\u0432\u0435\u0441\u0442\u0438|\u043f\u0440\u043e\u0441\u043b\u0443\u0448\u0430\u0442\u044c/.test(value))?'play':'';
        if(!action) continue;
        if(control.closest('.TopNavigation__player,[class*=TopNavigation],[data-testid*=top-player],[data-qa*=top-player]')) continue;
        const r=control.getBoundingClientRect();
        controls.push({action,x:r.left+r.width/2,y:r.top+r.height/2,area:r.width*r.height,box_length:boxText.length});
      }
      if(controls.length) break;
    }
    if(controls.length) break;
  }
  const unique=[];
  for(const item of controls) if(!unique.some(other=>Math.abs(item.x-other.x)<2&&Math.abs(item.y-other.y)<2)) unique.push(item);
  if(unique.length!==1) return {found:false,reason:unique.length?'vk_direct_play_control_ambiguous':'vk_direct_play_control_not_found'};
  return {found:true,action:unique[0].action,x:unique[0].x,y:unique[0].y};
}"""


_FIND_VISIBLE_TRACK_CONTROL = r"""(target) => {
  const visible = el => { const r=el.getBoundingClientRect(),s=getComputedStyle(el);
    return r.width>0&&r.height>0&&s.display!=='none'&&s.visibility!=='hidden'; };
  const norm = value => String(value||'').normalize('NFKC').toLocaleLowerCase().replace(/\s+/g,' ').trim();
  const title=norm(target.title), artist=norm(target.artist);
  const seconds=Number(target.duration_seconds);
  const duration=Number.isFinite(seconds)&&seconds>0 ? `${Math.floor(seconds/60)}:${String(seconds%60).padStart(2,'0')}` : '';
  const qualifies=el=>{
    if(!visible(el)) return false;
    const t=norm(el.innerText);
    if(t.length<8||t.length>500||!t.includes(title)||!t.includes(artist)) return false;
    return !duration||new RegExp(`(?:^|\\s)${duration.replace(':','\\:')}(?:$|\\s)`).test(t);
  };
  const matched=[...document.querySelectorAll('div,li,article,[role=listitem]')].filter(qualifies);
  if(!matched.length) return {found:false,reason:'vk_visible_track_metadata_not_found'};
  const shortest=Math.min(...matched.map(el=>norm(el.innerText).length));
  const boxes=matched.filter(el=>norm(el.innerText).length===shortest).map(el=>{const r=el.getBoundingClientRect();return {el,x:r.left+r.width/2,y:r.top+r.height/2,
    left:r.left,right:r.right,top:r.top,bottom:r.bottom,area:r.width*r.height};});
  boxes.sort((a,b)=>a.area-b.area);
  const unique=[];
  for(const item of boxes) if(!unique.some(other=>{
    const w=Math.max(0,Math.min(item.right,other.right)-Math.max(item.left,other.left));
    const h=Math.max(0,Math.min(item.bottom,other.bottom)-Math.max(item.top,other.top));
    return w*h/Math.max(1,Math.min(item.area,other.area))>0.7;
  })) unique.push(item);
  if(unique.length!==1) return {found:false,reason:'vk_visible_track_metadata_ambiguous'};
  return {found:true,x:unique[0].x,y:unique[0].y};
}"""


_RESOURCE_DOM_SNAPSHOT = r"""() => {
  const visible = e => { const r=e.getBoundingClientRect(),s=getComputedStyle(e);
    return r.width>0&&r.height>0&&s.display!=='none'&&s.visibility!=='hidden'; };
  const normalize = value => {
    if (typeof value !== 'string') return '';
    const text = value.trim();
    if (/^-?\d+_-?\d+$/.test(text)) return text;
    try {
      const value = JSON.parse(text);
      if (Array.isArray(value) && /^-?\d+$/.test(String(value[0])) && /^-?\d+$/.test(String(value[1]))) return `${value[0]}_${value[1]}`;
      if (value && typeof value === 'object') {
        const owner = value.owner_id ?? value.ownerId, audio = value.id ?? value.audio_id ?? value.audioId;
        if (/^-?\d+$/.test(String(owner)) && /^-?\d+$/.test(String(audio))) return `${owner}_${audio}`;
      }
    } catch {}
    return '';
  };
  const getId = root => {
    if (!(root instanceof Element)) return '';
    const nodes = [root, ...root.querySelectorAll('[data-full-id],[data-audio],[data-audio-id],[data-owner-id],[data-owner_id],a[href*="audio"]')];
    for (const el of nodes) {
      for (const attr of ['data-full-id','data-id','data-audio','data-audio-id']) {
        const id = normalize(el.getAttribute(attr)); if (id) return id;
      }
      if (el.tagName === 'A') {
        try {
          const u = new URL(el.href, location.href);
          const source = u.pathname + ' ' + [...u.searchParams.values()].join(' ');
          const match = source.match(/(?:^|[^a-z0-9])audio(-?\d+)_(-?\d+)(?:$|[^a-z0-9])/i);
          if (match) return `${match[1]}_${match[2]}`;
        } catch {}
      }
      const owner = el.getAttribute('data-owner-id') || el.getAttribute('data-owner_id');
      const audio = el.getAttribute('data-audio-id') || el.getAttribute('data-id');
      if (/^-?\d+$/.test(owner||'') && /^-?\d+$/.test(audio||'')) return `${owner}_${audio}`;
    }
    return '';
  };
  const rows = [...document.querySelectorAll('.audio_row,[data-full-id],[data-audio],[data-audio-id],[data-owner-id],[data-owner_id],a[href*="audio"]')].slice(0, 300);
  const ids = [...new Set(rows.map(getId).filter(Boolean))].slice(0, 30);
  let active = null, activeSource = null;
  for (const row of rows) {
    const className = String(row.className||'');
    const playingClass = /playing|is_playing|current|is_current/i.test(className);
    const pauseControl = [...row.querySelectorAll('[aria-label],[title]')].some(el => /pause/i.test(`${el.getAttribute('aria-label')||''} ${el.getAttribute('title')||''}`));
    if (playingClass || pauseControl) {
      active = getId(row);
      if (active) { activeSource = 'active_row_data'; break; }
    }
  }
  if (!active) {
    const player = document.querySelector('.audio_page_player,[class*=audio_player],[data-testid*=player],[data-qa*=player]');
    active = player ? getId(player) : '';
    if (active) activeSource = 'player_data';
  }
  const controls = [...document.querySelectorAll('button,[role="button"],[aria-label],[title],[class*=play],[class*=pause]')].slice(0, 80)
    .filter(el => /audio|player|play|pause/i.test(`${el.className||''} ${el.getAttribute('aria-label')||''} ${el.getAttribute('title')||''}`))
    .slice(0, 24).map(el => ({tag: el.tagName.toLowerCase(), role: el.getAttribute('role')||'',
      class_hint: String(el.className||'').split(/\s+/).filter(v=>/audio|player|play|pause/i.test(v)).slice(0,4).join(' ').slice(0,100),
      action: /pause/i.test(`${el.getAttribute('aria-label')||''} ${el.getAttribute('title')||''} ${el.className||''}`) ? 'pause'
        : /play/i.test(`${el.getAttribute('aria-label')||''} ${el.getAttribute('title')||''} ${el.className||''}`) ? 'play' : 'unknown'}));
  const media = [...document.querySelectorAll('audio,video')].slice(0, 12).map(el => ({tag: el.tagName.toLowerCase(),
    paused: !!el.paused, ended: !!el.ended, ready_state: Number(el.readyState)||0,
    source_kind: !el.currentSrc ? 'empty' : el.currentSrc.startsWith('blob:') ? 'blob' : 'url'}));
  const modelKeys = Object.keys(window).filter(key => /audio|player|playback|currentAudio/i.test(key)).slice(0, 20);
  const root = document.documentElement;
  const appNodes = [...document.querySelectorAll('[data-reactroot],[data-v-app]')].length;
  const visibleLinks = [...document.querySelectorAll('a[href]')].filter(visible);
  const safeLinkIds = visibleLinks.map(anchor => {
    try {
      const u=new URL(anchor.href,location.href);
      const source=u.pathname+' '+[...u.searchParams.values()].join(' ');
      const match=source.match(/(?:^|[^a-z0-9])audio(-?\d+)_(-?\d+)(?:$|[^a-z0-9])/i);
      return match?`${match[1]}_${match[2]}`:'';
    } catch { return ''; }
  }).filter(Boolean);
  const resultNodes = [...document.querySelectorAll('a,button,div,li,article,[role=listitem]')]
    .filter(el => visible(el) && (el.innerText||'').length <= 220 && /sonne|rammstein/i.test(el.innerText||''))
    .sort((a,b) => (a.innerText||'').length - (b.innerText||'').length).slice(0,10)
    .map(el => ({tag:el.tagName.toLowerCase(), class_hint:String(el.className||'').split(/\s+/).filter(Boolean).slice(0,5).join(' ').slice(0,120),
      text:(el.innerText||'').replace(/\s+/g,' ').trim().slice(0,120), stable_id:getId(el),
      data_attribute_names:[...el.attributes].map(a=>a.name).filter(name=>name.startsWith('data-')).slice(0,12),
      has_click_handler:typeof el.onclick==='function'}));
  const path=location.pathname.toLowerCase();
  return {page_kind: /\/audio(?:-?\d+_-?\d+)?(?:\/|$)/i.test(location.pathname) ? 'vk_audio' : /login/i.test(location.pathname) ? 'login' : 'other',
    route_kind: /^\/audio-?\d+_-?\d+\/?$/i.test(location.pathname) ? 'direct_audio' : path==='/search/audio' ? 'search_audio' : path==='/audio' ? 'audio_home' : path.includes('/search') ? 'search_other' : 'other',
    visibility: document.visibilityState, active_audio_id: active || null, active_audio_id_source: activeSource,
    audio_id_candidates: ids, controls, media, native_media_count: media.length,
    visible_audio_link_count: visibleLinks.filter(anchor => /audio/i.test(anchor.pathname||'') || /audio/i.test(anchor.getAttribute('href')||'')).length,
    visible_audio_link_id_count: safeLinkIds.length, data_audio_element_count: document.querySelectorAll('[data-full-id],[data-audio],[data-audio-id]').length,
    data_owner_id_element_count: document.querySelectorAll('[data-owner-id],[data-owner_id]').length,
    visible_result_nodes: resultNodes,
    visible_headings: [...document.querySelectorAll('h1,h2,h3')].filter(visible).slice(0,8).map(el=>(el.innerText||'').trim().slice(0,80)),
    native_media_playing: media.filter(item => !item.paused && !item.ended).length,
    media_session_metadata_present: !!navigator.mediaSession?.metadata,
    app_root_count: appNodes, window_model_keys: modelKeys};
}"""


_VISIBLE_ROWS = r"""(query) => {
  const visible = e => { const r=e.getBoundingClientRect(),s=getComputedStyle(e);
    return r.width>0&&r.height>0&&s.display!=='none'&&s.visibility!=='hidden'; };
  const text = (root, selectors) => {
    for (const selector of selectors) {
      const e=root.querySelector(selector); if(e&&visible(e)) return (e.innerText||e.textContent||'').trim();
    }
    return '';
  };
  const selectors = '.audio_row, [data-testid*=audio], [data-qa*=audio], [data-full-id], [data-audio], [data-owner-id], [data-owner_id], a[href*="audio"]';
  const candidates = new Set(document.querySelectorAll(selectors));
  const rowFor = node => {
    let row=node;
    for(let i=0;i<7&&row.parentElement;i++) {
      const c=String(row.className||'').toLowerCase();
      const t=(row.innerText||'').replace(/\s+/g,' ').trim();
      if(row.matches?.('.audio_row,[data-testid*=audio],[data-qa*=audio],[data-full-id],[data-audio],[role=listitem]')
        || /audio|track|row/.test(c) || row.hasAttribute('data-owner-id') || row.hasAttribute('data-owner_id')
        || (i>=2&&t.length>=20&&t.length<=500&&row.querySelector('a[href*="audio"]'))) return row;
      row=row.parentElement;
    }
    return node.parentElement||node;
  };
  const idFromHref = anchor => {
    try {
      const u=new URL(anchor.href,location.href);
      const source=u.pathname+' '+[...u.searchParams.values()].join(' ');
      const match=source.match(/(?:^|[^a-z0-9])audio(-?\d+)_(-?\d+)(?:$|[^a-z0-9])/i);
      return match?`${match[1]}_${match[2]}`:'';
    } catch { return ''; }
  };
  for(const anchor of document.querySelectorAll('a[href*="audio"]')) {
    if(visible(anchor)&&idFromHref(anchor)) candidates.add(rowFor(anchor));
  }
  const titleNeedle=(query.title||'').toLocaleLowerCase();
  if(titleNeedle) for(const e of document.querySelectorAll('div,li,article,[role=listitem]')) {
    if(!visible(e)) continue;
    const t=(e.innerText||'').replace(/\s+/g,' ').trim();
    if(t.length>=8&&t.length<=400&&t.toLocaleLowerCase().includes(titleNeedle)) {
      let row=e;
      for(let i=0;i<6&&row.parentElement;i++) {
        const c=String(row.className||'').toLowerCase();
        if(c.includes('audio')||c.includes('track')||c.includes('row')||row.hasAttribute('data-full-id')||row.hasAttribute('data-audio')||row.getAttribute('role')==='listitem') break;
        row=row.parentElement;
      }
      candidates.add(row);
    }
  }
  const out=[];
  for (const e of candidates) {
    if(!visible(e)) continue;
    const descendants=Array.from(e.querySelectorAll('[data-full-id],[data-audio],[data-audio-id],[data-owner-id],[data-owner_id],a[href*="audio"]')).slice(0,12);
    const sources=[e,...descendants];
    const attr=(name)=>sources.map(x=>x.getAttribute(name)).find(Boolean)||'';
    const full=attr('data-full-id')||attr('data-id');
    const audio=attr('data-audio');
    const audioId=attr('data-audio-id');
    const hrefId=descendants.map(x=>x.tagName==='A'?idFromHref(x):'').find(Boolean)||'';
    const owner=attr('data-owner-id')||attr('data-owner_id');
    const localAudio=audioId||attr('data-id');
    const title=text(e,['.audio_row__title_inner','.audio_row__title','.audio_title','[class*=title]']);
    const artist=text(e,['.audio_row__performers','.audio_row__artist','.audio_performer','[class*=performer]','[class*=artist]']);
    const duration=text(e,['.audio_row__duration','.audio_duration']);
    const lines=(e.innerText||'').split(/\n+/).map(x=>x.trim()).filter(Boolean);
    const parsedDuration=duration||lines.find(x=>/^\d{1,2}:\d{2}(?::\d{2})?$/.test(x))||'';
    const fallbackTitle=lines.find(x=>x!==parsedDuration&&x.length>1&&!/^(play|pause|more|like)$/i.test(x))||'';
    const fallbackArtist=lines.find(x=>x!==fallbackTitle&&x!==parsedDuration&&x.length>1)||'';
    out.push({full_id:full,audio_data:audio,audio_id:audioId,href_id:hrefId,owner_id:owner,local_audio_id:localAudio,title:title||fallbackTitle,artist:artist||fallbackArtist,duration:parsedDuration,
      text:(e.innerText||'').replace(/\s+/g,' ').trim().slice(0,300),
      label:[e.getAttribute('aria-label')||'',...Array.from(e.querySelectorAll('[aria-label]')).slice(0,4).map(x=>x.getAttribute('aria-label'))].join(' '),
      class_name:String(e.className||'').slice(0,120)});
  }
  return out;
}"""


class VKAudioBrowserReader:
    """Uses ordinary VK UI in a visible, memory-only browser context."""

    def __init__(self, *, headless: bool = False):
        self.headless = headless
        self._session_lock = asyncio.Lock()
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self._diagnostic_cdp = None
        self._diagnostic_state = None
        self._diagnostic_requests: dict[str, dict] = {}
        self._diagnostic_pending_request_headers: dict[str, dict] = {}
        self._diagnostic_pending_response_headers: dict[str, dict] = {}
        self._diagnostic_page_response_handler = None
        self._diagnostic_expiry_task = None
        self._resource_capture: dict | None = None
        self._resource_descriptors: dict[str, VKResourceDescriptor] = {}
        self._resource_binding_installed = False
        self._resource_capture_expiry_task = None
        self._resource_page = None
        self._resource_page_handler = None
        self._last_search_capture = {"json_response_count": 0, "distilled_record_count": 0,
                                     "dom_row_count": 0, "candidate_count": 0}

    async def _ensure_browser_locked(self, playwright_factory) -> None:
        if self._browser is not None:
            return
        try:
            self._playwright = await playwright_factory().start()
            self._browser = await self._playwright.chromium.launch(channel="chrome", headless=self.headless)
            self._context = await self._browser.new_context(accept_downloads=False)
            self._page = await self._context.new_page()
        except Exception as exc:
            raise _reader_operation_failure("browser_launch", exc) from None

    async def search_in_session(self, reference: RecordingReference) -> VKAudioSearchResult:
        """Search in a visible in-memory VK session without shell prompts."""
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise _reader_operation_failure("browser_launch", exc) from None
        async with self._session_lock:
            await self._ensure_browser_locked(async_playwright)
            page = self._page
            if page is None or page.is_closed():
                raise VKReaderOperationFailure("browser_page", "vk_reader_page_unavailable", "PageClosedError")
            try:
                await page.goto("https://vk.com/audio", wait_until="domcontentloaded", timeout=45000)
                await page.wait_for_timeout(1200)
            except Exception as exc:
                raise _reader_operation_failure("navigation", exc) from None
            path = page.url.split("?", 1)[0].rstrip("/").casefold()
            if "login" in path or await page.locator('input[type="password"]').count():
                raise VKLoginRequired
            query = f"{reference.artist} {reference.title}"
            response_rows: list[dict] = []
            response_bytes = 0
            response_count = 0

            async def capture_search_response(response) -> None:
                nonlocal response_bytes, response_count
                try:
                    parsed = urlsplit(response.url)
                    host = (parsed.hostname or "").casefold().rstrip(".")
                    if not (host == "vk.com" or host.endswith(".vk.com") or host == "vk.ru" or host.endswith(".vk.ru")):
                        return
                    if response.status != 200 or response.request.resource_type not in {"xhr", "fetch"}:
                        return
                    content_type = str(response.headers.get("content-type", "")).casefold()
                    if "json" not in content_type or response_count >= 40 or response_bytes >= 8 * 1024 * 1024:
                        return
                    length = response.headers.get("content-length")
                    if length and length.isdigit() and int(length) > 2 * 1024 * 1024:
                        return
                    body = await response.body()
                    if len(body) > 2 * 1024 * 1024 or response_bytes + len(body) > 8 * 1024 * 1024:
                        return
                    response_count += 1
                    response_bytes += len(body)
                    payload = json.loads(body.decode("utf-8-sig"))
                    response_rows.extend(_visible_search_recordings(payload, limit=max(0, 300-len(response_rows))))
                except Exception:
                    # The regular UI search remains usable when a response is
                    # not JSON or its schema is not a recording list.
                    return

            page.on("response", capture_search_response)
            try:
                await self._search_in_ui(page, query)
                await page.wait_for_timeout(3000)
                rows = await page.evaluate(_VISIBLE_ROWS, {"title": reference.title, "artist": reference.artist})
            except RuntimeError as exc:
                raise VKSearchUnavailable from exc
            except Exception as exc:
                raise _reader_operation_failure("search", exc) from None
            finally:
                page.remove_listener("response", capture_search_response)
            visible_response_rows = [item for item in response_rows if _search_recording_visible_in_rows(item, rows)]
            all_recordings = clean_recordings([*rows, *visible_response_rows])
            ranked = match_recordings(reference, VKAudioSearchResult(query, len(all_recordings), all_recordings))
            # These response bodies can contain adjacent non-audio feed items.
            # Keep only recordings the shared Matching Engine considers
            # plausible; unknown durations remain uncertain, never confirmed.
            recordings = tuple(match.recording for match in ranked if match.assessment.status != "mismatch")
            self._last_search_capture = {"json_response_count": response_count,
                                         "distilled_record_count": len(response_rows),
                                         "visible_response_record_count": len(visible_response_rows),
                                         "dom_row_count": len(rows), "raw_candidate_count": len(all_recordings),
                                         "candidate_count": len(recordings)}
            return VKAudioSearchResult(query, len(recordings), recordings)

    async def read_visible_results(self, reference: RecordingReference) -> VKAudioSearchResult:
        """Read rows after the user searched manually in the opened VK window."""
        async with self._session_lock:
            page = self._page
            if page is None or page.is_closed():
                raise VKLoginRequired
            path = page.url.split("?", 1)[0].rstrip("/").casefold()
            if "login" in path or await page.locator('input[type="password"]').count():
                raise VKLoginRequired
            rows = await page.evaluate(_VISIBLE_ROWS, {"title": reference.title, "artist": reference.artist})
            recordings = clean_recordings(rows)
            return VKAudioSearchResult(f"{reference.artist} {reference.title}", len(recordings), recordings)

    async def read_direct_recording(self, vk_url: str) -> VKAudioRecording:
        """Open and inspect one explicit VK audio page in this Reader context."""
        target_id = recording_id_from_url(vk_url)
        if not target_id:
            raise ValueError("invalid_vk_audio_url")
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise _reader_operation_failure("browser_launch", exc) from None
        async with self._session_lock:
            await self._ensure_browser_locked(async_playwright)
            page = self._page
            if page is None or page.is_closed():
                raise VKReaderOperationFailure("browser_page", "vk_reader_page_unavailable", "PageClosedError")
            response_rows: list[dict] = []

            async def capture_direct_page_metadata(response) -> None:
                try:
                    parsed = urlsplit(response.url)
                    host = (parsed.hostname or "").casefold().rstrip(".")
                    if (host not in {"vk.com", "www.vk.com", "vk.ru", "www.vk.ru"}
                            or response.status != 200
                            or "json" not in str(response.headers.get("content-type", "")).casefold()):
                        return
                    length = response.headers.get("content-length", "")
                    if length.isdigit() and int(length) > 2 * 1024 * 1024:
                        return
                    body = await response.body()
                    if len(body) > 2 * 1024 * 1024:
                        return
                    rows = _visible_search_recordings(json.loads(body.decode("utf-8-sig")), limit=300)
                    response_rows.extend(row for row in rows if row.get("full_id") == target_id)
                except Exception:
                    return

            page.on("response", capture_direct_page_metadata)
            try:
                try:
                    await page.goto(vk_url, wait_until="domcontentloaded", timeout=45000)
                    await page.wait_for_timeout(2200)
                except Exception as exc:
                    raise _reader_operation_failure("direct_navigation", exc) from None
                if (await self._reader_page_status(page)).get("login_required"):
                    raise VKLoginRequired
                if _direct_vk_page_id(page.url) != target_id:
                    raise RuntimeError("vk_direct_page_identity_not_preserved")
                try:
                    visible_rows = await page.evaluate(_VISIBLE_ROWS, {"title": "", "artist": ""})
                except Exception:
                    visible_rows = []
                visible = next((item for item in clean_recordings(visible_rows) if item.id == target_id), None)
                if visible is not None:
                    return visible
                # The direct route itself is the exact selection. Accept only the
                # structured recording object carrying that same owner_audio ID.
                record = next((item for item in clean_recordings(response_rows) if item.id == target_id), None)
                if record is None:
                    raise RuntimeError("vk_direct_metadata_unavailable")
                return record
            finally:
                page.remove_listener("response", capture_direct_page_metadata)

    async def start_playback_diagnostics(self) -> dict:
        """Observe media response metadata on this reader's own page only."""
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise _reader_operation_failure("browser_launch", exc) from None
        async with self._session_lock:
            await self._ensure_browser_locked(async_playwright)
            page = self._page
            if page is None or page.is_closed():
                raise VKReaderOperationFailure("browser_page", "vk_reader_page_unavailable", "PageClosedError")
            if (await self._reader_page_status(page)).get("login_required"):
                raise VKLoginRequired
            if page.url == "about:blank":
                try:
                    await page.goto("https://vk.com/audio", wait_until="domcontentloaded", timeout=45000)
                    await page.wait_for_timeout(1000)
                except Exception as exc:
                    raise _reader_operation_failure("navigation", exc) from None
            if self._diagnostic_cdp is not None:
                return self._diagnostic_snapshot_locked()

            cdp = await self._context.new_cdp_session(page)
            await cdp.send("Runtime.enable")
            await cdp.send("Runtime.addBinding", {"name": "__msVkResourceAction"})
            await cdp.send("Network.enable")
            state = {
                "active": True,
                "started_at": time.monotonic(),
                "request_count": 0,
                "media_requests": deque(maxlen=_DIAGNOSTICS_MAX_MEDIA),
                "drm_counters": {"encrypted_media_events": 0, "waiting_for_key_events": 0,
                                 "key_systems_requested": []},
            }
            self._diagnostic_state = state
            self._diagnostic_requests = {}
            self._diagnostic_pending_request_headers = {}
            self._diagnostic_pending_response_headers = {}
            self._diagnostic_cdp = cdp
            self._diagnostic_page_response_handler = self._diagnostic_playwright_response
            page.on("response", self._diagnostic_page_response_handler)
            cdp.on("Network.requestWillBeSent", self._diagnostic_request)
            cdp.on("Network.requestWillBeSentExtraInfo", self._diagnostic_request_extra)
            cdp.on("Network.responseReceived", self._diagnostic_response)
            cdp.on("Network.responseReceivedExtraInfo", self._diagnostic_response_extra)
            cdp.on("Network.loadingFinished", self._diagnostic_loading_finished)
            cdp.on("Runtime.bindingCalled", self._diagnostic_play_action)
            self._resource_binding_installed = True
            try:
                await page.evaluate(_INSTALL_MEDIA_DIAGNOSTICS)
            except Exception:
                # CDP response metadata remains useful when VK has no DOM media element.
                pass
            session_started = state["started_at"]
            self._diagnostic_expiry_task = asyncio.create_task(
                self._expire_diagnostics(session_started, _DIAGNOSTICS_TTL_SECONDS))
            result = self._diagnostic_snapshot_locked()
            result["reader_page"] = await self._reader_page_status(page)
            return result

    async def arm_resource_capture(self, vk_audio_id: str, musicstream_origin: str) -> dict:
        """Arm exact-ID Play capture; raw media URLs stay in this process only."""
        if not isinstance(vk_audio_id, str) or not _OWNER_AUDIO_ID.fullmatch(vk_audio_id):
            raise ValueError("invalid_vk_audio_id")
        origin = urlsplit(musicstream_origin)
        if origin.scheme not in {"http", "https"} or not origin.netloc or origin.path not in {"", "/"}:
            raise ValueError("invalid_musicstream_origin")
        await self.start_playback_diagnostics()
        async with self._session_lock:
            page = self._page
            if page is None or page.is_closed():
                raise RuntimeError("vk_reader_page_unavailable")
            if (await self._reader_page_status(page)).get("login_required"):
                raise VKLoginRequired
            if self._diagnostic_cdp is not None and not self._resource_binding_installed:
                await self._diagnostic_cdp.send("Runtime.enable")
                try:
                    await self._diagnostic_cdp.send("Runtime.addBinding", {"name": "__msVkResourceAction"})
                except Exception:
                    # CDP reports an error if the binding already exists.
                    pass
                self._diagnostic_cdp.on("Runtime.bindingCalled", self._diagnostic_play_action)
                self._resource_binding_installed = True
            self._resource_capture = {"vk_audio_id": vk_audio_id, "musicstream_origin": origin.scheme + "://" + origin.netloc,
                "armed_at": time.monotonic(), "play_action_sequence": None, "matched_request_count": 0,
                "manifest_seen": False, "expected_manifest_urls": set(), "expected_media_urls": set(), "descriptor": None,
                "pending_descriptor": None, "play_confirmed": False, "click_detected": False,
                "active_audio_id": None, "playing_detected": False, "observer_attached": False,
                "play_response_summary": None,
                "observed_page_kind": "unavailable",
                "reason": "waiting_for_exact_play_and_resource_id"}
            if self._resource_capture_expiry_task:
                self._resource_capture_expiry_task.cancel()
            capture_started = self._resource_capture["armed_at"]
            self._resource_capture_expiry_task = asyncio.create_task(self._expire_resource_capture(capture_started))
            await page.evaluate(_INSTALL_RESOURCE_OBSERVER, {"targetId": vk_audio_id})
            self._resource_capture["observer_attached"] = True
            self._resource_capture["observed_page_kind"] = (await self._reader_page_status(page))["page_kind"]
            self._bind_resource_navigation(page)
            return self._resource_capture_summary()

    async def search_play_and_capture(self, reference: RecordingReference, vk_audio_id: str,
                                      musicstream_origin: str, *, timeout_seconds: float = 28) -> dict:
        """Use ordinary VK search and the exact row's visible Play control, then wait for its ID-bound MPD."""
        results = await self.search_in_session(reference)
        candidate = next((item for item in results.recordings if item.id == vk_audio_id), None)
        if candidate is None:
            return {"descriptor_ready": False, "reason": "vk_candidate_not_in_current_search"}
        async with self._session_lock:
            page = self._page
            if page is None or page.is_closed():
                raise RuntimeError("vk_reader_page_unavailable")
            if (await self._reader_page_status(page)).get("login_required"):
                raise VKLoginRequired
        await self.arm_resource_capture(vk_audio_id, musicstream_origin)
        async with self._session_lock:
            page = self._page
            if page is None or page.is_closed():
                raise RuntimeError("vk_reader_page_unavailable")
            capture = self._resource_capture
            if capture and capture.get("vk_audio_id") == vk_audio_id:
                capture["expected_title"] = candidate.title
                capture["expected_artist"] = candidate.artist or ""
                capture["expected_duration_seconds"] = candidate.duration_seconds
                await page.evaluate(_INSTALL_RESOURCE_OBSERVER, {"targetId": vk_audio_id,
                    "title": candidate.title, "artist": candidate.artist or "",
                    "duration_seconds": candidate.duration_seconds})
            target = await page.evaluate(_FIND_EXACT_PLAY_CONTROL, vk_audio_id)
            if not isinstance(target, dict) or not target.get("found"):
                target = await page.evaluate(_FIND_VISIBLE_TRACK_CONTROL, {
                    "title": candidate.title, "artist": candidate.artist or "",
                    "duration_seconds": candidate.duration_seconds,
                })
            if not isinstance(target, dict) or not target.get("found"):
                return {"descriptor_ready": False,
                        "reason": target.get("reason", "vk_track_play_control_not_found") if isinstance(target, dict) else "vk_track_play_control_not_found"}
            if capture and capture.get("vk_audio_id") == vk_audio_id:
                # This sequence is set before the trusted mouse event so a fast
                # network request cannot race observer state initialization.
                capture["play_action_sequence"] = int(time.monotonic() * 1000)
                capture["play_clicked_at"] = time.monotonic()
                capture["click_detected"] = True
                capture["reason"] = "reader_clicked_exact_vk_track_play"
            await page.mouse.click(float(target["x"]), float(target["y"]))
        deadline = time.monotonic() + max(1.0, min(timeout_seconds, 45.0))
        while time.monotonic() < deadline:
            state = await self.resource_capture_status()
            if state.get("descriptor_ready"):
                return state
            await asyncio.sleep(0.4)
        return await self.resource_capture_status()

    async def direct_play_and_capture(self, reference: RecordingReference, vk_url: str,
                                      musicstream_origin: str, *, timeout_seconds: float = 28) -> dict:
        """Load the exact direct page under capture, then use its page-level Play if needed."""
        target_id = recording_id_from_url(vk_url)
        if not target_id:
            return {"descriptor_ready": False, "reason": "invalid_vk_audio_url"}
        # Arm before navigation so the owner_audio-bearing page/API response is
        # available to link a later media request. Arming after metadata reading
        # misses the only response that may explicitly contain the resource URL.
        await self.arm_resource_capture(target_id, musicstream_origin)
        capture = self._resource_capture
        if capture and capture.get("vk_audio_id") == target_id:
            capture["play_action_sequence"] = int(time.monotonic() * 1000)
            capture["direct_page_selection"] = True
            capture["reason"] = "reader_opening_exact_vk_audio_page"
        item = await self.read_direct_recording(vk_url)
        if item.id != target_id:
            return {"descriptor_ready": False, "reason": "vk_direct_page_identity_not_preserved"}
        assessment = assess(reference, item.title, item.artist, item.duration_seconds)
        if assessment.status == "mismatch":
            return {"descriptor_ready": False, "reason": "vk_direct_recording_mismatch"}
        async with self._session_lock:
            page = self._page
            if page is None or page.is_closed():
                raise RuntimeError("vk_reader_page_unavailable")
        if _direct_vk_page_id(page.url) != target_id:
            return {"descriptor_ready": False, "reason": "vk_direct_page_identity_not_preserved"}
        if capture and capture.get("vk_audio_id") == target_id:
            capture["expected_title"] = item.title
            capture["expected_artist"] = item.artist or ""
            capture["expected_duration_seconds"] = item.duration_seconds
        await page.evaluate(_INSTALL_RESOURCE_OBSERVER, {"targetId": target_id,
            "title": item.title, "artist": item.artist or "",
            "duration_seconds": item.duration_seconds})
        state = await self.resource_capture_status()
        if state.get("descriptor_ready"):
            return state
        target = await page.evaluate(_FIND_DIRECT_PAGE_PLAY_CONTROL, {
            "targetId": target_id, "title": item.title,
            "artist": item.artist or "", "duration_seconds": item.duration_seconds,
        })
        if not isinstance(target, dict) or not target.get("found"):
            target = await page.evaluate(_FIND_EXACT_PLAY_CONTROL, target_id)
        if not isinstance(target, dict) or not target.get("found"):
            return {"descriptor_ready": False,
                    "reason": target.get("reason", "vk_direct_play_control_not_found")
                    if isinstance(target, dict) else "vk_direct_play_control_not_found"}
        if target.get("action") == "pause":
            await page.mouse.click(float(target["x"]), float(target["y"]))
            await page.wait_for_timeout(350)
            target = await page.evaluate(_FIND_DIRECT_PAGE_PLAY_CONTROL, {
                "targetId": target_id, "title": item.title,
                "artist": item.artist or "", "duration_seconds": item.duration_seconds,
            })
            if not isinstance(target, dict) or not target.get("found") or target.get("action") != "play":
                return {"descriptor_ready": False, "reason": "vk_direct_page_play_control_not_ready"}
        if capture and capture.get("vk_audio_id") == target_id:
            capture["play_action_sequence"] = int(time.monotonic() * 1000)
            capture["play_clicked_at"] = time.monotonic()
            capture["click_detected"] = True
            capture["reason"] = "reader_clicked_direct_vk_track_play"
        await page.mouse.click(float(target["x"]), float(target["y"]))
        deadline = time.monotonic() + max(1.0, min(timeout_seconds, 45.0))
        while time.monotonic() < deadline:
            state = await self.resource_capture_status()
            if state.get("descriptor_ready"):
                return state
            await asyncio.sleep(0.4)
        return await self.resource_capture_status()

    def _bind_resource_navigation(self, page) -> None:
        if self._resource_page is not None and self._resource_page_handler is not None:
            try:
                self._resource_page.remove_listener("framenavigated", self._resource_page_handler)
            except Exception:
                pass
        self._resource_page = page
        capture_started = self._resource_capture["armed_at"]

        def on_navigation(frame):
            if frame != page.main_frame:
                return
            asyncio.create_task(self._reattach_resource_observer(page, capture_started))

        self._resource_page_handler = on_navigation
        page.on("framenavigated", on_navigation)

    async def _reattach_resource_observer(self, page, capture_started: float) -> None:
        capture = self._resource_capture
        if not capture or capture.get("armed_at") != capture_started or page.is_closed():
            return
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=10000)
            if self._resource_capture is not capture or capture.get("armed_at") != capture_started:
                return
            result = await page.evaluate(_INSTALL_RESOURCE_OBSERVER, {"targetId": capture["vk_audio_id"],
                "title": capture.get("expected_title", ""), "artist": capture.get("expected_artist", ""),
                "duration_seconds": capture.get("expected_duration_seconds")})
            capture["observer_attached"] = result is True
            capture["observed_page_kind"] = (await self._reader_page_status(page))["page_kind"]
        except Exception:
            capture["observer_attached"] = False
            capture["observed_page_kind"] = "unavailable"

    async def _expire_resource_capture(self, started_at: float) -> None:
        try:
            await asyncio.sleep(180)
            async with self._session_lock:
                capture = self._resource_capture
                if capture and capture.get("armed_at") == started_at:
                    self._resource_descriptors.pop(capture["vk_audio_id"], None)
                    self._resource_capture = None
        except asyncio.CancelledError:
            pass

    async def resource_capture_status(self) -> dict:
        async with self._session_lock:
            capture = self._resource_capture
            if capture and time.monotonic() - capture["armed_at"] > 180:
                self._resource_descriptors.pop(capture["vk_audio_id"], None)
                self._resource_capture = None
                capture = None
            page = self._page
            if capture and page is not None and not page.is_closed():
                try:
                    observed = await page.evaluate(_RESOURCE_CAPTURE_SNAPSHOT)
                    if isinstance(observed, dict):
                        capture.update({
                            "observer_attached": bool(observed.get("observer_attached")),
                            "observed_page_kind": (await self._reader_page_status(page))["page_kind"],
                            "click_detected": bool(capture.get("click_detected") or observed.get("click_detected")),
                            "active_audio_id": observed.get("active_audio_id") or capture.get("active_audio_id"),
                            "playing_detected": bool(capture.get("playing_detected") or observed.get("playing_detected")),
                        })
                        # MediaSession is an additional confirmation when VK exposes it;
                        # resource identity is established independently through the exact
                        # owner_audio response URL and the subsequent matching Media request.
                        session = await page.evaluate(_MEDIA_SESSION_SNAPSHOT)
                        if isinstance(session, dict):
                            norm = lambda value: " ".join(str(value or "").casefold().split())
                            expected_title = norm(capture.get("expected_title"))
                            expected_artist = norm(capture.get("expected_artist"))
                            session_title = norm(session.get("title"))
                            session_artist = norm(session.get("artist"))
                            if (capture.get("play_action_sequence") is not None
                                    and session.get("playback_state") == "playing"
                                    and expected_title and expected_artist
                                    and expected_title == session_title
                                    and expected_artist == session_artist):
                                capture["active_audio_id"] = capture["vk_audio_id"]
                                capture["playing_detected"] = True
                                capture["play_confirmed"] = True
                                capture["media_session_confirmed"] = True
                                if capture.get("pending_media"):
                                    self._promote_progressive_media(capture)
                        model = await page.evaluate(_VK_PLAYER_MODEL_SNAPSHOT)
                        if isinstance(model, dict):
                            capture["vk_player_model_available"] = bool(model.get("model_available"))
                            capture["vk_player_active_audio_ids"] = model.get("active_audio_ids", [])
                            if (capture.get("play_action_sequence") is not None and model.get("playing") is True
                                    and capture["vk_audio_id"] in capture["vk_player_active_audio_ids"]):
                                capture["active_audio_id"] = capture["vk_audio_id"]
                                capture["playing_detected"] = True
                                capture["play_confirmed"] = True
                                if not capture.get("resource_link_confirmed"):
                                    capture["reason"] = "vk_player_model_confirmed_playback_not_resource_link"
                        if observed.get("click_audio_id") == capture["vk_audio_id"] and isinstance(observed.get("click_sequence"), int):
                            capture["play_action_sequence"] = observed["click_sequence"]
                            if not capture.get("play_response_summary"):
                                capture["reason"] = "target_play_click_detected"
                        elif observed.get("click_detected") and not capture.get("play_confirmed"):
                            capture["reason"] = ("play_click_seen_active_id_unmatched" if observed.get("active_audio_id")
                                                  else "play_click_seen_active_id_not_found")
                        if observed.get("playing_detected") and observed.get("active_audio_id") == capture["vk_audio_id"]:
                            capture["play_confirmed"] = True
                            capture["reason"] = "vk_playing_detected" if not capture.get("descriptor") else "descriptor_ready"
                        elif not observed.get("observer_attached"):
                            capture["reason"] = "observer_not_attached"
                        elif capture.get("observed_page_kind") != "vk_audio":
                            capture["reason"] = "reader_page_not_vk_audio"
                except Exception:
                    capture["observer_attached"] = False
                    capture["observed_page_kind"] = "unavailable"
            return self._resource_capture_summary(capture)

    def get_resource_descriptor(self, vk_audio_id: str) -> VKResourceDescriptor | None:
        """Internal-only handoff for the VK provider; never serialize this object to clients."""
        descriptor = self._resource_descriptors.get(vk_audio_id)
        if descriptor is None:
            return None
        if descriptor.expires_at <= time.monotonic():
            self._resource_descriptors.pop(vk_audio_id, None)
            return None
        return descriptor

    def _resource_capture_summary(self, capture: dict | None = None) -> dict:
        capture = self._resource_capture if capture is None else capture
        if capture is None:
            return {"active": False, "descriptor_ready": False, "reason": "resource_capture_not_armed"}
        descriptor = capture.get("descriptor")
        if descriptor and descriptor.expires_at <= time.monotonic():
            self._resource_descriptors.pop(descriptor.vk_audio_id, None)
            capture["descriptor"] = None
            descriptor = None
            capture["reason"] = "source_expired"
        unlinked_mp3_responses = sum(1 for request in self._diagnostic_requests.values()
            if request.get("resource_type") == "Media"
            and request.get("_observed_media_response", {}).get("mimeType", "").split(";", 1)[0].lower() == "audio/mpeg"
            and request.get("_capture_audio_id") != capture["vk_audio_id"])
        return {"active": True, "vk_audio_id": capture["vk_audio_id"],
            "observer_attached": capture.get("observer_attached", False),
            "observed_page_kind": capture.get("observed_page_kind", "unavailable"),
            "click_detected": capture.get("click_detected", False),
            "active_audio_id": capture.get("active_audio_id"),
            "playing_detected": capture.get("playing_detected", False),
            "vk_player_model_available": capture.get("vk_player_model_available", False),
            "play_observed": capture.get("play_action_sequence") is not None,
            "playing_confirmed": capture.get("play_confirmed", False),
            "matched_request_count": capture["matched_request_count"],
            "descriptor_ready": descriptor is not None,
            "descriptor": descriptor.summary() if descriptor else None,
            "play_response": capture.get("play_response_summary"),
            "unlinked_mp3_response_count": unlinked_mp3_responses,
            "reason": "descriptor_ready" if descriptor else capture["reason"]}

    def _promote_progressive_media(self, capture: dict) -> None:
        media = capture.get("pending_media")
        if not media or not (capture.get("media_session_confirmed") or capture.get("resource_link_confirmed")):
            return
        now = time.monotonic()
        media_type = str(media.get("mime") or "").split(";", 1)[0].strip().lower()
        codec = "mp3" if media_type == "audio/mpeg" else None
        descriptor = VKResourceDescriptor(vk_audio_id=capture["vk_audio_id"],
            media_type=media_type, codec=codec, manifest_url="", media_url=media["url"],
            segment_origins=(media["origin"],), detected_at=now, expires_at=now + 60,
            access_restricted=False)
        capture["descriptor"] = descriptor
        self._resource_descriptors[descriptor.vk_audio_id] = descriptor
        capture.pop("pending_media", None)
        capture["reason"] = "descriptor_ready"

    def _diagnostic_play_action(self, event: dict) -> None:
        capture = self._resource_capture
        if not capture or event.get("name") != "__msVkResourceAction":
            return
        try:
            payload = json.loads(event.get("payload", ""))
        except (ValueError, TypeError):
            return
        if (not isinstance(payload, dict) or
                (payload.get("targetId") != capture["vk_audio_id"] and payload.get("audioId") != capture["vk_audio_id"]) or
                not isinstance(payload.get("sequence"), int)):
            return
        action = payload.get("action")
        if action == "click":
            capture["click_detected"] = True
            capture["active_audio_id"] = payload.get("audioId")
            if payload.get("audioId") == capture["vk_audio_id"]:
                capture["play_action_sequence"] = payload["sequence"]
                if not capture.get("play_response_summary"):
                    capture["reason"] = "target_play_click_detected"
            else:
                capture["reason"] = "play_click_seen_active_id_unmatched"
        elif (action == "playing" and payload.get("audioId") == capture["vk_audio_id"]):
            capture["active_audio_id"] = payload["audioId"]
            capture["playing_detected"] = True
            capture["play_confirmed"] = True
            if capture.get("pending_descriptor"):
                descriptor = capture.pop("pending_descriptor")
                capture["descriptor"] = descriptor
                self._resource_descriptors[descriptor.vk_audio_id] = descriptor
                capture["expected_manifest_urls"].clear()
            if capture.get("media_session_confirmed"):
                self._promote_progressive_media(capture)
            capture["reason"] = "descriptor_ready" if capture.get("descriptor") else "vk_playing_detected"
        elif action == "play":
            capture["click_detected"] = True
            capture["active_audio_id"] = payload.get("audioId")
            capture["play_action_sequence"] = payload["sequence"]
            capture["reason"] = "play_observed_waiting_for_id_correlated_manifest"

    @staticmethod
    def _request_mentions_audio_id(url: str, post_data: str, vk_audio_id: str) -> bool:
        owner, audio = vk_audio_id.split("_", 1)
        for value in (url, post_data):
            if not value:
                continue
            decoded = value.replace("%2D", "-").replace("%5F", "_").replace("%2C", ",")
            if re.search(rf"(?<![\d-]){re.escape(vk_audio_id)}(?!\d)", decoded):
                return True
            if re.search(rf"(?<!\d){re.escape(owner)}[,_\s]+{re.escape(audio)}(?!\d)", decoded):
                return True
            try:
                fields = parse_qs(urlsplit(value).query if value == url else value, keep_blank_values=True)
            except (ValueError, TypeError):
                fields = {}
            field_owner = next((fields[key][-1] for key in ("owner_id", "ownerId", "owner") if fields.get(key)), None)
            field_audio = next((fields[key][-1] for key in ("audio_id", "audioId", "id") if fields.get(key)), None)
            if field_owner == owner and field_audio == audio:
                return True
            # Common JSON request form: {owner_id: ..., id: ...}.
            try:
                parsed = json.loads(post_data) if post_data and value == post_data else None
            except (ValueError, TypeError):
                parsed = None
            if isinstance(parsed, dict):
                owner_value = parsed.get("owner_id", parsed.get("ownerId"))
                audio_value = parsed.get("id", parsed.get("audio_id", parsed.get("audioId")))
                if str(owner_value) == owner and str(audio_value) == audio:
                    return True
        return False

    async def playback_diagnostics(self) -> dict:
        async with self._session_lock:
            if self._diagnostic_state is None:
                return {"active": False, "reason": "diagnostics_not_started", "media_requests": []}
            if (self._diagnostic_state["active"] and
                    time.monotonic() - self._diagnostic_state["started_at"] >= _DIAGNOSTICS_TTL_SECONDS):
                await self._stop_diagnostics_locked()
            page = self._page
            if page is not None and not page.is_closed() and self._diagnostic_state["active"]:
                try:
                    dom_state = await page.evaluate("""() => window.__musicstreamVkMediaDiagnostics || null""")
                    if isinstance(dom_state, dict):
                        self._diagnostic_state["drm_counters"].update({
                            "encrypted_media_events": int(dom_state.get("encrypted_media_events", 0)),
                            "waiting_for_key_events": int(dom_state.get("waiting_for_key_events", 0)),
                            "key_systems_requested": [value for value in dom_state.get("key_systems_requested", [])
                                                       if isinstance(value, str) and
                                                       re.fullmatch(r"[a-zA-Z0-9._-]{1,100}", value)][:8],
                        })
                except Exception:
                    pass
            result = self._diagnostic_snapshot_locked()
            result["resource_capture"] = self._resource_capture_summary()
            result["search_capture"] = dict(self._last_search_capture)
            if page is not None and not page.is_closed():
                result["reader_page"] = await self._reader_page_status(page)
                try:
                    result["reader_ui"] = await page.evaluate(_RESOURCE_DOM_SNAPSHOT)
                except Exception:
                    result["reader_ui"] = {"snapshot_available": False}
            return result

    @staticmethod
    async def _reader_page_status(page) -> dict:
        try:
            path = urlsplit(page.url).path.casefold()
            password_field = await page.locator('input[type="password"]').count() > 0
            login_required = "login" in path or password_field
            return {"page_kind": "login" if login_required else "vk_audio" if "audio" in path else "other",
                    "login_required": login_required}
        except Exception:
            return {"page_kind": "unavailable", "login_required": None}

    async def stop_playback_diagnostics(self) -> dict:
        async with self._session_lock:
            if self._diagnostic_state is not None and self._diagnostic_state["active"]:
                await self._stop_diagnostics_locked()
            return self._diagnostic_snapshot_locked()

    async def _expire_diagnostics(self, started_at: float, ttl: int) -> None:
        try:
            await asyncio.sleep(ttl)
            async with self._session_lock:
                if (self._diagnostic_state is not None and self._diagnostic_state["started_at"] == started_at
                        and self._diagnostic_state["active"]):
                    await self._stop_diagnostics_locked(expired=True)
        except asyncio.CancelledError:
            pass

    async def _stop_diagnostics_locked(self, *, expired: bool = False) -> None:
        if self._diagnostic_state is not None:
            self._diagnostic_state["active"] = False
            self._diagnostic_state["stop_reason"] = "expired" if expired else "stopped_by_request"
        task = self._diagnostic_expiry_task
        self._diagnostic_expiry_task = None
        if task is not None and task is not asyncio.current_task():
            task.cancel()
        cdp = self._diagnostic_cdp
        self._diagnostic_cdp = None
        page = self._page
        if page is not None and self._diagnostic_page_response_handler is not None:
            try:
                page.remove_listener("response", self._diagnostic_page_response_handler)
            except Exception:
                pass
        self._diagnostic_page_response_handler = None
        if cdp is not None:
            try:
                await cdp.detach()
            except Exception:
                pass
        self._diagnostic_requests.clear()
        self._diagnostic_pending_request_headers.clear()
        self._diagnostic_pending_response_headers.clear()

    def _diagnostic_request(self, event: dict) -> None:
        state = self._diagnostic_state
        if not state or not state["active"]:
            return
        request_id = event.get("requestId")
        request = event.get("request") or {}
        if not isinstance(request_id, str):
            return
        raw_url = request.get("url", "")
        parts = urlsplit(raw_url if isinstance(raw_url, str) else "")
        host = (parts.hostname or "").lower()
        resource_type = event.get("type") if isinstance(event.get("type"), str) else "Other"
        headers = request.get("headers") if isinstance(request.get("headers"), dict) else {}
        item = self._diagnostic_requests.setdefault(request_id, {
            "host": host if _SAFE_HOST.fullmatch(host) else "unknown",
            "resource_type": resource_type[:24],
            "path_media_hint": parts.path.lower().endswith(_MEDIA_SUFFIXES),
            "method": request.get("method") if request.get("method") in {"GET", "HEAD", "POST"} else "other",
            "request_cookie_present": self._contains_header(headers, "cookie"),
            "request_authorization_present": self._contains_header(headers, "authorization"),
            "request_range_present": self._contains_header(headers, "range"),
            "_observed_at": time.monotonic(),
        })
        capture = self._resource_capture
        if capture and isinstance(raw_url, str) and (parts.path.lower().endswith(".mpd") or ".mpd?" in raw_url.lower()):
            # Candidate only; it is not associated until the exact URL is found in the ID-bound Play response.
            item["_observed_manifest_url"] = raw_url
        mentions_id = bool(capture and capture.get("play_action_sequence") is not None and isinstance(raw_url, str)
                           and self._request_mentions_audio_id(raw_url, request.get("postData", ""), capture["vk_audio_id"]))
        expected_manifest = bool(capture and isinstance(raw_url, str) and raw_url in capture.get("expected_manifest_urls", set()))
        if mentions_id:
            # This request body/URL is the exact owner_audio binding. Its response may contain the manifest URL.
            item["_capture_id_request"] = True
            item["_capture_audio_id"] = capture["vk_audio_id"]
            item["_capture_action_sequence"] = capture["play_action_sequence"]
            capture["matched_request_count"] += 1
            capture["reason"] = "id_correlated_play_request_seen"
        expected_media = bool(capture and capture.get("play_action_sequence") is not None
                              and isinstance(raw_url, str)
                              and raw_url in capture.get("expected_media_urls", set()))
        if expected_media:
            item["_capture_audio_id"] = capture["vk_audio_id"]
            item["_capture_url"] = raw_url
            item["_capture_action_sequence"] = capture["play_action_sequence"]
            item["_capture_link_kind"] = "id_bound_play_response_media_url"
            capture["matched_request_count"] += 1
            capture["reason"] = "id_linked_media_request_seen"
        if expected_manifest:
            # Keep the URL only on the process object after exact ID evidence and a captured Play action.
            item["_capture_audio_id"] = capture["vk_audio_id"]
            item["_capture_url"] = raw_url
            item["_capture_action_sequence"] = capture["play_action_sequence"]
            capture["matched_request_count"] += 1
            capture["reason"] = "response_linked_manifest_request_seen"
        pending = self._diagnostic_pending_request_headers.pop(request_id, None)
        if pending:
            item.update(pending)
        state["request_count"] += 1
        redirect = event.get("redirectResponse")
        if isinstance(redirect, dict):
            self._append_diagnostic_response(request_id, item, redirect, is_redirect=True)
        if len(self._diagnostic_requests) > 700:
            self._diagnostic_requests.pop(next(iter(self._diagnostic_requests)))

    def _diagnostic_request_extra(self, event: dict) -> None:
        request_id = event.get("requestId")
        item = self._diagnostic_requests.get(request_id)
        headers = event.get("headers") if isinstance(event.get("headers"), dict) else {}
        presence = {"request_cookie_present": self._contains_header(headers, "cookie"),
                    "request_authorization_present": self._contains_header(headers, "authorization"),
                    "request_range_present": self._contains_header(headers, "range")}
        if item is None:
            self._diagnostic_pending_request_headers[request_id] = presence
            if len(self._diagnostic_pending_request_headers) > 300:
                self._diagnostic_pending_request_headers.pop(next(iter(self._diagnostic_pending_request_headers)))
            return
        item.update(presence)
        pending_media = item.pop("_pending_progressive_response", None)
        if pending_media:
            self._capture_progressive_media_response(request_id, item, pending_media)

    def _diagnostic_response(self, event: dict) -> None:
        if not self._diagnostic_state or not self._diagnostic_state["active"]:
            return
        request_id = event.get("requestId")
        response = event.get("response") if isinstance(event.get("response"), dict) else {}
        item = self._diagnostic_requests.get(request_id)
        if item is None:
            return
        if item.get("_capture_id_request"):
            item["_capture_id_response_status"] = response.get("status")
            item["_capture_id_response_mime"] = str(response.get("mimeType", "")).split(";", 1)[0].strip().lower()
        response["resource_type"] = event.get("type")
        if (self._resource_capture and
                str(response.get("mimeType", "")).split(";", 1)[0].strip().lower() == "application/dash+xml"
                and isinstance(response.get("url"), str)):
            item["_observed_manifest_url"] = response["url"]
        if self._resource_capture and item.get("resource_type") == "Media" and isinstance(response.get("url"), str):
            item["_observed_media_url"] = response["url"]
            response_headers = response.get("headers") if isinstance(response.get("headers"), dict) else {}
            allowed_headers = {"content-length", "content-range", "accept-ranges"}
            item["_observed_media_response"] = {
                "url": response["url"], "mimeType": response.get("mimeType"), "status": response.get("status"),
                "headers": {str(key): value for key, value in response_headers.items()
                            if str(key).casefold() in allowed_headers}}
        if self._append_diagnostic_response(request_id, item, response):
            pending = self._diagnostic_pending_response_headers.pop(request_id, None)
            if pending:
                target = next((entry for entry in reversed(self._diagnostic_state["media_requests"])
                               if entry.get("_request_id") == request_id), None)
                if target:
                    target.update(pending)
                    target["request_cookie_present"] = item.get("request_cookie_present")
                    target["request_authorization_present"] = item.get("request_authorization_present")
                    target["request_range_present"] = item.get("request_range_present")
            if item.get("_capture_audio_id") and item.get("_capture_action_sequence") is not None:
                raw_url = item.get("_capture_url")
                mime = str(response.get("mimeType", "")).split(";", 1)[0].strip().lower()
                if isinstance(raw_url, str) and (mime == "application/dash+xml" or urlsplit(raw_url).path.lower().endswith(".mpd")):
                    self._capture_manifest_response(request_id, item, response)
            elif item.get("_observed_manifest_url"):
                self._store_manifest_response_metadata(item, response)
        if (self._resource_capture and item.get("_capture_audio_id") == self._resource_capture.get("vk_audio_id")
                and item.get("_capture_action_sequence") == self._resource_capture.get("play_action_sequence")
                and item.get("resource_type") == "Media"):
            self._capture_progressive_media_response(request_id, item, response)

    def _capture_progressive_media_response(self, request_id: str, request: dict, response: dict) -> None:
        capture = self._resource_capture
        if (not capture or request.get("_capture_audio_id") != capture.get("vk_audio_id")
                or request.get("_capture_action_sequence") != capture.get("play_action_sequence")
                or request.get("_capture_url") != response.get("url")):
            return
        mime = str(response.get("mimeType", "")).split(";", 1)[0].strip().lower()
        status = response.get("status")
        if mime not in {"audio/mpeg", "audio/mp4"} or status not in {200, 206}:
            return
        if request.get("request_cookie_present") is None or request.get("request_authorization_present") is None:
            # ExtraInfo may be delivered after responseReceived. Hold only the
            # in-memory response metadata until credential presence is known.
            headers = response.get("headers") if isinstance(response.get("headers"), dict) else {}
            allowed = {"content-length", "content-range", "accept-ranges"}
            request["_pending_progressive_response"] = {
                "url": response.get("url"), "mimeType": response.get("mimeType"),
                "status": response.get("status"),
                "headers": {str(key): value for key, value in headers.items() if str(key).casefold() in allowed}}
            capture["reason"] = "vk_progressive_audio_waiting_for_request_header_metadata"
            return
        raw_url = response.get("url")
        if not isinstance(raw_url, str):
            return
        parsed = urlsplit(raw_url)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.port not in (None, 443)
                or parsed.username or parsed.password or parsed.fragment
                or not _SAFE_HOST.fullmatch(parsed.hostname.lower())):
            capture["reason"] = "vk_progressive_audio_url_invalid"
            return
        headers = response.get("headers") if isinstance(response.get("headers"), dict) else {}
        normalized = {str(key).casefold(): str(value).strip() for key, value in headers.items()}
        range_ok = (normalized.get("accept-ranges", "").casefold() == "bytes"
                    or (status == 206 and normalized.get("content-range", "").lower().startswith("bytes ")))
        if not range_ok:
            capture["reason"] = "vk_progressive_audio_range_not_supported"
            return
        if request.get("request_cookie_present") is not False or request.get("request_authorization_present") is not False:
            capture["reason"] = "vk_progressive_audio_requires_browser_credentials"
            return
        origin = f"{parsed.scheme}://{parsed.netloc}"
        capture["pending_media"] = {"url": raw_url, "origin": origin, "status": status,
            "mime": mime, "range_supported": True, "request_id": request_id}
        capture["resource_link_confirmed"] = True
        capture["reason"] = "vk_id_linked_progressive_audio_ready"
        self._promote_progressive_media(capture)

    def _capture_manifest_response(self, request_id: str, request: dict, response: dict) -> None:
        capture = self._resource_capture
        cdp = self._diagnostic_cdp
        if (not capture or not cdp or capture.get("play_action_sequence") != request.get("_capture_action_sequence")
                or capture.get("vk_audio_id") != request.get("_capture_audio_id")):
            return
        capture["manifest_seen"] = True
        capture["reason"] = "id_correlated_manifest_inspecting_content_and_cors"
        if isinstance(response.get("status"), int) and response["status"] in {200, 206}:
            # Exact owner_audio Play request/response linkage plus successful
            # manifest fetch confirms the selected item even if VK renders its
            # player state outside native HTMLMediaElement.
            capture["play_confirmed"] = True
        self._store_manifest_response_metadata(request, response)

    @staticmethod
    def _store_manifest_response_metadata(request: dict, response: dict) -> None:
        headers = response.get("headers") if isinstance(response.get("headers"), dict) else {}
        normalized = {str(key).casefold(): str(value).strip() for key, value in headers.items()}
        request["_candidate_manifest_response"] = {
            "mime": str(response.get("mimeType", "")).split(";", 1)[0].strip().lower(),
            "status": response.get("status"),
            "allow_origin": normalized.get("access-control-allow-origin", "").lower(),
            "allow_credentials": normalized.get("access-control-allow-credentials", "").lower() == "true",
        }
        if request.get("_capture_audio_id"):
            request["_capture_manifest_response"] = request["_candidate_manifest_response"]

    def _diagnostic_loading_finished(self, event: dict) -> None:
        request_id = event.get("requestId")
        request = self._diagnostic_requests.get(request_id)
        cdp = self._diagnostic_cdp
        if not request or not cdp:
            return
        if request.get("_capture_manifest_response"):
            asyncio.create_task(self._read_resource_manifest(cdp, request_id, request,
                                                            request["_capture_manifest_response"]))
        elif request.get("_capture_id_request"):
            asyncio.create_task(self._inspect_id_response_for_manifest(cdp, request_id, request))

    def _diagnostic_playwright_response(self, response) -> None:
        """Read an exact-ID response through Playwright as a fallback to CDP bodies."""
        capture = self._resource_capture
        if not capture or capture.get("play_action_sequence") is None:
            return
        try:
            request = response.request
            post_data = request.post_data or ""
            if not self._request_mentions_audio_id(request.url, post_data, capture["vk_audio_id"]):
                return
            seen = capture.setdefault("_playwright_response_objects", set())
            request_key = id(request)
            if request_key in seen:
                return
            seen.add(request_key)
            asyncio.create_task(self._inspect_playwright_id_response(
                response, capture["vk_audio_id"], capture["play_action_sequence"], int(response.status)))
        except Exception:
            return

    async def _inspect_playwright_id_response(self, response, vk_audio_id: str,
                                              action_sequence: int, status: int) -> None:
        capture = self._resource_capture
        if (not capture or capture.get("vk_audio_id") != vk_audio_id
                or capture.get("play_action_sequence") != action_sequence):
            return
        try:
            headers = response.headers
            content_type = str(headers.get("content-type", "")).split(";", 1)[0].strip().lower()
            length = headers.get("content-length", "")
            if length.isdigit() and int(length) > 2 * 1024 * 1024:
                raise ValueError("play_response_too_large")
            body_bytes = await response.body()
            if len(body_bytes) > 2 * 1024 * 1024:
                raise ValueError("play_response_too_large")
            body = body_bytes.decode("utf-8-sig", errors="strict").replace("\\/", "/").replace("&amp;", "&")
            payload = None
            body_kind = "text"
            try:
                payload = json.loads(body)
                body_kind = "json_object" if isinstance(payload, dict) else "json_array" if isinstance(payload, list) else "json_scalar"
            except (ValueError, TypeError):
                pass
            top_level_keys = []
            if isinstance(payload, dict):
                top_level_keys = sorted(key for key in payload if isinstance(key, str)
                    and re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,39}", key)
                    and not re.search(r"token|cookie|auth|secret|key|sig", key, re.I))[:24]
            strings = []
            stack = [(payload, 0)] if payload is not None else [(body, 0)]
            visited = 0
            while stack and visited < 5000:
                value, depth = stack.pop()
                visited += 1
                if depth > 12:
                    continue
                if isinstance(value, str):
                    strings.append(value)
                elif isinstance(value, dict):
                    stack.extend((child, depth + 1) for child in value.values())
                elif isinstance(value, list):
                    stack.extend((child, depth + 1) for child in value[:1000])
            urls = set()
            for value in strings:
                for candidate in re.findall(r"(?:https?:)?//[^\s\"'<>\\]+", value):
                    urls.add(candidate.rstrip(",;)}]"))
                decoded = unquote(value)
                if decoded != value:
                    for candidate in re.findall(r"https://[^\s\"'<>\\]+", decoded):
                        urls.add(candidate.rstrip(",;)}]"))
            manifests, media_urls = set(), set()
            for value in urls:
                clean = "https:" + value if value.startswith("//") else value
                parts = urlsplit(clean)
                if (parts.scheme != "https" or not parts.hostname or not _SAFE_HOST.fullmatch(parts.hostname.lower())
                        or parts.username or parts.password or parts.fragment):
                    continue
                if parts.path.lower().endswith(".mpd") or ".mpd?" in clean.lower():
                    manifests.add(clean)
                else:
                    media_urls.add(clean)
            capture["play_response_summary"] = {
                "status": status, "content_type": content_type or "unknown", "body_kind": body_kind,
                "body_source": "playwright_response", "top_level_keys": top_level_keys,
                "secure_url_count": len(urls), "manifest_url_count": len(manifests),
                "other_url_count": len(media_urls),
            }
            cdp = self._diagnostic_cdp
            self._link_id_response_urls(capture, vk_audio_id, action_sequence, manifests, media_urls, cdp)
            if not manifests and not media_urls:
                capture["reason"] = "id_response_contains_no_usable_resource_url"
            elif not capture.get("descriptor"):
                capture["reason"] = "id_response_resource_url_not_fetched_as_supported_audio"
        except Exception as exc:
            capture = self._resource_capture
            if capture and capture.get("vk_audio_id") == vk_audio_id:
                capture["play_response_summary"] = {
                    "status": status, "content_type": str(getattr(response, "headers", {}).get("content-type", "unknown")).split(";", 1)[0].lower(),
                    "body_kind": "unavailable", "body_source": "playwright_response",
                    "error_type": type(exc).__name__,
                }
                capture["reason"] = "id_response_body_unavailable_or_unparseable"

    def _link_id_response_urls(self, capture: dict, vk_audio_id: str, action_sequence: int,
                               manifests: set[str], media_urls: set[str], cdp) -> None:
        if (self._resource_capture is not capture or capture.get("vk_audio_id") != vk_audio_id
                or capture.get("play_action_sequence") != action_sequence):
            return
        capture["expected_manifest_urls"].update(manifests)
        capture["expected_media_urls"].update(media_urls)
        for candidate_id, candidate in self._diagnostic_requests.items():
            observed_manifest = candidate.get("_observed_manifest_url")
            if observed_manifest in manifests:
                candidate["_capture_audio_id"] = vk_audio_id
                candidate["_capture_action_sequence"] = action_sequence
                candidate["_capture_url"] = candidate.pop("_observed_manifest_url")
                candidate["_capture_manifest_response"] = candidate.get("_candidate_manifest_response")
                capture["matched_request_count"] += 1
                if cdp and candidate.get("_capture_manifest_response"):
                    asyncio.create_task(self._read_resource_manifest(
                        cdp, candidate_id, candidate, candidate["_capture_manifest_response"]))
            observed_media = candidate.get("_observed_media_url")
            if candidate.get("resource_type") == "Media" and observed_media in media_urls:
                candidate["_capture_audio_id"] = vk_audio_id
                candidate["_capture_action_sequence"] = action_sequence
                candidate["_capture_url"] = observed_media
                candidate["_capture_link_kind"] = "id_bound_play_response_media_url"
                capture["matched_request_count"] += 1
                media_response = candidate.get("_observed_media_response")
                if media_response:
                    self._capture_progressive_media_response(candidate_id, candidate, media_response)

    async def _inspect_id_response_for_manifest(self, cdp, request_id: str, request: dict) -> None:
        try:
            current = self._resource_capture
            if (not current or request.get("_capture_audio_id") != current.get("vk_audio_id")
                    or request.get("_capture_action_sequence") != current.get("play_action_sequence")
                    or request.get("_capture_id_response_status") not in {200, 206}):
                return
            result = await cdp.send("Network.getResponseBody", {"requestId": request_id})
            body = result.get("body", "")
            if result.get("base64Encoded"):
                import base64
                body = base64.b64decode(body).decode("utf-8", errors="strict")
            if not isinstance(body, str) or len(body) > 2 * 1024 * 1024:
                raise ValueError("play_response_invalid")
            body = body.replace("\\/", "/").replace("&amp;", "&")
            payload = None
            body_kind = "text"
            try:
                payload = json.loads(body)
                body_kind = "json_object" if isinstance(payload, dict) else "json_array" if isinstance(payload, list) else "json_scalar"
            except (ValueError, TypeError):
                pass
            top_level_keys = []
            if isinstance(payload, dict):
                top_level_keys = sorted(key for key in payload if isinstance(key, str)
                    and re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,39}", key)
                    and not re.search(r"token|cookie|auth|secret|key|sig", key, re.I))[:24]

            strings = []
            stack = [(payload, 0)] if payload is not None else [(body, 0)]
            visited = 0
            while stack and visited < 5000:
                value, depth = stack.pop()
                visited += 1
                if depth > 12:
                    continue
                if isinstance(value, str):
                    strings.append(value)
                elif isinstance(value, dict):
                    stack.extend((child, depth + 1) for child in value.values())
                elif isinstance(value, list):
                    stack.extend((child, depth + 1) for child in value[:1000])
            urls = set()
            for value in strings:
                for candidate in re.findall(r"(?:https?:)?//[^\s\"'<>\\]+", value):
                    urls.add(candidate.rstrip(",;)}]"))
                decoded = unquote(value)
                if decoded != value:
                    for candidate in re.findall(r"https://[^\s\"'<>\\]+", decoded):
                        urls.add(candidate.rstrip(",;)}]"))
            linked = set()
            linked_media = set()
            for value in urls:
                clean = value
                if clean.startswith("//"):
                    clean = "https:" + clean
                parts = urlsplit(clean)
                if (parts.scheme == "https" and parts.hostname and _SAFE_HOST.fullmatch(parts.hostname.lower())
                        and not parts.username and not parts.password
                        and (parts.path.lower().endswith(".mpd") or ".mpd?" in clean.lower())):
                    linked.add(clean)
                elif (parts.scheme == "https" and parts.hostname and _SAFE_HOST.fullmatch(parts.hostname.lower())
                      and not parts.username and not parts.password and not parts.fragment):
                    # Candidate only: it becomes an audio resource only if this exact URL
                    # is subsequently fetched as a Media request with audio/mpeg response.
                    linked_media.add(clean)
            if (current and request.get("_capture_audio_id") == current["vk_audio_id"]
                    and request.get("_capture_action_sequence") == current.get("play_action_sequence") and linked):
                current["expected_manifest_urls"].update(linked)
                for candidate_id, candidate in self._diagnostic_requests.items():
                    if candidate.get("_observed_manifest_url") in linked:
                        candidate["_capture_audio_id"] = current["vk_audio_id"]
                        candidate["_capture_action_sequence"] = current["play_action_sequence"]
                        candidate["_capture_url"] = candidate.pop("_observed_manifest_url")
                        candidate["_capture_manifest_response"] = candidate.get("_candidate_manifest_response")
                        current["matched_request_count"] += 1
                        if candidate.get("_capture_manifest_response"):
                            asyncio.create_task(self._read_resource_manifest(cdp, candidate_id, candidate,
                                candidate["_capture_manifest_response"]))
                current["reason"] = "play_response_linked_manifest_waiting_for_fetch"
            if (current and request.get("_capture_audio_id") == current["vk_audio_id"]
                    and request.get("_capture_action_sequence") == current.get("play_action_sequence")
                    and linked_media):
                current["expected_media_urls"].update(linked_media)
                for candidate_id, candidate in self._diagnostic_requests.items():
                    if (candidate.get("resource_type") == "Media"
                            and candidate.get("_observed_media_url") in linked_media):
                        candidate["_capture_audio_id"] = current["vk_audio_id"]
                        candidate["_capture_action_sequence"] = current["play_action_sequence"]
                        candidate["_capture_url"] = candidate["_observed_media_url"]
                        candidate["_capture_link_kind"] = "id_bound_play_response_media_url"
                        current["matched_request_count"] += 1
                        media_response = candidate.get("_observed_media_response")
                        if media_response:
                            self._capture_progressive_media_response(candidate_id, candidate, media_response)
            if current and request.get("_capture_audio_id") == current.get("vk_audio_id"):
                current["play_response_summary"] = {
                    "status": request.get("_capture_id_response_status"),
                    "content_type": request.get("_capture_id_response_mime") or "unknown",
                    "body_kind": body_kind,
                    "top_level_keys": top_level_keys,
                    "secure_url_count": len(urls),
                    "manifest_url_count": len(linked),
                    "other_url_count": len(linked_media),
                }
                if not linked and not linked_media:
                    current["reason"] = "id_response_contains_no_usable_resource_url"
                elif linked_media and not capture.get("descriptor"):
                    current["reason"] = "id_response_resource_url_not_fetched_as_supported_audio"
        except Exception as exc:
            current = self._resource_capture
            if current and request.get("_capture_audio_id") == current["vk_audio_id"]:
                current["play_response_summary"] = {
                    "status": request.get("_capture_id_response_status"),
                    "content_type": request.get("_capture_id_response_mime") or "unknown",
                    "body_kind": "unavailable", "body_source": "cdp_response",
                    "error_type": type(exc).__name__,
                }
                current["reason"] = "id_response_body_unavailable_or_unparseable"

    async def _read_resource_manifest(self, cdp, request_id: str, request: dict, response: dict) -> None:
        try:
            status = response.get("status")
            if isinstance(status, int) and status >= 400:
                capture = self._resource_capture
                if capture and capture.get("vk_audio_id") == request.get("_capture_audio_id"):
                    capture["reason"] = f"manifest_http_{status}"
                return
            result = await cdp.send("Network.getResponseBody", {"requestId": request_id})
            body = result.get("body", "")
            if result.get("base64Encoded"):
                import base64
                body = base64.b64decode(body).decode("utf-8", errors="strict")
            if not isinstance(body, str) or len(body) > 1024 * 1024:
                raise ValueError("manifest_body_invalid")
            root = ElementTree.fromstring(body)
            if root.tag.rsplit("}", 1)[-1] != "MPD":
                raise ValueError("dash_manifest_invalid")
            if any(node.tag.rsplit("}", 1)[-1] == "ContentProtection" for node in root.iter()):
                capture = self._resource_capture
                if capture and capture.get("vk_audio_id") == request.get("_capture_audio_id"):
                    capture["reason"] = "dash_drm_content_rejected"
                return
            mime = next((node.get("mimeType") for node in root.iter()
                         if node.tag.rsplit("}", 1)[-1] in {"Representation", "AdaptationSet"}
                         and (node.get("mimeType") or "").lower().startswith("audio/")), "")
            codec = None
            for adaptation in root.iter():
                if adaptation.tag.rsplit("}", 1)[-1] != "AdaptationSet":
                    continue
                for representation in adaptation:
                    if representation.tag.rsplit("}", 1)[-1] != "Representation":
                        continue
                    rep_mime = (representation.get("mimeType") or adaptation.get("mimeType") or "").lower()
                    if rep_mime.startswith("audio/"):
                        codec = representation.get("codecs") or adaptation.get("codecs")
                        if codec:
                            break
                if codec:
                    break
            base = request.get("_capture_url", "")
            base_parts = urlsplit(base)
            if base_parts.scheme != "https" or not base_parts.hostname or base_parts.username or base_parts.password:
                raise ValueError("manifest_url_invalid")
            origins = {urlsplit(base).scheme + "://" + urlsplit(base).netloc}
            from urllib.parse import urljoin
            for node in root.iter():
                kind = node.tag.rsplit("}", 1)[-1]
                values = ([node.text.strip()] if kind == "BaseURL" and node.text else
                          [node.get(key) for key in ("media", "initialization") if node.get(key)]
                          if kind == "SegmentTemplate" else
                          [node.get(key) for key in ("media", "index") if node.get(key)]
                          if kind == "SegmentURL" else
                          [node.get("sourceURL")] if kind == "Initialization" and node.get("sourceURL") else [])
                for value in values:
                    if kind == "SegmentTemplate":
                        value = re.sub(r"\$[^$]+\$", "segment", value)
                    resolved = urlsplit(urljoin(base, value))
                    if resolved.scheme == "https" and resolved.netloc and not resolved.username and not resolved.password:
                        origins.add(resolved.scheme + "://" + resolved.netloc)
            capture = self._resource_capture
            if not capture or capture.get("vk_audio_id") != request.get("_capture_audio_id"):
                return
            origin = capture["musicstream_origin"].lower()
            cors_allowed, cors_reason = await asyncio.to_thread(_cors_probe_sync, base, origin)
            if not cors_allowed:
                capture["reason"] = cors_reason
                return
            if not mime.lower().startswith("audio/mp4") or not codec or "mp4a.40." not in codec.lower():
                capture["reason"] = "dash_audio_aac_not_confirmed"
                return
            now = time.monotonic()
            descriptor = VKResourceDescriptor(vk_audio_id=capture["vk_audio_id"], media_type=mime,
                codec=codec, manifest_url=base, manifest_xml=body,
                segment_origins=tuple(sorted(origins)), detected_at=now,
                expires_at=now + 90, access_restricted=False, cors_test_origin=origin,
                musicstream_cors_allowed=True)
            if capture.get("play_confirmed"):
                self._resource_descriptors[descriptor.vk_audio_id] = descriptor
                capture["descriptor"] = descriptor
                capture["expected_manifest_urls"].clear()
                capture["reason"] = "descriptor_ready"
            else:
                capture["pending_descriptor"] = descriptor
                capture["reason"] = "resource_linked_waiting_for_vk_playing_event"
        except Exception:
            capture = self._resource_capture
            if capture and capture.get("vk_audio_id") == request.get("_capture_audio_id"):
                capture["reason"] = "manifest_inspection_failed"
        finally:
            request.pop("_capture_url", None)

    def _diagnostic_response_extra(self, event: dict) -> None:
        request_id = event.get("requestId")
        state = self._diagnostic_state
        if state is None:
            return
        responses = state["media_requests"]
        target = next((entry for entry in reversed(responses) if entry.get("_request_id") == request_id), None)
        headers = event.get("headers") if isinstance(event.get("headers"), dict) else {}
        request = self._diagnostic_requests.get(request_id, {})
        safe_headers = {}
        self._merge_safe_headers(safe_headers, headers)
        safe_headers["request_cookie_present"] = request.get("request_cookie_present")
        safe_headers["request_authorization_present"] = request.get("request_authorization_present")
        safe_headers["request_range_present"] = request.get("request_range_present")
        safe_headers["response_set_cookie_present"] = self._contains_header(headers, "set-cookie")
        status = event.get("statusCode")
        if isinstance(status, int) and 100 <= status <= 599:
            safe_headers["status"] = status
            safe_headers["access_restriction_indicator"] = status in {401, 403, 407, 423, 429, 451}
            safe_headers["range_rejected"] = status == 416
        if target is None:
            self._diagnostic_pending_response_headers[request_id] = safe_headers
            if len(self._diagnostic_pending_response_headers) > 300:
                self._diagnostic_pending_response_headers.pop(next(iter(self._diagnostic_pending_response_headers)))
            return
        target.update(safe_headers)

    @staticmethod
    def _contains_header(headers: dict, name: str) -> bool:
        return any(isinstance(key, str) and key.casefold() == name for key in headers)

    @classmethod
    def _merge_safe_headers(cls, target: dict, headers: dict) -> None:
        normalized = {key.casefold(): value for key, value in headers.items() if isinstance(key, str)}
        content_type = normalized.get("content-type")
        if isinstance(content_type, str):
            mime = content_type.split(";", 1)[0].strip().lower()
            target["content_type"] = mime if _SAFE_MIME.fullmatch(mime) else None
        content_length = normalized.get("content-length")
        target["content_length"] = content_length if isinstance(content_length, str) and content_length.isdigit() else None
        content_range = normalized.get("content-range")
        target["content_range"] = (content_range[:80] if isinstance(content_range, str) and
                                    re.fullmatch(r"bytes (?:\d+-\d+/\d+|\*/\d+)", content_range.strip()) else None)
        accept_ranges = normalized.get("accept-ranges")
        target["accept_ranges"] = (accept_ranges[:32].lower() if isinstance(accept_ranges, str) and
                                   re.fullmatch(r"[a-zA-Z0-9, -]{1,32}", accept_ranges) else None)
        target["response_set_cookie_present"] = cls._contains_header(headers, "set-cookie")
        cors_origin = normalized.get("access-control-allow-origin")
        if isinstance(cors_origin, str):
            origin = cors_origin.strip().lower()
            target["cors_allow_origin"] = origin if origin == "*" or origin in {"https://vk.com", "https://www.vk.com"} else "other"
        else:
            target["cors_allow_origin"] = None
        target["cors_allow_credentials"] = normalized.get("access-control-allow-credentials", "").strip().lower() == "true"

    def _append_diagnostic_response(self, request_id: str, request: dict, response: dict,
                                    *, is_redirect: bool = False) -> bool:
        state = self._diagnostic_state
        if state is None:
            return False
        headers = response.get("headers") if isinstance(response.get("headers"), dict) else {}
        mime = response.get("mimeType") if isinstance(response.get("mimeType"), str) else ""
        if not mime:
            normalized = {key.casefold(): value for key, value in headers.items() if isinstance(key, str)}
            mime = str(normalized.get("content-type", "")).split(";", 1)[0]
        mime = mime.strip().lower()
        mime = mime if _SAFE_MIME.fullmatch(mime) else "unknown"
        resource_type = response.get("resource_type") or request.get("resource_type", "Other")
        raw_url = response.get("url", "")
        path_hint = request.get("path_media_hint", False)
        if isinstance(raw_url, str):
            path_hint = path_hint or urlsplit(raw_url).path.lower().endswith(_MEDIA_SUFFIXES)
        manifest = mime in {"application/dash+xml", "application/vnd.apple.mpegurl", "application/x-mpegurl"} or (isinstance(raw_url, str) and urlsplit(raw_url).path.lower().endswith((".mpd", ".m3u8")))
        is_media = resource_type == "Media" or mime.startswith(("audio/", "video/")) or manifest or path_hint
        if not is_media:
            return False
        status = response.get("status")
        if not isinstance(status, int) or not 100 <= status <= 599:
            status = None
        host = request.get("host", "unknown")
        if isinstance(raw_url, str):
            candidate_host = (urlsplit(raw_url).hostname or "").lower()
            host = candidate_host if _SAFE_HOST.fullmatch(candidate_host) else host
        entry = {
            "host": host,
            "resource_type": resource_type[:24] if isinstance(resource_type, str) else "Other",
            "media_type": self._media_type(mime, resource_type, manifest),
            "status": status,
            "is_redirect": bool(is_redirect),
            "mime_type": mime,
            "request_method": request.get("method", "other"),
            "request_cookie_present": request.get("request_cookie_present"),
            "request_authorization_present": request.get("request_authorization_present"),
            "request_range_present": request.get("request_range_present"),
            "response_set_cookie_present": self._contains_header(headers, "set-cookie"),
            "content_type": None,
            "content_length": None,
            "content_range": None,
            "accept_ranges": None,
            "_request_id": request_id,
        }
        self._merge_safe_headers(entry, headers)
        entry["access_restriction_indicator"] = status in {401, 403, 407, 423, 429, 451}
        entry["range_rejected"] = status == 416
        state["media_requests"].append(entry)
        return True

    @staticmethod
    def _media_type(mime: str, resource_type: str, manifest: bool) -> str:
        if manifest:
            return "dash_manifest" if "dash" in mime or mime.endswith("mpd") else "hls_manifest"
        if mime in {"audio/mpeg", "audio/mp3"}:
            return "mp3"
        if mime.startswith("audio/mp4") or mime == "video/mp4":
            return "mp4_audio_or_segment"
        if mime in {"application/octet-stream", "unknown"} and resource_type == "Media":
            return "opaque_media"
        return "other_media"

    def _diagnostic_snapshot_locked(self) -> dict:
        state = self._diagnostic_state
        if state is None:
            return {"active": False, "reason": "diagnostics_not_started", "media_requests": []}
        media = [{key: value for key, value in item.items() if not key.startswith("_")} for item in state["media_requests"]]
        drm = dict(state["drm_counters"])
        drm["detected"] = bool(drm.get("encrypted_media_events") or drm.get("waiting_for_key_events") or drm.get("key_systems_requested"))
        return {"active": state["active"], "reason": state.get("stop_reason"),
                "elapsed_seconds": max(0, int(time.monotonic() - state["started_at"])),
                "request_count": state["request_count"], "media_request_count": len(media),
                "media_requests": media, "drm_indicators": drm,
                "scope": "VK Reader page only; URL values and secret headers are not retained"}

    async def close(self):
        async with self._session_lock:
            if self._diagnostic_state is not None and self._diagnostic_state["active"]:
                await self._stop_diagnostics_locked()
            if self._context is not None:
                await self._context.close()
            if self._browser is not None:
                await self._browser.close()
            if self._playwright is not None:
                await self._playwright.stop()
            self._page = self._context = self._browser = self._playwright = None
            self._diagnostic_requests.clear()
            self._resource_descriptors.clear()
            self._resource_capture = None
            if self._resource_capture_expiry_task:
                self._resource_capture_expiry_task.cancel()
                self._resource_capture_expiry_task = None

    async def search(self, reference: RecordingReference) -> tuple[VKAudioSearchResult, tuple]:
        return (await self.search_many((reference,)))[0]

    async def search_many(self, references: tuple[RecordingReference, ...]) -> list[tuple[VKAudioSearchResult, tuple]]:
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise RuntimeError("Install the optional browser dependency: uv sync --extra hitmos") from exc

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(channel="chrome", headless=self.headless)
            context = await browser.new_context(accept_downloads=False)
            page = await context.new_page()
            try:
                await page.goto("https://vk.com/audio", wait_until="domcontentloaded", timeout=45000)
                print("VK открыт в видимом окне. При необходимости войдите обычным способом; профиль временный.")
                await asyncio.to_thread(input, "После входа нажмите Enter в этом терминале: ")
                await page.goto("https://vk.com/audio", wait_until="domcontentloaded", timeout=45000)
                await page.wait_for_timeout(1800)
                results = []
                for reference in references:
                    query = f"{reference.artist} {reference.title}"
                    print(f"Ищу в стандартном интерфейсе VK Audio: {query}")
                    await self._search_in_ui(page, query)
                    await page.wait_for_timeout(2500)
                    rows = await page.evaluate(_VISIBLE_ROWS, {"title": reference.title, "artist": reference.artist})
                    recordings = clean_recordings(rows)
                    if not recordings:
                        print(f"Автоматическая выдача пуста. В открытой вкладке VK Audio выполните поиск: {query}")
                        await asyncio.to_thread(input, "Когда результаты появятся, нажмите Enter здесь: ")
                        await page.wait_for_timeout(800)
                        rows = await page.evaluate(_VISIBLE_ROWS, {"title": reference.title, "artist": reference.artist})
                        recordings = clean_recordings(rows)
                    batch = VKAudioSearchResult(query, len(recordings), recordings)
                    results.append((batch, match_recordings(reference, batch)))
                return results
            finally:
                # Context is in-memory and discarded; never inspect or export its storage.
                await context.close()
                await browser.close()

    @staticmethod
    async def _search_in_ui(page, query: str) -> None:
        # VK's current visible audio search route is /search/audio (observed
        # in the signed-in UI). Use that ordinary page directly rather than
        # relying on transient category controls from general search.
        parsed = urlsplit(page.url)
        origin = f"{parsed.scheme}://{parsed.netloc}" if parsed.hostname in {"vk.com", "www.vk.com", "vk.ru", "www.vk.ru"} else "https://vk.com"
        await page.goto(f"{origin}/search/audio?q={quote(query)}", wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(1800)
        # The initial music search shows a short track preview. Expand its
        # visible "Show all" control through the UI so candidate metadata can
        # be tied to a rendered row before accepting IDs from response JSON.
        show_all = page.get_by_text(re.compile(r"^(?:\u041f\u043e\u043a\u0430\u0437\u0430\u0442\u044c \u0432\u0441\u0435|Show all)$", re.I))
        for index in range(await show_all.count()):
            item = show_all.nth(index)
            if await item.is_visible():
                await item.click(timeout=4000)
                await page.wait_for_timeout(1200)
                break


class VKLoginRequired(Exception):
    """The visible VK page needs the user to sign in or expose audio search."""


class VKSearchUnavailable(Exception):
    """The VK page is open but its current layout has no searchable field."""


class VKAudioProvider(AudioProvider):
    """VK metadata provider with an opt-in, short-lived DASH playback handoff."""

    def __init__(self, reader: VKAudioBrowserReader | None = None, *, playback_adapter=None,
                 playback_experimental: bool = False):
        self.reader = reader or VKAudioBrowserReader()
        self.playback_adapter = playback_adapter
        self.playback_experimental = playback_experimental
        self.musicstream_origin = ""
        self._recent: dict[tuple, tuple[float, dict[str, DiscoveredRecording]]] = {}
        self._direct_sources: dict[tuple, tuple[float, str]] = {}

    def get_resource_descriptor(self, vk_audio_id: str) -> VKResourceDescriptor | None:
        """Server-internal bridge point for an approved playback adapter."""
        return self.reader.get_resource_descriptor(vk_audio_id)

    @staticmethod
    def _recording_key(recording: RecordingReference) -> tuple:
        return (recording.title, recording.artist, recording.duration_seconds, recording.explicit_lyrics)

    def _remember(self, recording: RecordingReference, batch: DiscoveryBatch) -> None:
        now = time.monotonic()
        self._recent = {key: value for key, value in self._recent.items() if value[0] > now}
        self._recent[self._recording_key(recording)] = (now + 300, {item.id: item for item in batch.recordings})
        while len(self._recent) > 64:
            del self._recent[next(iter(self._recent))]

    async def resolve_direct(self, recording: RecordingReference, vk_url: str) -> SourceResult:
        """Open one user-selected VK page and compare its ID-bound metadata."""
        vk_audio_id = recording_id_from_url(vk_url)
        if not vk_audio_id:
            return SourceResult(source="vk", status="unavailable", reason="invalid_vk_audio_url",
                message="Укажите HTTPS-ссылку vk.ru/audio{owner}_{audio} или vk.com/audio{owner}_{audio}.",
                search_url=self.search_url(recording))
        try:
            item = await self.reader.read_direct_recording(vk_url)
        except VKLoginRequired:
            return SourceResult(source="vk", status="unavailable", reason="auth_required",
                message="Войдите в VK в видимом окне MusicStream Reader и повторите проверку.",
                search_url=vk_url)
        except VKReaderOperationFailure as exc:
            return SourceResult(source="vk", status="error", reason=exc.code,
                message=f"VK Reader failed: stage={exc.stage}; error_type={exc.error_type}.",
                search_url=vk_url, stage="source_search")
        except (RuntimeError, ValueError) as exc:
            reason = str(exc) if str(exc).startswith("vk_") or str(exc) == "invalid_vk_audio_url" else "vk_direct_metadata_unavailable"
            return SourceResult(source="vk", status="unavailable", reason=reason,
                message=f"VK не подтвердил метаданные прямой записи: {reason}.", search_url=vk_url)

        batch = VKAudioSearchResult(vk_url, 1, (item,))
        discovered = self._batch_from_search(recording, batch)
        self._remember(recording, discovered)
        result = rank_batch(recording, discovered)
        candidate = result.candidates[0]
        if candidate.match.status == "mismatch":
            result.status = "unavailable"
            result.reason = "recording_mismatch"
            result.message = "Метаданные записи по ссылке VK не совпали с выбранной композицией MusicStream."
            result.selection = "none"
            result.selected_id = None
            result.selected_source = None
            candidate.playback_status = "playback_unavailable"
            candidate.playback_reason = "recording_mismatch"
            return result

        now = time.monotonic()
        key = (self._recording_key(recording), vk_audio_id)
        self._direct_sources[key] = (now + 300, vk_url)
        self._direct_sources = {entry: value for entry, value in self._direct_sources.items()
                                if value[0] > now}
        while len(self._direct_sources) > 64:
            del self._direct_sources[next(iter(self._direct_sources))]
        candidate.playback_status = "playback_unavailable"
        candidate.playback_reason = "vk_direct_selection_ready"
        result.status = "playback_unavailable"
        result.reason = "vk_direct_selection_ready"
        result.stage = "source_metadata"
        result.message = "Открыта точная запись VK и прочитаны её метаданные. Нажмите кнопку проверки воспроизведения, чтобы подготовить ресурс."
        result.selection = "choose"
        result.selected_id = None
        result.selected_source = None
        return result

    @staticmethod
    def search_url(recording: RecordingReference) -> str:
        return "https://vk.com/audio"

    async def discover(self, recording: RecordingReference) -> DiscoveryBatch:
        url = self.search_url(recording)
        try:
            batch = await self.reader.search_in_session(recording)
        except VKLoginRequired:
            return DiscoveryBatch(source="vk", status="blocked", reason="auth_required",
                message="Войдите в VK в открытом окне Chrome и повторите поиск. Сессия хранится только в памяти MusicStream.", search_url=url)
        except VKSearchUnavailable:
            return DiscoveryBatch(source="vk", status="blocked", reason="manual_search_required",
                message="VK Audio открыт. Выполните поиск в штатном интерфейсе и нажмите «Проверить результаты VK».", search_url=url)
        except VKReaderOperationFailure as exc:
            return DiscoveryBatch(source="vk", status="error", reason=exc.code,
                message=f"VK Reader failed: stage={exc.stage}; error_type={exc.error_type}.",
                search_url=url, complete=False)
        except Exception:
            return DiscoveryBatch(source="vk", status="error", reason="vk_unavailable",
                message="VK Audio сейчас недоступен. Остальные источники продолжают работать.", search_url=url)
        discovered = self._batch_from_search(recording, batch)
        self._remember(recording, discovered)
        return discovered

    @staticmethod
    def _batch_from_search(recording: RecordingReference, batch: VKAudioSearchResult) -> DiscoveryBatch:
        rows = [DiscoveredRecording(id=item.id, title=item.title, artist=item.artist,
                    duration_seconds=item.duration_seconds, method="vk_unavailable", external_url=item.external_url)
                for item in batch.recordings]
        return DiscoveryBatch(source="vk", status="ok" if rows else "blocked",
            reason="candidates_found" if rows else "manual_search_required",
            message=f"Найдено кандидатов VK Audio: {len(rows)}." if rows else "В открытом окне Chrome выполните поиск в штатном VK Audio, затем нажмите «Проверить результаты VK».",
            search_url=VKAudioProvider.search_url(recording), recordings=rows, candidate_count=len(rows))

    async def prepare(self, recording: RecordingReference, candidate_id: str) -> SourceResult:
        recent = self._recent.get(self._recording_key(recording))
        if recent is None or recent[0] <= time.monotonic() or candidate_id not in recent[1]:
            return SourceResult(source="vk", status="unavailable", reason="vk_candidate_not_cached",
                message="Выбранной записи нет в недавней выдаче MusicStream. Выполните VK Audio поиск ещё раз.",
                search_url=self.search_url(recording))
        item = recent[1][candidate_id]
        batch = DiscoveryBatch(source="vk", status="ok", reason="candidates_found",
            message="Проверены метаданные выбранной записи VK.", search_url=self.search_url(recording),
            recordings=[item], candidate_count=1)
        result = rank_batch(recording, batch)
        candidate = result.candidates[0]
        if self.playback_experimental and self.playback_adapter is not None:
            if candidate.match.status == "mismatch":
                result.status = "unavailable"
                result.reason = "recording_mismatch"
                result.message = "VK запись не прошла общую проверку совпадения MusicStream."
                result.selection = "none"
                result.selected_id = None
                result.selected_source = None
                return result
            descriptor = self.get_resource_descriptor(candidate_id)
            if descriptor is None:
                try:
                    direct_key = (self._recording_key(recording), candidate_id)
                    direct_source = self._direct_sources.get(direct_key)
                    if direct_source and direct_source[0] > time.monotonic():
                        capture = await self.reader.direct_play_and_capture(
                            recording, direct_source[1], self.musicstream_origin)
                    else:
                        self._direct_sources.pop(direct_key, None)
                        capture = await self.reader.search_play_and_capture(
                            recording, candidate_id, self.musicstream_origin)
                    descriptor = self.get_resource_descriptor(candidate_id)
                except VKLoginRequired:
                    capture = {"reason": "vk_auth_required"}
                except VKReaderOperationFailure as exc:
                    capture = {"reason": exc.code, "stage": exc.stage, "error_type": exc.error_type}
                except VKSearchUnavailable:
                    capture = {"reason": "vk_search_controls_unavailable"}
                except Exception as exc:
                    capture = {"reason": str(exc) if str(exc).startswith("vk_") else "vk_reader_capture_failed"}
                if descriptor is None:
                    reason = capture.get("reason", "vk_resource_not_captured")
                    context = (f" stage={capture['stage']} error_type={capture['error_type']}"
                               if capture.get("stage") and capture.get("error_type") else "")
                    candidate.playback_status = "playback_unavailable"
                    candidate.playback_reason = reason
                    result.status = "playback_unavailable"
                    result.reason = reason
                    result.stage = "browser_playback"
                    result.message = f"VK resource capture did not confirm an audio resource: {reason}.{context}"
                    result.selection = "none"
                    result.selected_id = None
                    result.selected_source = None
                    return result
            try:
                grant = await self.playback_adapter.prepare(descriptor)
            except ValueError as exc:
                reason = str(exc) if str(exc).startswith("vk_") else "vk_manifest_invalid"
                candidate.playback_status = "playback_unavailable"
                candidate.playback_reason = reason
                result.status = "playback_unavailable"
                result.reason = reason
                result.stage = "stream_resolution"
                result.message = f"VK DASH handoff не подготовлен: {reason}."
                result.selection = "none"
                result.selected_id = None
                result.selected_source = None
                return result
            if grant.get("mode") == "progressive_audio":
                candidate.method = "html_audio"
                candidate.stream_url = grant["resource_url"]
                media_type = descriptor.media_type.split(";", 1)[0].strip().lower()
                result.reason = "vk_progressive_mp3_ready" if media_type == "audio/mpeg" else "vk_progressive_mp4_ready"
                result.message = ("Подтверждённый VK MP3 подготовлен в короткоживущем локальном транспорте MusicStream."
                                  if media_type == "audio/mpeg" else
                                  "Подтверждённый VK audio/mp4 подготовлен в короткоживущем локальном транспорте MusicStream.")
            else:
                candidate.method = "dash_mse"
                candidate.stream_url = f"/api/audio/vk/playback/{grant['playback_id']}/manifest"
            candidate.playback_id = grant["playback_id"]
            candidate.playback_status = None
            candidate.playback_reason = None
            result.status = "available"
            if grant.get("mode") != "progressive_audio":
                result.reason = "vk_dash_ready"
                result.message = "VK AAC/DASH-ресурс подготовлен для общего MSE-плеера MusicStream."
            result.stage = "stream_resolution"
            result.selection = "automatic" if candidate.match.status == "likely" else "choose"
            result.selected_id = candidate.id if result.selection == "automatic" else None
            result.selected_source = "vk" if result.selection == "automatic" else None
            return result
        candidate.playback_status = "playback_unavailable"
        candidate.playback_reason = "vk_external_playback_not_verified"
        result.status = "playback_unavailable"
        result.reason = "vk_external_playback_not_verified"
        result.message = ("Не найден публично документированный способ передать поток этой записи во внешний плеер. "
                          "MusicStream не извлекает временную медиассылку; откройте запись в штатном VK.")
        result.selection = "none"
        result.selected_id = None
        result.selected_source = None
        return result

    async def refresh_visible(self, recording: RecordingReference) -> SourceResult:
        try:
            found = await self.reader.read_visible_results(recording)
            batch = self._batch_from_search(recording, found)
            self._remember(recording, batch)
        except VKLoginRequired:
            batch = DiscoveryBatch(source="vk", status="blocked", reason="auth_required",
                message="Войдите в VK в открытом окне Chrome и повторите поиск. Сессия хранится только в памяти MusicStream.",
                search_url=self.search_url(recording))
        except Exception:
            batch = DiscoveryBatch(source="vk", status="error", reason="vk_unavailable",
                message="VK Audio сейчас недоступен. Остальные источники продолжают работать.",
                search_url=self.search_url(recording))
        return self._rank(recording, batch)

    @staticmethod
    def _rank(recording: RecordingReference, batch: DiscoveryBatch) -> SourceResult:
        result = rank_batch(recording, batch)
        if batch.reason in {"auth_required", "manual_search_required"}:
            result.status = "unavailable"
            result.reason = batch.reason
            return result
        if batch.recordings:
            for candidate in result.candidates:
                candidate.playback_status = "playback_unavailable"
            result.reason = "vk_external_playback_not_verified"
            result.status = "playback_unavailable"
            result.message = ("Записи VK найдены и сопоставлены, но документированный способ передачи потока "
                              "во внешний плеер не подтверждён. Откройте запись в VK.")
        return result

    async def find(self, recording: RecordingReference) -> SourceResult:
        batch = await self.discover(recording)
        return self._rank(recording, batch)

    async def close(self):
        await self.reader.close()
