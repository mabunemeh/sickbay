"""Scrubbing of hostnames, users, IPs, MACs, machine id and environment values."""

from __future__ import annotations

import ipaddress
import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sickbay.snapshot.source import Source

_W = rb"A-Za-z0-9_"
_A = rb"A-Za-z0-9"  # word characters inside names: "_" and "-" count as boundaries
# Distro default logins identify nobody and appear in evidence (NAME="Ubuntu"), so keep them.
_DEFAULT_LOGINS = frozenset(
    {"ubuntu", "debian", "admin", "ec2-user", "centos", "fedora", "vagrant", "pi"}
)
# Domains that are public suffixes or reserved names are never scrubbed on their own.
_PUBLIC_SUFFIXES = frozenset(
    {
        "com", "net", "org", "edu", "gov", "mil", "int", "io", "dev", "app", "info", "biz",
        "co.uk", "org.uk", "ac.uk", "gov.uk", "com.au", "co.jp", "co.nz", "com.br",
        "local", "localdomain", "lan", "internal", "home", "intranet", "corp", "example",
        "test", "invalid", "localhost",
    }
)  # fmt: skip
_ENV_LINE = re.compile(rb"^([ \t]*Environment=)(.*)$", re.MULTILINE)
_ENV_TOKEN = re.compile(
    rb'(?<!\S)(?:"([A-Za-z_][A-Za-z0-9_]*)=(?:[^"\\]|\\.)*"|([A-Za-z_][A-Za-z0-9_]*)=\S*)'
)
# Known over-scrub: the dash form also matches any six dash-separated hex pairs that are not MACs.
_MAC = re.compile(
    rb"(?<![" + _W + rb":-])((?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2})(?![" + _W + rb":-])"
)
_IPV4 = re.compile(
    rb"(?<![" + _W + rb"-])(?<![" + _W + rb"]\.)"
    rb"(\d{1,3}(?:\.\d{1,3}){3})(?![" + _W + rb"-])(?!\.[" + _W + rb"])"
)
_IPV6 = re.compile(
    rb"(?<![" + _W + rb":.-])((?:[0-9A-Fa-f]{0,4}:){2,7}[0-9A-Fa-f]{0,4})"
    rb"(?![" + _W + rb":-])(?!\.[" + _W + rb"])"
)
_DOC_V4 = ("192.0.2.", "198.51.100.", "203.0.113.")


@dataclass(frozen=True)
class HostFacts:
    hostnames: tuple[str, ...] = ()
    users: tuple[tuple[int, str, str], ...] = ()  # (uid, login, gecos name)
    own_ips: tuple[str, ...] = ()
    machine_id: str = ""
    domains: tuple[str, ...] = ()


def _parse_passwd(text: str) -> list[tuple[int, str, str]]:
    users = []
    for line in text.splitlines():
        f = line.split(":")
        if len(f) < 5 or not f[2].isdigit():
            continue
        uid = int(f[2])
        if uid >= 1000 and uid != 65534 and f[0]:
            users.append((uid, f[0], f[4].split(",")[0].strip()))
    return users


def _parse_fib_trie(text: str) -> list[str]:
    ips: list[str] = []
    last = ""
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("|--"):
            last = s[3:].strip()
        elif s.startswith("/32 host LOCAL") and last:
            ips.append(last)
    return ips


def _parse_if_inet6(text: str) -> list[str]:
    ips = []
    for line in text.splitlines():
        tok = line.split()
        if tok and len(tok[0]) == 32:
            try:
                ips.append(str(ipaddress.IPv6Address(bytes.fromhex(tok[0]))))
            except ValueError:
                continue
    return ips


def _parse_resolv(text: str) -> list[str]:
    ips = []
    for line in text.splitlines():
        tok = line.split()
        if len(tok) >= 2 and tok[0] == "nameserver":
            ips.append(tok[1].split("%", 1)[0])
    return ips


def gather_facts(source: Source) -> HostFacts:
    """Collect what must be scrubbed. passwd content itself is never stored."""

    def text(path: str) -> str:
        try:
            return source.read_bytes(path).decode("utf-8", "replace")
        except OSError:
            return ""

    names = {n.strip().strip(".") for n in source.hostnames() if n.strip()}
    names |= {n.strip().strip(".") for n in text("/etc/hostname").splitlines() if n.strip()}
    domains = {n.split(".", 1)[1].lower() for n in names if "." in n}
    names |= {n.split(".", 1)[0] for n in names}
    ips = (
        _parse_fib_trie(text("/proc/net/fib_trie"))
        + _parse_if_inet6(text("/proc/net/if_inet6"))
        + _parse_resolv(text("/etc/resolv.conf"))
    )
    return HostFacts(
        hostnames=tuple(sorted(n for n in names if len(n) >= 2)),
        users=tuple(_parse_passwd(text("/etc/passwd"))),
        own_ips=tuple(sorted(set(ips))),
        machine_id=text("/etc/machine-id").strip().lower(),
        domains=tuple(sorted(d for d in domains if d and d not in _PUBLIC_SUFFIXES)),
    )


def _word_pattern(words: list[bytes], flags: int = 0) -> re.Pattern[bytes] | None:
    if not words:
        return None
    alt = rb"|".join(re.escape(w) for w in sorted(set(words), key=lambda w: (-len(w), w)))
    return re.compile(rb"(?<![" + _A + rb"])(?:" + alt + rb")(?![" + _A + rb"])", flags)


def _keep_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv4Address):
        return ip.is_loopback or ip == ipaddress.IPv4Address("0.0.0.0")
    return ip in (ipaddress.IPv6Address("::1"), ipaddress.IPv6Address("::"))


