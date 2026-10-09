"""Open MRF files: detect the format, decompress, and repair broken text.

Hospital files arrive as plain, gzip or zip, sometimes with a wrong or missing
extension, sometimes in UTF-16, often with stray Windows-1252 bytes, and the
JSON ones regularly carry double or trailing commas. Everything here streams:
memory stays flat no matter how big the file is.
"""

from __future__ import annotations

import codecs
import copy
import gzip
import io
import logging
import zipfile
import zlib
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, Iterator, List, Optional, TextIO, Tuple

import inflate64

log = logging.getLogger(__name__)

# Zip method 9. The stdlib zipfile cannot read it, and Windows uses it for
# large archives, so real MRFs show up compressed this way.
ZIP_DEFLATE64 = 9

_UTF8_BOM = b'\xef\xbb\xbf'
_UTF16_BOMS = (b'\xff\xfe', b'\xfe\xff')
_SNIFF_BYTES = 4096


# ---------------------------------------------------------------------------
# Format detection
# ---------------------------------------------------------------------------

def detect_file_format(path: Path) -> Tuple[str, Optional[str]]:
    """Return ``(file_format, compression)`` for *path*.

    *file_format* is ``'csv'`` or ``'json'``; *compression* is ``'gz'``,
    ``'zip'`` or ``None``.

    The extension decides first. Plain ``.csv`` and ``.json`` names are
    checked against the content, because misnamed files are common. Anything
    else (API downloads often have no extension) is sniffed.

    Raises ``ValueError`` for Excel files and ambiguous zips, and
    ``zipfile.BadZipFile`` for corrupt zips.
    """
    path = Path(path)
    name = path.name.lower()

    if looks_like_html(path):
        raise ValueError(
            f"{path.name} is an HTML page, not an MRF: the download probably hit "
            f"an error page or a bot challenge.")
    if name.endswith(('.xlsx', '.xls')):
        raise ValueError(
            f"Excel/XLSX files are not supported: {path.name}. "
            f"Please provide the CSV or JSON version of this MRF."
        )
    if name.endswith('.zip'):
        _reject_if_xlsx(path)
        return detect_format_from_zip(path), 'zip'
    if name.endswith('.csv.gz'):
        return 'csv', 'gz'
    if name.endswith('.json.gz'):
        return 'json', 'gz'
    if name.endswith(('.csv', '.json')):
        ext_format = 'csv' if name.endswith('.csv') else 'json'
        try:
            content_format, content_compression = _detect_format_from_content(path)
        except ValueError:
            # Empty file: nothing to contradict the extension.
            return ext_format, None
        if content_compression is not None:
            # Compressed despite a plain extension: trust the content.
            return content_format, content_compression
        if content_format != ext_format:
            log.warning(
                "Extension says %s but content looks like %s, trusting content: %s",
                ext_format, content_format, path.name,
            )
            return content_format, None
        return ext_format, None
    return _detect_format_from_content(path)


# Signs of an HTML error page saved under an MRF name: ASP.NET download
# stubs, CDN 403/404 pages, Cloudflare challenges.
_HTML_MARKERS = (b"<!doctype html", b"<html", b"<head", b"attention required", b"__viewstate")


def looks_like_html(path: Path) -> bool:
    """True when the first 512 bytes of *path* look like an HTML page.

    Compressed files and anything starting with ``{`` or ``[`` never match,
    so a real MRF is not flagged.
    """
    try:
        with open(path, 'rb') as fh:
            head = fh.read(512)
    except OSError:
        return False
    if not head or head[:2] in (b'\x1f\x8b', b'PK'):
        return False
    stripped = head.removeprefix(_UTF8_BOM).lstrip()
    if stripped.startswith((b'{', b'[')):
        return False
    lowered = stripped.lower()
    return any(marker in lowered for marker in _HTML_MARKERS)


def _detect_format_from_content(path: Path) -> Tuple[str, Optional[str]]:
    """Detect the format from the first bytes of *path*.

    Gzip and zip are recognized by magic bytes. Text whose first
    non-whitespace byte is ``{`` or ``[`` is JSON, anything else is CSV.
    UTF-16 text is transcoded before sniffing. Raises ``ValueError`` for an
    empty file.
    """
    with open(path, 'rb') as f:
        head = f.read(_SNIFF_BYTES)

    if not head:
        raise ValueError(f"Cannot detect format: file is empty: {path.name}")

    if head[:4] == b'PK\x03\x04':
        _reject_if_xlsx(path)
        return detect_format_from_zip(path), 'zip'

    if head[:2] == b'\x1f\x8b':
        with gzip.open(path, 'rb') as gz:
            inner_head = gz.read(_SNIFF_BYTES)
        return _sniff_text(_transcode_to_utf8(inner_head)), 'gz'

    return _sniff_text(_transcode_to_utf8(head)), None


