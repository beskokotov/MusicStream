"""Synthetic torrent and local HTTP response fixtures; no external traffic."""

import hashlib
import io
import socket
from pathlib import Path

import libtorrent as lt
import pytest

from musicstream.torrent.safe_webseed import (
    PieceHashMismatchError, SafeWebSeedFetcher, UnsafeWebSeedError,
    WebSeedHTTPError,
)


def _torrent(path: Path, *, seeds=None, private=False):
    first = b"ID3" + b"A" * (10000 - 3)
    second = bytes(range(256)) * 40
    joined = first + second
    info = {
        b"name": b"album", b"piece length": 16384,
        b"pieces": b"".join(hashlib.sha1(joined[i:i + 16384]).digest()
                          for i in range(0, len(joined), 16384)),
        b"files": [
            {b"length": len(first), b"path": [b"one.mp3"]},
            {b"length": len(second), b"path": [b"two.mp3"]},
        ],
    }
    if private:
        info[b"private"] = 1
    path.write_bytes(lt.bencode({b"info": info, b"url-list": seeds if seeds is not None else [
        b"https://archive.org/download/", b"http://unsafe.example/", b"/relative/",
    ]}))
    return str(lt.load_torrent_file(str(path)).ti.info_hashes().v1), (first, second)


class _Socket:
    def __init__(self, host, files, requests, *, mode="ok", redirect_host="dn123456.us.archive.org"):
        self.host = host
        self.files = files
        self.requests = requests
        self.mode = mode
        self.redirect_host = redirect_host
        self.answer = None

    def settimeout(self, value):
        assert value > 0

    def getpeername(self):
        return ("93.184.216.34", 443)

    def sendall(self, data):
        request = data.decode("ascii")
        self.requests.append((self.host, request))
        path = request.split(" ", 2)[1]
        filename = path.rsplit("/", 1)[1]
        if self.host == "archive.org":
            location = f"https://{self.redirect_host}/0/items/album/{filename}"
            self.answer = io.BytesIO((
                "HTTP/1.1 302 Found\r\nContent-Length: 0\r\n"
                f"Location: {location}\r\n\r\n"
            ).encode())
            return
        start, end = map(int, request.split("Range: bytes=")[1].split("\r\n", 1)[0].split("-"))
        body = self.files[filename][start:end + 1]
        status = "200 OK" if self.mode == "no_range" else "206 Partial Content"
        if self.mode == "wrong_hash":
            body = b"X" + body[1:]
        if self.mode == "truncated":
            body = body[:-1]
        range_start = start + 1 if self.mode == "bad_range" else start
        headers = (f"HTTP/1.1 {status}\r\n"
                   f"Content-Range: bytes {range_start}-{end}/{len(self.files[filename])}\r\n"
                   f"Content-Length: {end - start + 1}\r\n\r\n")
        self.answer = io.BytesIO(headers.encode() + body)

    def makefile(self, mode):
        assert mode == "rb"
        return self.answer

    def close(self):
        pass


def _fake_network(monkeypatch, files, *, mode="ok", redirect_host="dn123456.us.archive.org"):
    requests = []
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, port, **kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
    ])
    monkeypatch.setattr(SafeWebSeedFetcher, "_connect", staticmethod(
        lambda host, ip, timeout, timings=None: _Socket(host, files, requests, mode=mode,
                                           redirect_host=redirect_host)))
    return requests


def test_cross_file_piece_and_redirects_are_verified(tmp_path, monkeypatch):
    path = tmp_path / "album.torrent"
    info_hash, (first, second) = _torrent(path)
    requests = _fake_network(monkeypatch, {"one.mp3": first, "two.mp3": second})
    fetcher = SafeWebSeedFetcher(path, approved_info_hashes=frozenset({info_hash}),
                                 archive_item="album")
    piece = fetcher.fetch_piece(0)
    assert piece == (first + second)[:16384]
    assert len(requests) == 4
    assert "Range: bytes=0-9999" in requests[0][1]
    assert "Range: bytes=0-6383" in requests[2][1]
    d = fetcher.diagnostics()
    assert (d.allowed_web_seeds, d.rejected_web_seeds, d.connections) == (1, 2, 4)
    assert (d.received_bytes, d.verified_pieces, d.http_errors) == (16384, 1, 0)
    assert d.first_piece_seconds is not None
    assert d.redirects == ("dn123456.us.archive.org",) * 2


def test_two_pieces_share_one_range_per_file(tmp_path, monkeypatch):
    path = tmp_path / "album.torrent"
    info_hash, (first, second) = _torrent(path)
    requests = _fake_network(monkeypatch, {"one.mp3": first, "two.mp3": second})
    fetcher = SafeWebSeedFetcher(path, approved_info_hashes=frozenset({info_hash}),
                                 archive_item="album")
    try:
        assert fetcher.fetch_pieces(0, 2) == ((first + second)[:16384], (first + second)[16384:])
        assert len(requests) == 4
        assert "Range: bytes=0-10239" in requests[2][1]
        assert fetcher.diagnostics().verified_pieces == 2
    finally:
        fetcher.close()


