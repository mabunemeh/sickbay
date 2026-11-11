"""Host path <-> on-disk path encoding, safe for Windows checkouts."""

from __future__ import annotations

from urllib.parse import unquote_to_bytes

_SAFE = frozenset(b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._@+,=-")
_RESERVED = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)


class InvalidHostPathError(ValueError):
    """A host path that is not absolute and normalized."""


def split_host_path(path: str) -> tuple[str, ...]:
    """Validate a host path and return its components ("/" gives an empty tuple)."""
    if not path.startswith("/") or "\x00" in path:
        raise InvalidHostPathError(f"host path must be absolute: {path!r}")
    if path == "/":
        return ()
    parts = path[1:].split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise InvalidHostPathError(f"host path must be normalized: {path!r}")
    return tuple(parts)


def encode_component(name: str) -> str:
    """Percent-encode one path component so it is safe on Windows and macOS."""
    if name == "":
        raise ValueError("empty path component")
    raw = name.encode("utf-8", "surrogateescape")
    out = "".join(chr(b) if b in _SAFE else f"%{b:02X}" for b in raw)
    if out in (".", ".."):
        out = out.replace(".", "%2E")
    if out.split(".", 1)[0].upper() in _RESERVED:
        out = f"%{ord(out[0]):02X}{out[1:]}"
    if out.endswith("."):
        out = f"{out[:-1]}%2E"
    return out


def decode_component(name: str) -> str:
    return unquote_to_bytes(name).decode("utf-8", "surrogateescape")


def disk_path(host_path: str) -> str:
    """Relative on-disk path (POSIX separators) for a captured host file."""
    return "/".join(["fs", *(encode_component(p) for p in split_host_path(host_path))])