def _sniff_text(head: bytes) -> str:
    stripped = head.lstrip(b' \t\r\n' + _UTF8_BOM)
    return 'json' if stripped[:1] in (b'{', b'[') else 'csv'


def validate_zip_integrity(zip_path: Path) -> None:
    """Raise ``zipfile.BadZipFile`` if the zip is corrupt or truncated.

    Reads every data member in full and checks its CRC, so a damaged archive
    fails here with a clear error instead of halfway through a parse.
    """
    try:
        with zipfile.ZipFile(zip_path, 'r') as zf:
            for name in _zip_data_members(zf):
                with _open_zip_member(zf, name) as member:
                    while member.read(1 << 20):
                        pass
    except (zlib.error, EOFError) as exc:
        raise zipfile.BadZipFile(
            f"ZIP decompression error ({Path(zip_path).name}): {exc}") from exc


def _reject_if_xlsx(zip_path: Path) -> None:
    """Raise ``ValueError`` if the zip is really an XLSX workbook."""
    try:
        with zipfile.ZipFile(zip_path, 'r') as zf:
            lower_names = [n.lower() for n in zf.namelist()]
    except zipfile.BadZipFile:
        return  # Not a valid zip: let the caller report that.
    if '[content_types].xml' in lower_names or any(n.startswith('xl/') for n in lower_names):
        raise ValueError(
            f"Excel/XLSX files are not supported: {Path(zip_path).name}. "
            f"Please provide the CSV or JSON version of this MRF."
        )


def _zip_data_members(zf: zipfile.ZipFile) -> List[str]:
    """Return the real data members, skipping directories and macOS sidecars.

    Finder-made zips add ``__MACOSX/._name`` resource forks and sometimes a
    ``.DS_Store``. Counting those would make a one-file archive look like
    three.
    """
    members = []
    for info in zf.infolist():
        if info.is_dir():
            continue
        base = info.filename.rsplit('/', 1)[-1]
        if info.filename.startswith('__MACOSX/') or base == '.DS_Store' or base.startswith('._'):
            continue
        members.append(info.filename)
    return members


def _zip_single_member(zf: zipfile.ZipFile, zip_name: str = "") -> str:
    """Return the one data member of *zf*, or raise ``ValueError``."""
    members = _zip_data_members(zf)
    if len(members) != 1:
        suffix = f" in {zip_name}" if zip_name else ""
        detail = f": {members}" if members else ""
        raise ValueError(
            f"ZIP must contain exactly one file, found {len(members)}{suffix}{detail}")
    return members[0]


def detect_format_from_zip(zip_path: Path) -> str:
    """Return ``'csv'`` or ``'json'`` for the single data file inside a zip.

    Uses the inner extension when it is ``.csv`` or ``.json`` and sniffs the
    content otherwise. Rejects nested gzip or zip. Validates the archive first.
    """
    validate_zip_integrity(zip_path)

    with zipfile.ZipFile(zip_path, 'r') as zf:
        inner = _zip_single_member(zf, Path(zip_path).name)
        inner_name = inner.lower()
        if inner_name.endswith('.csv'):
            return 'csv'
        if inner_name.endswith('.json'):
            return 'json'
        with _open_zip_member(zf, inner) as f:
            head = f.read(_SNIFF_BYTES)
    if not head:
        raise ValueError(f"ZIP inner file is empty: {inner_name}")
    if head[:2] == b'\x1f\x8b':
        raise ValueError(f"ZIP inner file appears to be gzip-compressed: {inner_name}")
    if head[:4] == b'PK\x03\x04':
        raise ValueError(f"ZIP inner file appears to be another ZIP archive: {inner_name}")
    return _sniff_text(_transcode_to_utf8(head))


# ---------------------------------------------------------------------------
# Opening files
# ---------------------------------------------------------------------------