def test_checked_connection_reuse_and_fresh_dns_after_rotation(tmp_path, monkeypatch):
    path = tmp_path / "album.torrent"
    info_hash, (first, second) = _torrent(path)
    requests = _fake_network(monkeypatch, {"one.mp3": first, "two.mp3": second})
    dns = []
    original = socket.getaddrinfo

    def resolve(host, port, **kwargs):
        dns.append(host)
        return original(host, port, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    fetcher = SafeWebSeedFetcher(path, approved_info_hashes=frozenset({info_hash}),
                                 archive_item="album")
    try:
        fetcher.fetch_piece(0)
        before = len(dns)
        for _ in range(3):
            fetcher.fetch_piece(1)
        assert len(dns) == before
        assert fetcher.diagnostics().reused_connections >= 3
        fetcher.fetch_piece(1)
        assert len(dns) == before + 1
        assert requests[-1][0] == "dn123456.us.archive.org"
    finally:
        fetcher.close()


def test_rebound_dns_is_rejected_after_keepalive_rotation(tmp_path, monkeypatch):
    path = tmp_path / "album.torrent"
    info_hash, (first, second) = _torrent(path)
    requests = _fake_network(monkeypatch, {"one.mp3": first, "two.mp3": second})
    fetcher = SafeWebSeedFetcher(path, approved_info_hashes=frozenset({info_hash}),
                                 archive_item="album")
    try:
        fetcher.fetch_piece(0)
        for _ in range(3):
            fetcher.fetch_piece(1)
        before = len(requests)
        monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 443)),
        ])
        with pytest.raises(UnsafeWebSeedError, match="non-global"):
            fetcher.fetch_piece(1)
        assert len(requests) == before
    finally:
        fetcher.close()


@pytest.mark.parametrize("mode,error", [
    ("no_range", WebSeedHTTPError), ("bad_range", WebSeedHTTPError),
    ("truncated", WebSeedHTTPError), ("wrong_hash", PieceHashMismatchError),
])
def test_invalid_responses_never_return_piece(tmp_path, monkeypatch, mode, error):
    path = tmp_path / "album.torrent"
    info_hash, (first, second) = _torrent(path)
    _fake_network(monkeypatch, {"one.mp3": first, "two.mp3": second}, mode=mode)
    fetcher = SafeWebSeedFetcher(path, approved_info_hashes=frozenset({info_hash}),
                                 archive_item="album")
    with pytest.raises(error):
        fetcher.fetch_piece(0)
    assert fetcher.diagnostics().verified_pieces == 0


def test_rejects_unapproved_hash_and_unsafe_only_seed(tmp_path):
    path = tmp_path / "album.torrent"
    info_hash, _ = _torrent(path)
    with pytest.raises(ValueError, match="approved"):
        SafeWebSeedFetcher(path, approved_info_hashes=frozenset({"0" * 40}), archive_item="album")
    path2 = tmp_path / "unsafe.torrent"
    other_hash, _ = _torrent(path2, seeds=[b"http://archive.org/download/"])
    with pytest.raises(UnsafeWebSeedError, match="approved HTTPS"):
        SafeWebSeedFetcher(path2, approved_info_hashes=frozenset({other_hash}), archive_item="album")
    private = tmp_path / "private.torrent"
    private_hash, _ = _torrent(private, private=True)
    with pytest.raises(ValueError, match="public"):
        SafeWebSeedFetcher(private, approved_info_hashes=frozenset({private_hash}), archive_item="album")


def test_rejects_private_dns_and_redirect_host_before_connect(tmp_path, monkeypatch):
    path = tmp_path / "album.torrent"
    info_hash, (first, second) = _torrent(path)
    fetcher = SafeWebSeedFetcher(path, approved_info_hashes=frozenset({info_hash}),
                                 archive_item="album")
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
    ])
    monkeypatch.setattr(SafeWebSeedFetcher, "_connect", staticmethod(
        lambda *args: pytest.fail("Connected after unsafe DNS answer")))
    with pytest.raises(UnsafeWebSeedError, match="non-global"):
        fetcher.fetch_piece(0)

    requests = _fake_network(monkeypatch, {"one.mp3": first, "two.mp3": second},
                             redirect_host="internal.example")
    with pytest.raises(UnsafeWebSeedError, match="host"):
        fetcher.fetch_piece(0)
    assert len(requests) == 1  # unsafe redirect never contacted


def test_rechecks_dns_after_redirect(tmp_path, monkeypatch):
    path = tmp_path / "album.torrent"
    info_hash, (first, second) = _torrent(path)
    requests = _fake_network(monkeypatch, {"one.mp3": first, "two.mp3": second})
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, port, **kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "",
         ("10.0.0.1" if host.startswith("dn") else "93.184.216.34", 443)),
    ])
    fetcher = SafeWebSeedFetcher(path, approved_info_hashes=frozenset({info_hash}),
                                 archive_item="album")
    with pytest.raises(UnsafeWebSeedError, match="non-global"):
        fetcher.fetch_piece(0)
    assert len(requests) == 1
    assert fetcher.diagnostics().received_bytes == 0


def test_direct_connection_pins_ip_and_tls_hostname(monkeypatch):
    calls = []

    class Raw:
        def getpeername(self):
            return "93.184.216.34", 443

        def close(self):
            pass

    class Context:
        def wrap_socket(self, raw, *, server_hostname):
            calls.append(("sni", server_hostname))
            return raw

    monkeypatch.setattr(socket, "create_connection", lambda target, timeout: (
        calls.append(("connect", target)) or Raw()))
    monkeypatch.setattr("musicstream.torrent.safe_webseed.ssl.create_default_context", Context)
    SafeWebSeedFetcher._connect("archive.org", "93.184.216.34", 5)
    assert calls == [("connect", ("93.184.216.34", 443)), ("sni", "archive.org")]


def test_direct_connection_rejects_wrong_peer_ip(monkeypatch):
    class Raw:
        def getpeername(self):
            return "10.0.0.1", 443

        def close(self):
            pass

    monkeypatch.setattr(socket, "create_connection", lambda *args, **kwargs: Raw())
    monkeypatch.setattr("musicstream.torrent.safe_webseed.ssl.create_default_context",
                        lambda: pytest.fail("TLS started for wrong peer IP"))
    with pytest.raises(UnsafeWebSeedError, match="unexpected IP"):
        SafeWebSeedFetcher._connect("archive.org", "93.184.216.34", 5)
