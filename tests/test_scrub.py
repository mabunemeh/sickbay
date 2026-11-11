from fakes import build_source

from sickbay.snapshot.scrub import HostFacts, Scrubber, gather_facts

FACTS = HostFacts(
    hostnames=("buildhost", "buildhost.corp.example"),
    users=((1000, "alice", "Alice Example"), (1001, "bob", "")),
    own_ips=("10.20.30.40", "fe80::1234", "127.0.0.1"),
    machine_id="0123456789abcdef0123456789abcdef",
    domains=("corp.example",),
)


def scrub(text: str, facts: HostFacts = FACTS) -> str:
    return Scrubber(facts).scrub_str(text)


def test_hostname_short_and_fqdn() -> None:
    assert scrub("on buildhost.corp.example and BUILDHOST") == "on host and host"


def test_hostname_word_boundaries() -> None:
    assert scrub("buildhost-api buildhost_x") == "host-api host_x"
    assert scrub("buildhosted xbuildhost buildhost2") == "buildhosted xbuildhost buildhost2"


def test_username_and_gecos() -> None:
    assert scrub("/home/alice owner=bob Alice Example") == "/home/user1000 owner=user1001 user1000"


def test_username_negative() -> None:
    assert scrub("alicefoo bobcat xbob root") == "alicefoo bobcat xbob root"


def test_ipv4_generic_mapping_is_consistent() -> None:
    out = scrub("a 8.8.8.8 b 8.8.4.4 c 8.8.8.8")
    assert out == "a 192.0.2.1 b 192.0.2.2 c 192.0.2.1"


def test_own_ip_and_port() -> None:
    assert scrub("10.20.30.40:8080") == "192.0.2.1:8080"


def test_kernel_string_kept() -> None:
    assert scrub("6.18.54.1-microsoft-standard-WSL2") == "6.18.54.1-microsoft-standard-WSL2"


def test_loopback_and_unspecified_kept() -> None:
    text = "127.0.0.1 127.0.0.53%lo 0.0.0.0 :: ::1 [::]:22 [::1]:22"
    assert scrub(text) == text


def test_invalid_quad_kept() -> None:
    assert scrub("999.1.1.1 1.2.3 01.2.3.4") == "999.1.1.1 1.2.3 01.2.3.4"


def test_word_adjacent_numbers_kept() -> None:
    assert scrub("v1.2.3.4x 1.2.3.4.5 a-1.2.3.4") == "v1.2.3.4x 1.2.3.4.5 a-1.2.3.4"


def test_ipv6_mapped_and_consistent() -> None:
    out = scrub("2606:4700::1111 and 2606:4700::1111 and fe80::1234 and 2606:4700::1001")
    assert out == "2001:db8::1 and 2001:db8::1 and 2001:db8::2 and 2001:db8::3"


def test_ipv6_ignores_times_and_macs() -> None:
    assert scrub("12:34:56 at") == "12:34:56 at"


def test_mac_mapped_consistently() -> None:
    out = scrub("aa:bb:cc:dd:ee:ff 11:22:33:44:55:66 AA:BB:CC:DD:EE:FF")
    assert out == "02:00:00:00:00:01 02:00:00:00:00:02 02:00:00:00:00:01"


def test_machine_id_zeroed_anywhere() -> None:
    out = scrub("id=0123456789ABCDEF0123456789abcdef/x")
    assert out == "id=" + "0" * 32 + "/x"


def test_environment_values_redacted_keys_kept() -> None:
    out = scrub('Environment=API_KEY=s3cret MODE=prod "MSG=a b"\nEnvironmentFile=/etc/x\n')
    assert out == (
        'Environment=API_KEY=<redacted> MODE=<redacted> "MSG=<redacted>"\nEnvironmentFile=/etc/x\n'
    )


def test_environment_negative() -> None:
    assert scrub("PassEnvironment=FOO\nExecStart=/x --env=A=b\n") == (
        "PassEnvironment=FOO\nExecStart=/x --env=A=b\n"
    )


def test_nul_separated_bytes_scrubbed() -> None:
    out = Scrubber(FACTS).scrub(b"/bin/x\x00--owner=alice\x0010.20.30.40\x00")
    assert out == b"/bin/x\x00--owner=user1000\x00192.0.2.1\x00"


def test_counts_record_rules_not_values() -> None:
    s = Scrubber(FACTS)
    s.scrub(b"alice alice 8.8.8.8 aa:bb:cc:dd:ee:ff")
    assert dict(s.counts) == {"username": 2, "ip": 1, "mac": 1}


