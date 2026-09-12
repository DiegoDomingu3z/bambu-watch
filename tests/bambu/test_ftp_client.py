import io
import zipfile

import pytest

from app.bambu.ftp_client import BambuFtpClient, candidate_names
from app.bambu.slice_info import SLICE_INFO_MEMBER
from app.config import Settings


def make_settings(**over) -> Settings:
    base = dict(bambu_host="h", bambu_serial="s", bambu_access_code="c",
                anthropic_api_key="k", discord_webhook_url="u")
    base.update(over)
    return Settings(**base)


SLICE_XML = (b'<config><plate><filament id="1" type="PLA" color="#FF0000" '
             b'used_m="113.2" used_g="340.0"/></plate></config>')


def make_3mf() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(SLICE_INFO_MEMBER, SLICE_XML)
    return buf.getvalue()


def test_candidate_names_covers_common_spellings():
    names = candidate_names("Mask_Final_v17")
    assert "Mask_Final_v17.3mf" in names
    assert "Mask_Final_v17.gcode.3mf" in names


def test_candidate_names_does_not_double_the_extension():
    names = candidate_names("Mask_Final_v17.3mf")
    assert "Mask_Final_v17.3mf" in names
    assert "Mask_Final_v17.3mf.3mf" not in names


def test_candidate_names_strips_any_directory():
    names = candidate_names("Metadata/plate_1.gcode")
    assert all("/" not in n for n in names), "only a basename can be fetched"
    assert "plate_1.3mf" in names


def test_candidate_names_refuses_traversal():
    for hostile in ("../../etc/passwd", "/", "   ", ""):
        assert all(".." not in n for n in candidate_names(hostile))


def test_candidate_names_are_unique():
    names = candidate_names("thing.3mf")
    assert len(names) == len(set(names))


def test_client_exposes_no_write_methods():
    client = BambuFtpClient(make_settings())
    for attr in dir(client):
        assert not attr.startswith(("store", "upload", "delete", "rename", "mkdir"))


async def test_fetch_returns_slice_info(monkeypatch):
    client = BambuFtpClient(make_settings())
    monkeypatch.setattr(client, "_fetch_archive", lambda name: make_3mf())
    info = await client.fetch_slice_info("Mask_Final_v17")
    assert info is not None
    assert info.total_grams == pytest.approx(340.0)
    assert info.filaments[0].color == "#FF0000"


async def test_fetch_returns_none_when_archive_missing(monkeypatch):
    client = BambuFtpClient(make_settings())
    monkeypatch.setattr(client, "_fetch_archive", lambda name: None)
    assert await client.fetch_slice_info("x") is None


async def test_fetch_returns_none_on_transport_error(monkeypatch):
    client = BambuFtpClient(make_settings())

    def boom(name):
        raise OSError("connection refused")

    monkeypatch.setattr(client, "_fetch_archive", boom)
    assert await client.fetch_slice_info("x") is None, (
        "an FTPS failure must never propagate into the monitor"
    )


async def test_fetch_returns_none_on_corrupt_archive(monkeypatch):
    client = BambuFtpClient(make_settings())
    monkeypatch.setattr(client, "_fetch_archive", lambda name: b"not a zip")
    assert await client.fetch_slice_info("x") is None


async def test_fetch_rejects_empty_file_name():
    client = BambuFtpClient(make_settings())
    assert await client.fetch_slice_info("") is None
    assert await client.fetch_slice_info("   ") is None


async def test_fetch_honours_the_master_switch(monkeypatch):
    client = BambuFtpClient(make_settings(enable_slice_fetch=False))
    called = []
    monkeypatch.setattr(client, "_fetch_archive", lambda n: called.append(n))
    assert await client.fetch_slice_info("x") is None
    assert called == [], "no connection may be opened when disabled"


class FakeFtp:
    def __init__(self, payloads=(b"x",), fail=False):
        self.payloads = payloads
        self.fail = fail
        self.issued = []

    def retrbinary(self, cmd, callback, blocksize=8192):
        self.issued.append(cmd)
        if self.fail:
            raise OSError("550 not found")
        for chunk in self.payloads:
            callback(chunk)


def test_size_cap_aborts_a_large_transfer():
    client = BambuFtpClient(make_settings(ftp_max_fetch_bytes=10))
    ftp = FakeFtp(payloads=[b"xxxx"] * 5)
    assert client._retrieve(ftp, "/x.3mf") is None


def test_retrieve_returns_bytes_under_the_cap():
    client = BambuFtpClient(make_settings())
    assert client._retrieve(FakeFtp([b"hello", b"world"]), "/x.3mf") == b"helloworld"


def test_retrieve_issues_only_a_retr_command():
    client = BambuFtpClient(make_settings())
    ftp = FakeFtp()
    client._retrieve(ftp, "/x.3mf")
    assert ftp.issued == ["RETR /x.3mf"]
    assert all(c.startswith("RETR ") for c in ftp.issued), (
        "no command other than RETR may ever be issued"
    )


def test_retrieve_returns_none_when_file_absent():
    client = BambuFtpClient(make_settings())
    assert client._retrieve(FakeFtp(fail=True), "/missing.3mf") is None


def test_retrieve_treats_empty_file_as_absent():
    client = BambuFtpClient(make_settings())
    assert client._retrieve(FakeFtp(payloads=[]), "/x.3mf") is None


def test_fetch_archive_searches_every_dir_and_name(monkeypatch):
    client = BambuFtpClient(make_settings(ftp_search_dirs="/,/cache"))
    tried = []
    monkeypatch.setattr(client, "_connect", lambda: object())
    monkeypatch.setattr(client, "_retrieve",
                        lambda ftp, path: tried.append(path) or None)
    assert client._fetch_archive("thing") is None
    assert tried == ["/thing.3mf", "/thing.gcode.3mf",
                     "/cache/thing.3mf", "/cache/thing.gcode.3mf"]


def test_fetch_archive_stops_at_the_first_hit(monkeypatch):
    client = BambuFtpClient(make_settings())
    tried = []

    def retrieve(ftp, path):
        tried.append(path)
        return b"data" if path == "/thing.3mf" else None

    monkeypatch.setattr(client, "_connect", lambda: object())
    monkeypatch.setattr(client, "_retrieve", retrieve)
    assert client._fetch_archive("thing") == b"data"
    assert tried == ["/thing.3mf"], "must not keep searching after a hit"


def test_fetch_archive_returns_none_when_connect_fails(monkeypatch):
    client = BambuFtpClient(make_settings())

    def boom():
        raise OSError("refused")

    monkeypatch.setattr(client, "_connect", boom)
    assert client._fetch_archive("thing") is None
