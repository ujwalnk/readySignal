"""Account image handling: content validation, SVG sanitising and encrypted at-rest storage.

Uploaded bytes are untrusted. Raster images are verified structurally (magic bytes, header dimensions, terminator);
SVG is re-built from a strict allow-list so no script, event handler, external reference or style import survives.
Stored files are encrypted with the same AES-256-GCM envelope as the JSON documents, so the encryption boundary holds.
"""

from __future__ import annotations

import os
import re
import struct
import xml.etree.ElementTree as ET
from collections.abc import Callable
from pathlib import Path

from .crypto import CryptoError, Keyring, decrypt, encrypt
from .store import StoreError, atomic_write

MAX_UPLOAD_BYTES = 512 * 1024
MAX_SVG_BYTES = 256 * 1024
MAX_DIMENSION = 4096
ASSET_ID = re.compile(r"[0-9a-f]{32}")
SVG_NS = "http://www.w3.org/2000/svg"
XLINK_NS = "http://www.w3.org/1999/xlink"
CONTENT_TYPES = ("image/png", "image/jpeg", "image/webp", "image/svg+xml")


class ImageError(ValueError):
    """The upload was rejected. The message is safe to show to the user."""


# ------------------------------------------------------------------ raster
def _png(data: bytes) -> None:
    if len(data) < 33 or data[12:16] != b"IHDR" or data[-12:-8] != b"\x00\x00\x00\x00" or data[-8:-4] != b"IEND":
        raise ImageError("PNG file is malformed or truncated")
    w, h = struct.unpack(">II", data[16:24])
    _dims(w, h)


def _jpeg(data: bytes) -> None:
    if data.rstrip(b"\x00")[-2:] != b"\xff\xd9":
        raise ImageError("JPEG file is malformed or truncated")
    i, n = 2, len(data)
    while i + 9 < n:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7 or marker == 0xFF:
            i += 1 if marker == 0xFF else 2
            continue
        (length,) = struct.unpack(">H", data[i + 2 : i + 4])
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            h, w = struct.unpack(">HH", data[i + 5 : i + 9])
            _dims(w, h)
            return
        i += 2 + length
    raise ImageError("JPEG file has no image header")