def test_no_leak_after_scrub() -> None:
    out = scrub("alice@buildhost 10.20.30.40 fe80::1234 8.8.8.8")
    for secret in ("alice", "buildhost", "10.20.30.40", "fe80::1234", "8.8.8.8"):
        assert secret not in out


def test_gather_facts_from_source() -> None:
    facts = gather_facts(build_source())
    assert "buildhost" in facts.hostnames
    assert (1000, "alice", "Alice Example") in facts.users
    assert all(uid >= 1000 and uid != 65534 for uid, _, _ in facts.users)
    assert "10.20.30.40" in facts.own_ips
    assert "10.20.30.1" in facts.own_ips  # resolv.conf nameserver
    assert facts.machine_id == "0123456789abcdef0123456789abcdef"


def test_username_case_insensitive() -> None:
    assert scrub("ALICE Bob aLiCe") == "user1000 user1001 user1000"
    assert scrub("ALICEFOO") == "ALICEFOO"


def test_underscore_and_dash_are_name_boundaries() -> None:
    assert scrub("alice_data bob-x") == "user1000_data user1001-x"
    assert scrub("alice2 xalice a_lice") == "alice2 xalice a_lice"


def test_gecos_scrubbed_as_a_whole_string_only() -> None:
    assert scrub("alice example") == "user1000"
    assert scrub("Example Exam") == "Example Exam"


def test_gecos_words_do_not_break_evidence() -> None:
    facts = HostFacts(users=((1002, "svc", "System Service Account"), (1003, "bld", "Build User")))
    text = "system.slice user@1000.service User=root Build User Service"
    assert scrub(text, facts) == "system.slice user@1000.service User=root user1003 Service"
    assert scrub("System Service Account", facts) == "user1002"


def test_default_distro_logins_and_short_names_are_kept() -> None:
    facts = HostFacts(users=((1000, "ubuntu", "Ubuntu"), (1001, "ab", ""), (1002, "carol", "")))
    out = scrub('NAME="Ubuntu" ab carol /home/ubuntu', facts)
    assert out == 'NAME="Ubuntu" ab user1002 /home/ubuntu'


def test_forged_placeholders_cannot_crash_or_forge_addresses() -> None:
    forged = b"\x00\x01SBIP7\x01\x00 \x00\x01SBIP0\x01\x00 \x01SBIP7\x01 8.8.8.8"
    out = Scrubber(FACTS).scrub(forged)
    assert out.startswith(b"\x00\x01SBIP7\x01\x00 \x00\x01SBIP0\x01\x00 \x01SBIP7\x01 ")
    assert out.endswith(b"192.0.2.1")
    assert out.count(b"SBIP") == 3


def test_replacement_never_equals_text_already_in_the_stream() -> None:
    out = scrub("8.8.8.8 and 192.0.2.1").split()
    assert out[0] != "192.0.2.1" and out[2] != "192.0.2.1" and out[0] != out[2]


def test_dash_form_mac() -> None:
    out = scrub("aa-bb-cc-dd-ee-ff and AA:BB:CC:DD:EE:FF")
    assert out == "02-00-00-00-00-01 and 02:00:00:00:00:01"


def test_dash_form_mac_negative() -> None:
    text = "aa-bb-cc-dd-ee and aa-bb-cc-dd-ee-ff-00 and xaa-bb-cc-dd-ee-ff"
    assert scrub(text) == text


def test_domain_part_scrubbed() -> None:
    assert scrub("search corp.example; api.corp.example") == "search domain; api.domain"
    assert scrub("corp.examples xcorp.corpx") == "corp.examples xcorp.corpx"


def test_public_suffixes_never_scrubbed_on_their_own() -> None:
    src = build_source()
    src.names = ["box.co.uk", "h.local", "x.example.com"]
    facts = gather_facts(src)
    assert facts.domains == ("example.com",)
    assert scrub("co.uk local example.com org", facts) == "co.uk local domain org"


def test_real_doc_range_addresses_are_remapped_without_collisions() -> None:
    out = scrub("192.0.2.1 8.8.8.8 192.0.2.1 2001:db8::1 2606:4700::1111").split()
    assert out[0] == out[2] and out[0] != "192.0.2.1"
    assert out[3] != "2001:db8::1"
    assert len(set(out)) == 4
    assert out[1] not in ("192.0.2.1",)


def test_replacements_skip_real_own_addresses() -> None:
    facts = HostFacts(own_ips=("192.0.2.1",))
    out = scrub("8.8.8.8 192.0.2.1", facts).split()
    assert "192.0.2.1" not in out and out[0] != out[1]
