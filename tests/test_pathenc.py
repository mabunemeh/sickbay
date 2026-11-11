import pytest

from sickbay.snapshot.pathenc import (
    InvalidHostPathError,
    decode_component,
    disk_path,
    encode_component,
    split_host_path,
)


@pytest.mark.parametrize(
    "name",
    [
        "plain.service",
        "ledger\\x2dapi.service",
        "a:b",
        "with space",
        "100%",
        "été",
        "trailing.",
        "trailing ",
        "con",
        "NUL.txt",
        "com1.tar.gz",
        "Lpt9",
        "\udcff-raw-byte",
        "user@1000.service",
    ],
)
def test_roundtrip(name: str) -> None:
    assert decode_component(encode_component(name)) == name


@pytest.mark.parametrize(
    ("name", "encoded"),
    [
        ("ledger\\x2dapi.service", "ledger%5Cx2dapi.service"),
        ("a:b", "a%3Ab"),
        ("con", "%63on"),
        ("NUL.txt", "%4EUL.txt"),
        ("trailing.", "trailing%2E"),
        ("user@1000.service", "user@1000.service"),
        ("a=b,c+d", "a=b,c+d"),
        ("console", "console"),
    ],
)
def test_encoding(name: str, encoded: str) -> None:
    assert encode_component(name) == encoded


def test_encoded_names_are_windows_safe() -> None:
    for name in ["con", "aux.conf", 'we"ird*?<>|', "x\\y", "end.", "end "]:
        out = encode_component(name)
        assert not set(out) & set('<>:"\\|?* ')
        assert not out.endswith(".")
        assert out.split(".", 1)[0].upper() not in {"CON", "AUX"}


def test_disk_path() -> None:
    assert disk_path("/etc/security/limits.d/aux.conf") == "fs/etc/security/limits.d/%61ux.conf"


@pytest.mark.parametrize("bad", ["etc/passwd", "/etc/../passwd", "/etc//passwd", "/etc/", "/a\x00"])
def test_rejects_bad_host_paths(bad: str) -> None:
    with pytest.raises(InvalidHostPathError):
        split_host_path(bad)


def test_root() -> None:
    assert split_host_path("/") == ()


def test_dot_components_are_encoded() -> None:
    # Normalized host paths never contain them, but the encoder must stay safe on its own.
    assert encode_component(".") == "%2E"
    assert encode_component("..") == "%2E%2E"
    assert decode_component("%2E%2E") == ".."


def test_empty_component_rejected() -> None:
    with pytest.raises(ValueError, match="empty"):
        encode_component("")