@contextmanager
def open_binary(path: Path, compression: Optional[str]) -> Iterator[BinaryIO]:
    """Yield the decompressed bytes of *path* as a readable stream."""
    path = Path(path)
    if compression == 'gz':
        with gzip.open(path, 'rb') as fh:
            yield fh
    elif compression == 'zip':
        with zipfile.ZipFile(path, 'r') as zf:
            with _open_zip_member(zf, _zip_single_member(zf, path.name)) as fh:
                yield fh
    else:
        with open(path, 'rb') as fh:
            yield fh


@contextmanager
def open_text(path: Path, compression: Optional[str]) -> Iterator[TextIO]:
    """Yield the decompressed content of *path* as text.

    UTF-16 (with or without a BOM) is detected and decoded. Everything else
    is read as UTF-8, with undecodable bytes replaced by U+FFFD. A BOM, when
    present, stays in the text as U+FEFF; header normalization removes it.
    """
    with open_binary(path, compression) as raw:
        buffered = raw if hasattr(raw, 'peek') else io.BufferedReader(raw)
        encoding = _detect_utf16_encoding(buffered.peek(32)[:32]) or 'utf-8'
        text = io.TextIOWrapper(buffered, encoding=encoding, errors='replace')
        try:
            yield text
        finally:
            if not text.closed:
                text.detach()  # leave closing to open_binary


@contextmanager
def open_json(path: Path, compression: Optional[str],
              sanitize: bool = False) -> Iterator[BinaryIO]:
    """Yield the decompressed content of *path* as UTF-8 bytes for ijson.

    Encoding problems are always repaired: BOMs are stripped, UTF-16 is
    transcoded and invalid UTF-8 becomes U+FFFD.

    With ``sanitize=True`` the stream also drops control characters and fixes
    double and trailing commas. That pass runs in pure Python, byte by byte,
    so parse with ``sanitize=False`` first and retry with ``True`` only when
    ijson reports a syntax error.
    """
    with open_binary(path, compression) as raw:
        reader = Utf8SanitizingReader(raw)
        yield JsonSanitizingReader(reader) if sanitize else reader


# ---------------------------------------------------------------------------
# Deflate64 zip members
# ---------------------------------------------------------------------------

def _open_zip_member(zf: zipfile.ZipFile, name: str) -> BinaryIO:
    """Open a zip member for reading, including Deflate64 members."""
    info = zf.getinfo(name)
    if info.compress_type != ZIP_DEFLATE64:
        return zf.open(info)
    # Open the member as STORED so zipfile hands back the compressed bytes
    # untouched, then inflate them ourselves. ZipInfo has __slots__, so copy.
    raw_info = copy.copy(info)
    raw_info.compress_type = zipfile.ZIP_STORED
    raw_info.file_size = info.compress_size
    raw_info.CRC = None  # skip zipfile's check, _Deflate64Reader does its own
    return io.BufferedReader(_Deflate64Reader(zf.open(raw_info), info))


class _Deflate64Reader(io.RawIOBase):
    """Inflate a Deflate64 stream and verify its CRC and size at the end."""

    def __init__(self, raw: BinaryIO, info: zipfile.ZipInfo, chunk_size: int = 1 << 16):
        self._raw = raw
        self._info = info
        self._chunk_size = chunk_size
        self._inflater = inflate64.Inflater()
        self._out = b''
        self._pos = 0
        self._crc = 0
        self._size = 0
        self._eof = False

    def readable(self) -> bool:
        return True

    def readinto(self, b) -> int:
        while self._pos >= len(self._out):
            if self._eof:
                return 0
            chunk = self._raw.read(self._chunk_size)
            if chunk:
                try:
                    self._out, self._pos = self._inflater.inflate(chunk), 0
                except ValueError as exc:
                    # inflate64 reports bad data as ValueError; callers treat
                    # ValueError as "unsupported file", so name it properly.
                    raise zipfile.BadZipFile(
                        f"Corrupt Deflate64 data in {self._info.filename}: {exc}") from exc
            else:
                self._eof = True
                self._verify()
        n = min(len(b), len(self._out) - self._pos)
        data = self._out[self._pos:self._pos + n]
        b[:n] = data
        self._pos += n
        self._crc = zlib.crc32(data, self._crc)
        self._size += n
        return n

    def _verify(self) -> None:
        if self._size != self._info.file_size or self._crc != self._info.CRC:
            raise zipfile.BadZipFile(
                f"Bad CRC-32 or size for Deflate64 member {self._info.filename}")

    def close(self) -> None:
        if not self.closed:
            self._raw.close()
        super().close()