class Scrubber:
    """Deterministic scrubber; one instance per capture so mappings are consistent."""

    def __init__(self, facts: HostFacts) -> None:
        self.counts: Counter[str] = Counter()
        self._ip_map: dict[str, str] = {}
        self._ip_taken: set[str] = set()  # real addresses, never used as replacements
        self._cur = b""  # stream being scrubbed: replacements never equal text already in it
        self._mac_map: dict[str, str] = {}
        self._v4_n = 0
        self._v6_n = 0
        self._mac_n = 0
        self._machine_id = (
            re.compile(re.escape(facts.machine_id.encode()), re.IGNORECASE)
            if len(facts.machine_id) == 32
            else None
        )
        self._host_re = _word_pattern([h.encode() for h in facts.hostnames], re.IGNORECASE)
        self._domain_re = _word_pattern([d.encode() for d in facts.domains], re.IGNORECASE)
        self._user_map: dict[bytes, bytes] = {}
        for uid, login, gecos in facts.users:
            repl = f"user{uid}".encode()
            if len(login) >= 3 and login.lower() not in _DEFAULT_LOGINS:
                self._user_map[login.lower().encode()] = repl
            # Only the whole GECOS string: single words ("System", "Service") occur in evidence.
            if len(gecos) >= 3 and gecos.lower() not in _DEFAULT_LOGINS:
                self._user_map.setdefault(gecos.lower().encode(), repl)
        self._user_re = _word_pattern(list(self._user_map), re.IGNORECASE)
        own: list[str] = []
        for raw in facts.own_ips:
            try:
                ip = ipaddress.ip_address(raw)
            except ValueError:
                continue
            if _keep_ip(ip):
                continue
            own.append(str(ip))
            self._ip_taken.add(str(ip))
            if isinstance(ip, ipaddress.IPv6Address):
                own.append(ip.exploded)
        alts = rb"|".join(re.escape(s.encode()) for s in sorted(set(own), key=lambda s: -len(s)))
        parts = []
        if own:
            parts.append(rb"(?i:(?<![0-9A-Za-z:])(" + alts + rb")(?![0-9A-Za-z:]))")
        parts += [_IPV6.pattern, _IPV4.pattern]
        # One pass, leftmost match wins; at a position: own addresses, then IPv6, then IPv4.
        self._ip_re = re.compile(rb"|".join(parts))

    def _map_ip(self, text: str) -> str | None:
        """Replacement for a real address, or None to keep it (loopback, unspecified)."""
        try:
            ip = ipaddress.ip_address(text)
        except ValueError:
            return None
        if _keep_ip(ip):
            return None
        key = str(ip)
        if key not in self._ip_map:
            self._ip_taken.add(key)
            while True:
                if isinstance(ip, ipaddress.IPv4Address):
                    block, n = divmod(self._v4_n, 254)
                    self._v4_n += 1
                    out = f"{_DOC_V4[block % 3]}{n + 1}"
                else:
                    self._v6_n += 1
                    out = f"2001:db8::{self._v6_n:x}"
                if out not in self._ip_taken and out.encode("ascii") not in self._cur:
                    break
            self._ip_map[key] = out
        return self._ip_map[key]

    def _ip_sub(self, m: re.Match[bytes]) -> bytes:
        text = next(g for g in m.groups() if g is not None)
        new = self._map_ip(text.decode("ascii"))
        if new is None:
            return m.group(0)
        self.counts["ip"] += 1
        return new.encode("ascii")

    def _mac_sub(self, m: re.Match[bytes]) -> bytes:
        raw = m.group(1).decode("ascii")
        key = raw.lower().replace("-", ":")
        if key not in self._mac_map:
            self._mac_n += 1
            self._mac_map[key] = f"02:00:00:00:00:{self._mac_n:02x}"
        self.counts["mac"] += 1
        out = self._mac_map[key]
        return out.replace(":", "-").encode("ascii") if "-" in raw else out.encode("ascii")

    def _env_sub(self, m: re.Match[bytes]) -> bytes:
        def tok(t: re.Match[bytes]) -> bytes:
            self.counts["env"] += 1
            if t.group(1):
                return b'"' + t.group(1) + b"=<redacted>" + b'"'
            return t.group(2) + b"=<redacted>"

        return m.group(1) + _ENV_TOKEN.sub(tok, m.group(2))

    def _counting(
        self, rule: str, repl: Callable[[re.Match[bytes]], bytes]
    ) -> Callable[[re.Match[bytes]], bytes]:
        def inner(m: re.Match[bytes]) -> bytes:
            self.counts[rule] += 1
            return repl(m)

        return inner

    def scrub(self, data: bytes) -> bytes:
        data = _ENV_LINE.sub(self._env_sub, data)
        if self._machine_id:
            data = self._machine_id.sub(self._counting("machine_id", lambda m: b"0" * 32), data)
        if self._host_re:
            data = self._host_re.sub(self._counting("hostname", lambda m: b"host"), data)
        if self._domain_re:
            data = self._domain_re.sub(self._counting("domain", lambda m: b"domain"), data)
        if self._user_re:
            data = self._user_re.sub(
                self._counting("username", lambda m: self._user_map[m.group(0).lower()]), data
            )
        data = _MAC.sub(self._mac_sub, data)
        self._cur = data
        return self._ip_re.sub(self._ip_sub, data)

    def scrub_str(self, text: str) -> str:
        raw = text.encode("utf-8", "surrogateescape")
        return self.scrub(raw).decode("utf-8", "surrogateescape")