def _webp(data: bytes) -> None:
    if len(data) < 30 or struct.unpack("<I", data[4:8])[0] != len(data) - 8:
        raise ImageError("WebP file is malformed or truncated")
    kind = data[12:16]
    if kind == b"VP8 ":
        w, h = struct.unpack("<HH", data[26:30])
        w, h = w & 0x3FFF, h & 0x3FFF
    elif kind == b"VP8L":
        if data[20] != 0x2F:
            raise ImageError("WebP file is malformed")
        bits = struct.unpack("<I", data[21:25])[0]
        w, h = (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    elif kind == b"VP8X":
        w = int.from_bytes(data[24:27], "little") + 1
        h = int.from_bytes(data[27:30], "little") + 1
    else:
        raise ImageError("unsupported WebP variant")
    _dims(w, h)


def _dims(w: int, h: int) -> None:
    if not (1 <= w <= MAX_DIMENSION and 1 <= h <= MAX_DIMENSION):
        raise ImageError(f"image dimensions must be between 1 and {MAX_DIMENSION} pixels")


# --------------------------------------------------------------------- svg
_TAGS = {
    "svg", "g", "path", "circle", "ellipse", "rect", "line", "polyline", "polygon", "defs", "linearGradient",
    "radialGradient", "stop", "clipPath", "mask", "title", "desc", "text", "tspan", "symbol", "use",
}  # fmt: skip
_TEXT_TAGS = {"title", "desc", "text", "tspan"}
_ATTRS = {
    "width", "height", "viewBox", "preserveAspectRatio", "fill", "stroke", "stroke-width", "stroke-linecap",
    "stroke-linejoin", "stroke-miterlimit", "stroke-dasharray", "stroke-dashoffset", "opacity", "fill-opacity",
    "stroke-opacity", "fill-rule", "clip-rule", "clip-path", "mask", "transform", "d", "cx", "cy", "r", "rx", "ry",
    "x", "y", "x1", "y1", "x2", "y2", "points", "id", "offset", "stop-color", "stop-opacity", "gradientUnits",
    "gradientTransform", "fx", "fy", "class", "font-size", "font-family", "font-weight", "text-anchor",
    "dominant-baseline", "clipPathUnits", "maskUnits", "role", "aria-label", "aria-hidden", "href", "style", "version",
}  # fmt: skip
_BAD_VALUE = re.compile(r"javascript:|data:|vbscript:|@import|expression\(|<|&#", re.I)
_URL = re.compile(r"url\(\s*(['\"]?)([^)]*)\1\s*\)", re.I)
MAX_SVG_ELEMENTS, MAX_SVG_DEPTH = 4000, 30


def _safe_value(value: str) -> bool:
    if _BAD_VALUE.search(value):
        return False
    return all(m.group(2).strip().startswith("#") for m in _URL.finditer(value))


def _local(name: str, allow_xlink: bool = False) -> str | None:
    if not name.startswith("{"):
        return name
    ns, _, local = name[1:].partition("}")
    if ns == SVG_NS:
        return local
    if allow_xlink and ns == XLINK_NS and local == "href":
        return "href"
    return None


def sanitize_svg(data: bytes) -> bytes:
    if len(data) > MAX_SVG_BYTES:
        raise ImageError("SVG is too large")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ImageError("SVG must be UTF-8 text") from exc
    if re.search(r"<!\s*(DOCTYPE|ENTITY)", text, re.I):
        raise ImageError("SVG must not contain a DOCTYPE or entity declarations")
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise ImageError("SVG is not well-formed XML") from exc
    if root.tag != f"{{{SVG_NS}}}svg":
        raise ImageError("file is not an SVG image (root element must be <svg> in the SVG namespace)")
    count = 0

    def clean(el: ET.Element, depth: int) -> None:
        nonlocal count
        count += 1
        if count > MAX_SVG_ELEMENTS or depth > MAX_SVG_DEPTH:
            raise ImageError("SVG is too complex")
        tag = _local(el.tag)
        el.tag = tag or ""
        attrs: dict[str, str] = {}
        for k, v in el.attrib.items():
            name = _local(k, allow_xlink=True)
            if name is None or name not in _ATTRS or not _safe_value(v):
                continue  # silently dropped: scripts, on* handlers, xmlns:*, external refs, ...
            if name == "href" and not v.startswith("#"):
                continue
            attrs[name] = v
        el.attrib.clear()
        el.attrib.update(attrs)
        if tag not in _TEXT_TAGS:
            el.text = None
        el.tail = None
        for child in list(el):
            if _local(child.tag) not in _TAGS:
                el.remove(child)  # script, style, foreignObject, image, a, animate*, set, ...
            else:
                clean(child, depth + 1)

    clean(root, 0)
    root.set("xmlns", SVG_NS)
    return ET.tostring(root, encoding="utf-8", xml_declaration=False)


# ----------------------------------------------------------------- facade
def validate_image(data: bytes) -> tuple[bytes, str]:
    """Return ``(bytes_to_store, content_type)`` or raise :class:`ImageError`."""
    if not data:
        raise ImageError("the uploaded file is empty")
    if len(data) > MAX_UPLOAD_BYTES:
        raise ImageError(f"image is too large (limit {MAX_UPLOAD_BYTES // 1024} KB)")
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        _png(data)
        return data, "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        _jpeg(data)
        return data, "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        _webp(data)
        return data, "image/webp"
    head = data[:2048].lstrip(b"\xef\xbb\xbf \t\r\n")
    if head.startswith(b"<"):
        return sanitize_svg(data), "image/svg+xml"
    raise ImageError("unsupported or unrecognised image format (use SVG, PNG, WebP or JPEG)")


class AssetStore:
    """Encrypted, server-named image files: ``<root>/<32-hex-id>.enc`` holding ``content-type \\n bytes``."""

    def __init__(self, root: Path, keyring: Keyring, salt: Callable[[], bytes | None]) -> None:
        self.root, self.keyring, self._salt = Path(root), keyring, salt

    def _path(self, asset_id: str) -> Path:
        if not ASSET_ID.fullmatch(asset_id):
            raise KeyError(asset_id)
        return self.root / f"{asset_id}.enc"

    def ensure(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            self.root.chmod(0o700)
        except OSError:
            pass

    def save(self, data: bytes, content_type: str) -> str:
        self.ensure()
        asset_id = os.urandom(16).hex()
        atomic_write(self._path(asset_id), encrypt(self.keyring, content_type.encode() + b"\n" + data, self._salt()))
        return asset_id

    def read(self, asset_id: str) -> tuple[bytes, str]:
        try:
            path = self._path(asset_id)
            plain, _ = decrypt(self.keyring, path.read_bytes())
        except (KeyError, FileNotFoundError):
            raise FileNotFoundError(asset_id) from None
        except (CryptoError, OSError) as exc:
            raise StoreError(f"image asset unreadable: {exc}") from exc
        ctype, _, body = plain.partition(b"\n")
        if ctype.decode("ascii", "replace") not in CONTENT_TYPES:
            raise StoreError("image asset has an unexpected content type")
        return body, ctype.decode("ascii")

    def delete(self, asset_id: str) -> None:
        try:
            self._path(asset_id).unlink(missing_ok=True)
        except (KeyError, OSError):
            pass

    def cleanup(self, referenced: set[str]) -> int:
        """Remove files no account references (left by a crash between write and commit)."""
        n = 0
        if self.root.is_dir():
            for p in self.root.glob("*.enc"):
                if p.stem not in referenced:
                    p.unlink(missing_ok=True)
                    n += 1
        return n