# ---------------------------------------------------------------------------
# Encoding repair
# ---------------------------------------------------------------------------

def _detect_utf16_encoding(probe: bytes) -> Optional[str]:
    """Return ``'utf-16-le'``, ``'utf-16-be'`` or ``None`` for *probe*.

    A BOM decides when present. Without one, four or more NUL bytes in the
    first 32 bytes mean UTF-16, and which positions hold the NULs give the
    byte order.
    """
    if len(probe) < 2:
        return None
    if probe[:2] == b'\xff\xfe':
        return 'utf-16-le'
    if probe[:2] == b'\xfe\xff':
        return 'utf-16-be'
    snippet = probe[:32]
    if snippet.count(b'\x00') >= 4:
        le_score = sum(1 for i in range(1, len(snippet), 2) if snippet[i] == 0)
        be_score = sum(1 for i in range(0, len(snippet), 2) if snippet[i] == 0)
        if le_score > be_score:
            return 'utf-16-le'
        if be_score > le_score:
            return 'utf-16-be'
    return None


def _transcode_to_utf8(data: bytes) -> bytes:
    """Return *data* as UTF-8 if it is UTF-16, unchanged otherwise. Drops a UTF-16 BOM."""
    enc = _detect_utf16_encoding(data)
    if enc is None:
        return data
    if data[:2] in _UTF16_BOMS:
        data = data[2:]
    return data.decode(enc, errors='replace').encode('utf-8')


class Utf8SanitizingReader:
    """Wrap a binary stream so everything read from it is valid UTF-8.

    Strips a leading UTF-8 BOM, transcodes UTF-16 (detected on the first
    read) and replaces invalid bytes, such as stray Windows-1252, with
    U+FFFD. An incremental decoder carries multi-byte sequences split across
    chunks. ``read(n)`` returns at most *n* bytes even when a replacement
    grows one input byte into three.
    """

    def __init__(self, fh: BinaryIO, chunk_size: int = 256 * 1024):
        self._fh = fh
        self._chunk_size = chunk_size
        self._decoder = codecs.getincrementaldecoder('utf-8')('replace')
        self._buf = b''
        self._eof = False
        self._first_read = True

    def _strip_bom(self, raw: bytes) -> bytes:
        if not self._first_read:
            return raw
        self._first_read = False
        if raw[:3] == _UTF8_BOM:
            raw = raw[3:]
        enc = _detect_utf16_encoding(raw)
        if enc is not None:
            self._decoder = codecs.getincrementaldecoder(enc)('replace')
            if raw[:2] in _UTF16_BOMS:
                raw = raw[2:]
        return raw

    def _pull(self) -> None:
        """Decode one more chunk into the buffer, or mark EOF."""
        raw = self._fh.read(self._chunk_size)
        if raw:
            raw = self._strip_bom(raw)  # may swap in a UTF-16 decoder, so call it first
            text = self._decoder.decode(raw, final=False)
        else:
            self._eof = True
            text = self._decoder.decode(b'', final=True)
        if text:
            self._buf += text.encode('utf-8')

    def read(self, n: int = -1) -> bytes:
        if n is None or n < 0:
            while not self._eof:
                self._pull()
            result, self._buf = self._buf, b''
            return result
        while len(self._buf) < n and not self._eof:
            self._pull()
        result, self._buf = self._buf[:n], self._buf[n:]
        return result

    def readline(self) -> bytes:
        """Return bytes up to and including the next newline, or to EOF."""
        while True:
            nl = self._buf.find(b'\n')
            if nl != -1:
                line, self._buf = self._buf[:nl + 1], self._buf[nl + 1:]
                return line
            if self._eof:
                line, self._buf = self._buf, b''
                return line
            self._pull()

    def __iter__(self):
        return self

    def __next__(self) -> bytes:
        line = self.readline()
        if not line:
            raise StopIteration
        return line

    def seekable(self) -> bool:
        return False

    def readable(self) -> bool:
        return True

    def close(self) -> None:
        self._fh.close()


# Bytes JsonSanitizingReader treats as control characters and whitespace.
_CTRL_CHAR_BYTES = frozenset(range(0x00, 0x09)) | frozenset(range(0x0E, 0x20)) | {0x0B, 0x0C}
_WS_BYTES = frozenset(b' \t\r\n')


