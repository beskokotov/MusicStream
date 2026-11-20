"""Short-lived, process-local VK resource handoff for resources observed by Reader.

The browser receives only opaque local playback/slot identifiers. Upstream URLs are
kept in this module's memory and are fetched without VK cookies or authorization.
"""

from __future__ import annotations

import ipaddress
import re
import secrets
import socket
import time
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit
from xml.etree import ElementTree

import httpx

from musicstream.audio_providers.vk_browser import VKResourceDescriptor

_TOKEN = re.compile(r"^[A-Za-z0-9_-]{32,64}$")
_SLOT = re.compile(r"^[0-9]{1,4}$")
_SUBSTITUTION = re.compile(r"\$([A-Za-z]+)(?:%0([1-9][0-9]?)d)?\$")
_HOST = re.compile(r"^[a-z0-9.-]{1,253}$")
_MAX_MANIFEST = 1024 * 1024
_MAX_LEASES = 24
_LEASE_SECONDS = 75


@dataclass
class _Lease:
    descriptor: VKResourceDescriptor
    expires_at: float
    resources: dict[str, tuple[str, bool]] = field(default_factory=dict)


class VKPlaybackAdapter:
    """One-process capability map and bounded clear-DASH resource relay."""

    def __init__(self, *, clock=time.monotonic):
        self.clock = clock
        self._leases: dict[str, _Lease] = {}

    def _prune(self) -> None:
        now = self.clock()
        self._leases = {key: value for key, value in self._leases.items()
                        if value.expires_at > now and value.descriptor.expires_at > now}

    async def prepare(self, descriptor: VKResourceDescriptor) -> dict:
        self._prune()
        if descriptor.expires_at <= self.clock():
            raise ValueError("vk_source_expired")
        if descriptor.access_restricted:
            raise ValueError("vk_access_restricted")
        if descriptor.media_url is not None:
            parsed = urlsplit(descriptor.media_url)
            origin = f"{parsed.scheme}://{parsed.netloc}"
            if (descriptor.media_type.split(";", 1)[0].lower() not in {"audio/mpeg", "audio/mp4"}
                    or parsed.scheme != "https" or parsed.port not in (None, 443)
                    or not parsed.hostname or parsed.username or parsed.password or parsed.fragment
                    or origin.lower() not in {item.lower() for item in descriptor.segment_origins}):
                raise ValueError("vk_progressive_resource_invalid")
            token = secrets.token_urlsafe(32)
            lease = _Lease(descriptor, min(descriptor.expires_at, self.clock() + _LEASE_SECONDS),
                           resources={"0": (descriptor.media_url, False)})
            self._prune()
            if len(self._leases) >= _MAX_LEASES:
                oldest = min(self._leases, key=lambda key: self._leases[key].expires_at)
                self._leases.pop(oldest, None)
            self._leases[token] = lease
            return {"playback_id": token, "mode": "progressive_audio",
                    "resource_url": f"/api/audio/vk/playback/{token}/resource/0",
                    "expires_in_seconds": max(1, int(lease.expires_at - self.clock()))}
        if not descriptor.musicstream_cors_allowed:
            raise ValueError("vk_manifest_cors_denied")
        if not descriptor.manifest_xml or len(descriptor.manifest_xml.encode("utf-8")) > _MAX_MANIFEST:
            raise ValueError("vk_manifest_body_unavailable")
        token = secrets.token_urlsafe(32)
        lease = _Lease(descriptor, min(descriptor.expires_at, self.clock() + _LEASE_SECONDS))
        try:
            body = self._rewrite_manifest(lease)
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError("vk_manifest_invalid") from exc
        self._prune()
        if len(self._leases) >= _MAX_LEASES:
            oldest = min(self._leases, key=lambda key: self._leases[key].expires_at)
            self._leases.pop(oldest, None)
        self._leases[token] = lease
        return {"playback_id": token,
                "expires_in_seconds": max(1, int(lease.expires_at - self.clock()))}

    def _rewrite_manifest(self, lease: _Lease) -> str:
        descriptor = lease.descriptor
        root = ElementTree.fromstring(descriptor.manifest_xml)
        if root.tag.rsplit("}", 1)[-1] != "MPD":
            raise ValueError("vk_manifest_invalid")
        if any(node.tag.rsplit("}", 1)[-1] == "ContentProtection" for node in root.iter()):
            raise ValueError("vk_dash_drm_content_rejected")
        if root.get("type", "static").lower() != "static":
            raise ValueError("vk_dash_dynamic_manifest_unsupported")

        parents = {child: parent for parent in root.iter() for child in parent}

        original_bases = {node: node.text.strip() for node in root.iter()
                           if node.tag.rsplit("}", 1)[-1] == "BaseURL" and node.text}

        def scoped_base(node, include_node: bool = False) -> str:
            chain = []
            current = node if include_node else parents.get(node)
            while current is not None:
                chain.append(current)
                current = parents.get(current)
            base = descriptor.manifest_url
            for scope in reversed(chain):
                base_node = next((child for child in scope
                                  if child.tag.rsplit("}", 1)[-1] == "BaseURL" and child in original_bases), None)
                if base_node is not None:
                    base = urljoin(base, original_bases[base_node])
            return base

        # The capability is embedded only as an opaque path. The lease token is
        # installed by prepare after this function returns.
        token_marker = "__VK_PLAYBACK_ID__"

        def local(slot: str) -> str:
            return f"/api/audio/vk/playback/{token_marker}/resource/{slot}"

        for node in list(root.iter()):
            kind = node.tag.rsplit("}", 1)[-1]
            if kind == "BaseURL" and node.text:
                upstream = urljoin(scoped_base(node),
                                   original_bases.get(node, node.text.strip()))
                parsed = urlsplit(upstream)
                if f"{parsed.scheme}://{parsed.netloc}".lower() not in {v.lower() for v in descriptor.segment_origins}:
                    raise ValueError("vk_segment_origin_not_allowed")
                slot = str(len(lease.resources))
                lease.resources[slot] = (upstream, False)
                node.text = local(slot) + "/"
            elif kind == "SegmentTemplate":
                base = scoped_base(node)
                for attr in ("media", "initialization"):
                    value = node.get(attr)
                    if value:
                        upstream = urljoin(base, value)
                        slot = str(len(lease.resources))
                        parts = urlsplit(upstream.split("$", 1)[0])
                        if (parts.scheme != "https" or f"{parts.scheme}://{parts.netloc}".lower()
                                not in {v.lower() for v in descriptor.segment_origins}):
                            raise ValueError("vk_segment_origin_not_allowed")
                        lease.resources[slot] = (upstream, True)
                        placeholders = {}
                        for match in re.finditer(r"\$(Number|Time|RepresentationID|Bandwidth)(?:%0[1-9][0-9]?d)?\$", value):
                            name = match.group(1)
                            placeholders.setdefault(name, match.group(0))
                        query = "&".join(f"{name}={pattern}" for name, pattern in placeholders.items())
                        node.set(attr, local(slot) + ("?" + query if query else ""))
            elif kind == "SegmentURL":
                base = scoped_base(node)
                for attr in ("media", "index"):
                    value = node.get(attr)
                    if value:
                        upstream = urljoin(base, value)
                        parts = urlsplit(upstream)
                        if (parts.scheme != "https" or f"{parts.scheme}://{parts.netloc}".lower()
                                not in {v.lower() for v in descriptor.segment_origins}):
                            raise ValueError("vk_segment_origin_not_allowed")
                        slot = str(len(lease.resources))
                        lease.resources[slot] = (upstream, False)
                        node.set(attr, local(slot))
            elif kind == "Initialization" and node.get("sourceURL"):
                base = scoped_base(node)
                upstream = urljoin(base, node.get("sourceURL"))
                parts = urlsplit(upstream)
                if (parts.scheme != "https" or f"{parts.scheme}://{parts.netloc}".lower()
                        not in {v.lower() for v in descriptor.segment_origins}):
                    raise ValueError("vk_segment_origin_not_allowed")
                slot = str(len(lease.resources))
                lease.resources[slot] = (upstream, False)
                node.set("sourceURL", local(slot))

        if not lease.resources:
            raise ValueError("vk_dash_segments_unsupported")
        xml = ElementTree.tostring(root, encoding="unicode")
        # Replace marker at response time; it is never part of stored MPD input.
        lease.resources["__manifest__"] = (xml, False)
        return xml

    def manifest(self, token: str) -> tuple[str, int]:
        self._prune()
        if not _TOKEN.fullmatch(token) or token not in self._leases:
            raise ValueError("vk_playback_id_expired")
        lease = self._leases[token]
        xml = lease.resources.get("__manifest__", ("", False))[0]
        if not xml:
            raise ValueError("vk_playback_id_expired")
        xml = xml.replace("__VK_PLAYBACK_ID__", token)
        return xml, max(1, int(lease.expires_at - self.clock()))

    def upstream_url(self, token: str, slot: str, suffix: str, query: dict[str, str]) -> tuple[str, tuple[str, ...], str]:
        self._prune()
        if not _TOKEN.fullmatch(token) or not _SLOT.fullmatch(slot) or token not in self._leases:
            raise ValueError("vk_playback_id_expired")
        lease = self._leases[token]
        resource = lease.resources.get(slot)
        if not resource:
            raise ValueError("vk_resource_not_found")
        base, template = resource
        if suffix:
            if any(part in {".", ".."} for part in suffix.split("/")) or "\\" in suffix:
                raise ValueError("vk_resource_path_invalid")
            target = urljoin(base, suffix)
        elif template:
            target = base
            substitutions = {key: value for key, value in query.items() if key in {"Number", "Time", "RepresentationID", "Bandwidth"}}
            def replace(match):
                name, width = match.group(1), match.group(2)
                value = substitutions.get(name)
                if value is None or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", value):
                    raise ValueError("vk_dash_template_parameter_missing")
                return value.zfill(int(width)) if width else value
            target = _SUBSTITUTION.sub(replace, target).replace("$$", "$")
            if "$" in target:
                raise ValueError("vk_dash_template_unsupported")
        else:
            target = base
        parsed = urlsplit(target)
        origins = tuple(lease.descriptor.segment_origins)
        if parsed.scheme != "https" or f"{parsed.scheme}://{parsed.netloc}".lower() not in {v.lower() for v in origins}:
            raise ValueError("vk_segment_origin_not_allowed")
        if range_header := query.get("__range"):
            if not re.fullmatch(r"bytes=(?:\d+-\d*|\d*-\d+)", range_header.strip()):
                raise ValueError("vk_range_invalid")
        return target, origins, lease.descriptor.cors_test_origin or ""

    async def open_resource(self, token: str, slot: str, suffix: str, query: dict[str, str],
                            method: str, range_header: str | None):
        params = dict(query)
        if range_header:
            params["__range"] = range_header
        url, origins, origin = self.upstream_url(token, slot, suffix, params)
        lease = self._leases[token]
        require_cors = lease.descriptor.media_url is None
        return await self.open_upstream(url, origins, origin, method, range_header, require_cors=require_cors)

    @staticmethod
    async def open_upstream(url: str, origins: tuple[str, ...], musicstream_origin: str,
                            method: str, range_header: str | None, *, require_cors: bool = True):
        """No cookies/auth; validate public DNS and each redirect before following."""
        current = url
        client = httpx.AsyncClient(timeout=httpx.Timeout(15, connect=5), follow_redirects=False, trust_env=False)
        try:
            for _ in range(4):
                parsed = urlsplit(current)
                host = (parsed.hostname or "").lower().rstrip(".")
                origin = f"{parsed.scheme}://{parsed.netloc}".lower()
                if (parsed.scheme != "https" or parsed.port not in (None, 443) or origin not in {v.lower() for v in origins}
                        or not _HOST.fullmatch(host) or parsed.username or parsed.password or parsed.fragment):
                    raise ValueError("vk_redirect_origin_not_allowed")
                addresses = {ipaddress.ip_address(item[4][0]) for item in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)}
                if not addresses or any(not address.is_global for address in addresses):
                    raise ValueError("vk_upstream_host_not_public")
                headers = {"Origin": musicstream_origin, "Accept-Encoding": "identity", "User-Agent": "MusicStream/0.1.0"}
                if range_header and re.fullmatch(r"bytes=(?:\d+-\d*|\d*-\d+)", range_header.strip()):
                    headers["Range"] = range_header.strip()
                request = client.build_request(method, current, headers=headers)
                response = await client.send(request, stream=True)
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location")
                    await response.aclose()
                    if not location:
                        raise ValueError("vk_redirect_missing_location")
                    current = urljoin(current, location)
                    continue
                if response.status_code not in {200, 206, 416}:
                    status = response.status_code
                    await response.aclose()
                    raise ValueError(f"vk_segment_http_{status}")
                allow_origin = response.headers.get("access-control-allow-origin", "").strip().lower()
                if require_cors and allow_origin not in {"*", musicstream_origin.lower()}:
                    await response.aclose()
                    raise ValueError("vk_segment_cors_denied")
                return client, response
            raise ValueError("vk_redirect_limit")
        except Exception:
            await client.aclose()
            raise

\n