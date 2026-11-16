"""Safe discovery of a local HTTP CONNECT proxy for Hitmos-only traffic."""

import ipaddress
import os
import sys
from urllib.parse import urlsplit


class HitmosProxyConfigurationError(ValueError):
    """Configured proxy is unsupported or is not a local proxy."""


def _canonical_local_http_proxy(value: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > 512:
        raise HitmosProxyConfigurationError("invalid_local_proxy_configuration")
    raw = value if "://" in value else "http://" + value
    try:
        parsed = urlsplit(raw)
        host = (parsed.hostname or "").rstrip(".").lower()
        port = parsed.port
    except ValueError as exc:
        raise HitmosProxyConfigurationError("invalid_local_proxy_configuration") from exc
    if (parsed.scheme != "http" or not host or not port or not 1 <= port <= 65535
            or parsed.username is not None or parsed.password is not None
            or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
        raise HitmosProxyConfigurationError("unsupported_local_proxy_configuration")
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = host == "localhost"
    if not loopback:
        raise HitmosProxyConfigurationError("proxy_must_be_loopback")
    display_host = f"[{host}]" if ":" in host else host
    return f"http://{display_host}:{port}"


def _select_windows_proxy(value: str) -> str:
    """Read the HTTPS/HTTP entry from WinINet ProxyServer without logging it."""
    entries = {}
    default = None
    for segment in value.split(";"):
        item = segment.strip()
        if not item:
            continue
        if "=" in item:
            key, endpoint = item.split("=", 1)
            entries[key.strip().lower()] = endpoint.strip()
        elif default is None:
            default = item
    endpoint = entries.get("https") or entries.get("http") or default
    if not endpoint:
        raise HitmosProxyConfigurationError("unsupported_local_proxy_configuration")
    return endpoint


def configured_hitmos_proxy() -> str | None:
    """Use explicit local override, else the enabled local WinINet proxy.

    Only unauthenticated loopback HTTP proxies are accepted. Remote proxies,
    SOCKS, PAC scripts and credential-bearing settings fail closed.
    """
    override = os.getenv("HITMOS_HTTP_PROXY")
    if override:
        return _canonical_local_http_proxy(override)
    if sys.platform != "win32":
        return None
    try:
        import winreg

        key_path = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            try:
                enabled, _ = winreg.QueryValueEx(key, "ProxyEnable")
            except FileNotFoundError:
                enabled = 0
            if not enabled:
                try:
                    pac_url, _ = winreg.QueryValueEx(key, "AutoConfigURL")
                except FileNotFoundError:
                    pac_url = None
                if pac_url:
                    raise HitmosProxyConfigurationError("pac_proxy_configuration_unsupported")
                return None
            try:
                pac_url, _ = winreg.QueryValueEx(key, "AutoConfigURL")
            except FileNotFoundError:
                pac_url = None
            if pac_url:
                raise HitmosProxyConfigurationError("pac_proxy_configuration_unsupported")
            try:
                proxy_server, _ = winreg.QueryValueEx(key, "ProxyServer")
            except FileNotFoundError as exc:
                raise HitmosProxyConfigurationError("enabled_proxy_has_no_server") from exc
    except OSError as exc:
        raise HitmosProxyConfigurationError("cannot_read_local_proxy_configuration") from exc
    return _canonical_local_http_proxy(_select_windows_proxy(str(proxy_server)))
