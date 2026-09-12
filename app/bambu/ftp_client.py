"""Read-only FTPS access to the printer's SD card.

RETRIEVAL ONLY. No write verb is wrapped anywhere in this module, and
tests/test_printer_readonly.py scans the source to keep it that way. The risk
being managed is not accidental writes but SD card I/O contention, because the
printer streams gcode from the same card this serves. The CALLER is
responsible for never invoking this while the printer is RUNNING.

Bambu printers serve FTPS on 990 with implicit TLS. ftplib.FTP_TLS does
explicit TLS by default, hence the socket-wrapping subclass. The certificate
is self-signed, so verification is disabled, as with MQTT and the camera.
"""

from __future__ import annotations

import asyncio
import contextlib
import ftplib
import logging
import ssl
from pathlib import Path

from app.bambu.slice_info import SliceInfo, extract_slice_info
from app.config import Settings

logger = logging.getLogger(__name__)


class ImplicitFtpTls(ftplib.FTP_TLS):
    """FTP_TLS that negotiates TLS on connect rather than after AUTH."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._sock = None

    @property
    def sock(self):
        return self._sock

    @sock.setter
    def sock(self, value):
        if value is not None and not isinstance(value, ssl.SSLSocket):
            value = self.context.wrap_socket(value)
        self._sock = value


def candidate_names(file_name: str) -> list[str]:
    """Plausible archive names for a print. MQTT reports either a friendly
    subtask_name with no extension or a path like Metadata/plate_1.gcode, so
    try the obvious spellings. Basenames only: no traversal."""
    base = Path(file_name.strip()).name
    if not base:
        return []

    stem = base
    for suffix in (".3mf", ".gcode"):
        if stem.lower().endswith(suffix):
            stem = stem[: -len(suffix)]

    names = [base] if base.lower().endswith(".3mf") else []
    names += [f"{stem}.3mf", f"{stem}.gcode.3mf"]

    seen: set[str] = set()
    return [n for n in names if not (n in seen or seen.add(n))]


class BambuFtpClient:
    def __init__(self, settings: Settings):
        self.settings = settings

    async def fetch_slice_info(self, file_name: str) -> SliceInfo | None:
        """Fetch and parse slice info. Returns None on any failure.

        MUST NOT be called while the printer reports RUNNING.
        """
        if not self.settings.enable_slice_fetch:
            return None
        if not file_name or not file_name.strip():
            return None

        loop = asyncio.get_running_loop()
        try:
            archive = await loop.run_in_executor(None, self._fetch_archive, file_name)
        except Exception as exc:
            logger.warning("slice info fetch failed: %s", exc)
            return None

        if not archive:
            return None
        return extract_slice_info(archive)

    # --- blocking, runs in an executor ---

    def _fetch_archive(self, file_name: str) -> bytes | None:
        names = candidate_names(file_name)
        if not names:
            return None

        try:
            ftp = self._connect()
        except Exception as exc:
            logger.warning("FTPS connect failed: %s", exc)
            return None

        try:
            for directory in self.settings.ftp_dirs:
                for name in names:
                    path = f"{directory.rstrip('/')}/{name}"
                    data = self._retrieve(ftp, path)
                    if data:
                        logger.info("fetched slice archive from %s", path)
                        return data
            logger.info("no sliced archive found for %r", file_name)
            return None
        finally:
            try:
                ftp.quit()
            except Exception:
                with contextlib.suppress(Exception):
                    ftp.close()

    def _connect(self) -> ImplicitFtpTls:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE

        ftp = ImplicitFtpTls(context=context)
        ftp.timeout = self.settings.ftp_timeout_seconds
        ftp.connect(
            host=self.settings.bambu_host,
            port=self.settings.bambu_ftp_port,
            timeout=self.settings.ftp_timeout_seconds,
        )
        ftp.login(user="bblp", passwd=self.settings.bambu_access_code)
        ftp.prot_p()
        return ftp

    def _retrieve(self, ftp, path: str) -> bytes | None:
        """RETR one file, aborting past the size cap. Returns None if the file
        is absent or too large."""
        chunks: list[bytes] = []
        total = 0
        cap = self.settings.ftp_max_fetch_bytes
        overflow = False

        def collect(chunk: bytes) -> None:
            nonlocal total, overflow
            if overflow:
                return
            total += len(chunk)
            if total > cap:
                overflow = True
                chunks.clear()
                return
            chunks.append(chunk)

        try:
            ftp.retrbinary(f"RETR {path}", collect)
        except (TimeoutError, OSError, *ftplib.all_errors) as exc:
            logger.debug("RETR %s failed: %s", path, exc)
            return None

        if overflow:
            logger.warning("aborted %s: larger than %d bytes", path, cap)
            return None
        return b"".join(chunks) or None