class JsonSanitizingReader:
    """Fix common JSON damage in a stream, without loading the whole file.

    Applied to a UTF-8 stream (normally a :class:`Utf8SanitizingReader`):

    1. strip control characters ``[\\x00-\\x08\\x0b\\x0c\\x0e-\\x1f]``
    2. collapse double commas ``,,`` into one
    3. drop trailing commas before ``]`` or ``}``

    Comma fixes only apply outside string literals. Quote and escape state is
    carried across chunks, and a comma whose meaning depends on the next
    chunk is held back until that chunk arrives. Memory stays at about two
    chunks when the caller reads in bounded sizes, as ijson does.
    """

    def __init__(self, fh, chunk_size: int = 256 * 1024):
        self._fh = fh
        self._chunk_size = chunk_size
        self._out_buf = b''
        self._eof = False
        self._in_string = False
        self._escaped = False
        # A comma (plus any whitespace) at the end of a chunk, waiting for
        # lookahead from the next one.
        self._pending = b''

    def _process_chunk(self, raw: bytes) -> bytes:
        """Return the sanitized form of *raw*, holding back an undecided comma."""
        if self._pending:
            raw = self._pending + raw
            self._pending = b''

        out = bytearray()
        i = 0
        n = len(raw)

        while i < n:
            bch = raw[i]

            if self._in_string:
                if bch in _CTRL_CHAR_BYTES:
                    i += 1
                    continue
                out.append(bch)
                if self._escaped:
                    self._escaped = False
                elif bch == 0x5C:   # backslash
                    self._escaped = True
                elif bch == 0x22:   # closing quote
                    self._in_string = False
                i += 1
                continue

            if bch in _CTRL_CHAR_BYTES:
                i += 1
                continue

            if bch == 0x22:  # opening quote
                self._in_string = True
                out.append(bch)
                i += 1
                continue

            if bch == 0x2C:  # comma
                j = i + 1
                while j < n and raw[j] in _WS_BYTES:
                    j += 1

                if j >= n:
                    # Need the next chunk to decide.
                    self._pending = bytes(raw[i:])
                    return bytes(out)

                next_byte = raw[j]

                if next_byte == 0x2C:
                    # Double comma: swallow the run of extra commas.
                    while j < n and raw[j] == 0x2C:
                        j += 1
                        while j < n and raw[j] in _WS_BYTES:
                            j += 1
                    if j >= n:
                        # Still undecided: keep one comma for the next chunk.
                        self._pending = bytes(raw[i:i + 1])
                        i = j
                        continue
                    if raw[j] in (0x5D, 0x7D):  # ] or }
                        i = j  # trailing comma: drop it
                        continue
                    out.append(0x2C)
                    k = i + 1
                    while k < n and raw[k] in _WS_BYTES:
                        out.append(raw[k])
                        k += 1
                    i = j
                    continue

                if next_byte in (0x5D, 0x7D):
                    # Trailing comma: drop it, keep the whitespace.
                    out.extend(raw[i + 1:j])
                    i = j
                    continue

                out.append(bch)
                i += 1
                continue

            out.append(bch)
            i += 1

        return bytes(out)

    def _pull(self) -> None:
        """Sanitize one more chunk into the buffer, or mark EOF."""
        raw = self._fh.read(self._chunk_size)
        if raw:
            self._out_buf += self._process_chunk(raw)
        else:
            # A comma still pending at EOF means the file ends mid-token.
            # Emit it rather than lose data.
            self._eof = True
            self._out_buf += self._pending
            self._pending = b''

    def read(self, n: int = -1) -> bytes:
        if n is None or n < 0:
            while not self._eof:
                self._pull()
            result, self._out_buf = self._out_buf, b''
            return result
        while len(self._out_buf) < n and not self._eof:
            self._pull()
        result, self._out_buf = self._out_buf[:n], self._out_buf[n:]
        return result

    def readline(self) -> bytes:
        """Return bytes up to and including the next newline, or to EOF."""
        while True:
            nl = self._out_buf.find(b'\n')
            if nl != -1:
                line, self._out_buf = self._out_buf[:nl + 1], self._out_buf[nl + 1:]
                return line
            if self._eof:
                line, self._out_buf = self._out_buf, b''
                return line
            self._pull()

    def __iter__(self):
        return self

    def __next__(self) -> bytes:
        line = self.readline()
        if not line:
            raise StopIteration
        return line

    def seekable(self) -> bool:
        return False

    def readable(self) -> bool:
        return True

    def close(self) -> None:
        self._fh.close()
