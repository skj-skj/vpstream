"""The public dashboard's password: verifier, challenge-response, sessions and limits."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import shutil
import stat
import subprocess
import unicodedata
from importlib import resources
from pathlib import Path
from typing import Any

import pytest

from vpstream import login
from vpstream.login import Limiter, Login, LoginRefused, Verifier

PASSWORD = "correct horse battery"
FEW = 1000  # iterations: enough to be real, few enough for tests


class Clock:
    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def verifier(password: str = PASSWORD) -> Verifier:
    return Verifier.make(password, iterations=FEW)


def answer(challenge: dict[str, Any], password: str = PASSWORD) -> str:
    """What a browser that knows `password` answers, computed apart from `login`."""
    key = hashlib.pbkdf2_hmac(
        "sha256",
        unicodedata.normalize("NFC", password).encode(),
        bytes.fromhex(challenge["salt"]),
        challenge["iterations"],
        32,
    )
    message = (challenge["label"] + challenge["nonce"]).encode()
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def make(path: Path | None = None, wall: Clock | None = None) -> tuple[Login, Clock, Clock]:
    mono, wall = Clock(), wall or Clock()
    limiter = Limiter(path, clock=wall, log=lambda m: None)
    return Login(verifier(), limiter, clock=mono), mono, wall


def log_in(auth: Login, ip: str = "192.0.2.1", password: str = PASSWORD) -> str:
    challenge = auth.challenge(ip)
    return auth.login(ip, challenge["nonce"], answer(challenge, password)).token


def refused(status: int, call: Any, *args: Any) -> LoginRefused:
    with pytest.raises(LoginRefused) as info:
        call(*args)
    assert info.value.status == status, info.value
    return info.value


# --- the verifier


def test_a_verifier_keeps_no_password_and_reads_back_as_written() -> None:
    made = verifier()
    text = made.to_text()
    assert PASSWORD not in text and PASSWORD.encode().hex() not in text
    assert text.startswith("pbkdf2-sha256$1000$") and text.endswith("\n")
    assert Verifier.parse(text) == made
    assert Verifier.make(PASSWORD, FEW).salt != made.salt, "a new salt each time"


def test_the_same_password_typed_two_ways_is_the_same_password() -> None:
    composed, decomposed = "café au lait!", "café au lait!"
    made = Verifier.make(composed, FEW)
    assert login.derive_key(decomposed, made.salt, FEW) == made.key


@pytest.mark.parametrize(
    "text",
    [
        "",
        "scrypt$1000$" + "00" * 16 + "$" + "00" * 32,
        "pbkdf2-sha256$999$" + "00" * 16 + "$" + "00" * 32,  # too few rounds
        "pbkdf2-sha256$10000001$" + "00" * 16 + "$" + "00" * 32,
        "pbkdf2-sha256$1e3$" + "00" * 16 + "$" + "00" * 32,
        # Arabic-Indic digits: isdecimal() takes them, int() too, a file must not.
        "pbkdf2-sha256$\u0661\u0660\u0660\u0660$" + "00" * 16 + "$" + "00" * 32,
        "pbkdf2-sha256$1000$" + "0" * 17 + "$" + "00" * 32,  # odd salt
        "pbkdf2-sha256$1000$" + "00" * 4 + "$" + "00" * 32,  # short salt
        "pbkdf2-sha256$1000$" + "zz" * 16 + "$" + "00" * 32,
        "pbkdf2-sha256$1000$" + "00" * 16 + "$" + "00" * 31,
        "pbkdf2-sha256$1000$" + "00" * 16 + "$" + "AA" * 32,  # upper case is not what is written
        "pbkdf2-sha256$1000$" + "00" * 16 + "$" + "00" * 32 + "$extra",
    ],
)
def test_a_verifier_file_that_is_not_exactly_right_is_refused(text: str) -> None:
    with pytest.raises(login.PasswordError):
        Verifier.parse(text)


@pytest.mark.parametrize(
    ("password", "problem"),
    [("short", "at least"), ("x" * 1025, "at most"), ("good password\n", "control")],
)
def test_passwords_that_cannot_be_used(password: str, problem: str) -> None:
    assert problem in (login.password_problem(password) or "")
    with pytest.raises(login.PasswordError, match=problem):
        Verifier.make(password, FEW)


def test_the_verifier_file_is_never_readable_by_others(tmp_path: Path) -> None:
    path = tmp_path / "dashboard.key"
    path.write_text("old")
    path.chmod(0o644)
    old_mask = os.umask(0)  # the file's own mode must do it, whatever the caller's umask
    try:
        login.write_verifier(path, verifier())
    finally:
        os.umask(old_mask)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert login.load_verifier(path) is not None
    assert [p.name for p in tmp_path.iterdir()] == ["dashboard.key"], "no temporary file left"
    assert login.load_verifier(tmp_path / "none") is None
    path.write_text("garbage")
    with pytest.raises(login.PasswordError, match=r"dashboard\.key"):
        login.load_verifier(path)


# --- challenge and answer


def test_the_right_answer_logs_in_and_the_session_is_found_by_its_cookie() -> None:
    auth, _, _ = make()
    challenge = auth.challenge("192.0.2.1")
    assert set(challenge) == {"nonce", "salt", "iterations", "label"}
    assert challenge["iterations"] == FEW and challenge["label"] == login.LABEL
    granted = auth.login("192.0.2.1", challenge["nonce"], answer(challenge))
    token, session = granted.token, granted.session
    assert auth.session(token) is session
    assert login.csrf_ok(session, session.csrf)
    assert token != session.csrf


def test_a_captured_answer_cannot_be_replayed() -> None:
    auth, _, _ = make()
    challenge = auth.challenge("192.0.2.1")
    proof = answer(challenge)
    auth.login("192.0.2.1", challenge["nonce"], proof)
    error = refused(400, auth.login, "198.51.100.7", challenge["nonce"], proof)
    assert "try again" in str(error)
    # A replay is not a guess at the password: it does not count towards a lockout.
    assert auth.limiter.wait(login.Who("198.51.100.7")) == 0


def test_a_wrong_answer_burns_the_nonce_too() -> None:
    auth, _, _ = make()
    challenge = auth.challenge("192.0.2.1")
    refused(401, auth.login, "192.0.2.1", challenge["nonce"], "0" * 64)
    # The right answer to that nonce is now worthless: each nonce is one try.
    refused(400, auth.login, "192.0.2.1", challenge["nonce"], answer(challenge))


def test_an_answer_for_another_nonce_is_wrong() -> None:
    auth, _, _ = make()
    first, second = auth.challenge("192.0.2.1"), auth.challenge("192.0.2.1")
    refused(401, auth.login, "192.0.2.1", second["nonce"], answer(first))


def test_a_nonce_the_server_never_gave_is_refused_without_a_check() -> None:
    auth, _, _ = make()
    refused(400, auth.login, "192.0.2.1", "ab" * 32, "cd" * 32)
    assert auth.limiter.wait(login.Who("192.0.2.1")) == 0


@pytest.mark.parametrize("nonce", [None, 5, ["x"], {"a": 1}, ""])
def test_a_nonce_of_the_wrong_kind_is_refused(nonce: Any) -> None:
    auth, _, _ = make()
    auth.challenge("192.0.2.1")
    refused(400, auth.login, "192.0.2.1", nonce, "00" * 32)


@pytest.mark.parametrize(
    "proof", [None, 7, ["a"], "", "zz" * 32, "AB" * 32, "é" * 64, "ab" * 33, "ab" * 31]
)
def test_a_malformed_answer_is_a_wrong_answer_not_a_crash(proof: Any) -> None:
    auth, _, _ = make()
    challenge = auth.challenge("192.0.2.1")
    refused(401, auth.login, "192.0.2.1", challenge["nonce"], proof)


def test_a_nonce_expires() -> None:
    auth, mono, _ = make()
    challenge = auth.challenge("192.0.2.1")
    mono.now += login.NONCE_SECONDS + 1
    refused(400, auth.login, "192.0.2.1", challenge["nonce"], answer(challenge))


def test_one_address_cannot_push_out_another_ones_nonce() -> None:
    auth, _, _ = make()
    mine = auth.challenge("192.0.2.1")
    for _ in range(login.MAX_NONCES_PER_ADDRESS * 3):
        flood = auth.challenge("198.51.100.9")
    auth.login("192.0.2.1", mine["nonce"], answer(mine))
    # The flooder keeps only its newest few.
    assert sum(1 for v in auth._nonces.values() if v[1] == "198.51.100.9") <= (
        login.MAX_NONCES_PER_ADDRESS
    )
    auth.login("198.51.100.9", flood["nonce"], answer(flood))


def test_a_flood_of_challenges_never_takes_back_a_nonce_given_out() -> None:
    auth, _, _ = make()
    mine = auth.challenge("198.51.100.200")  # the owner, deriving its key meanwhile
    for n in range(login.MAX_NONCES - 1):
        auth.challenge(f"2001:db8:{n // 256}:{n % 256}::1")
    error = refused(503, auth.challenge, "2001:db8:ffff::1")
    assert error.retry_after > 0 and len(auth._nonces) == login.MAX_NONCES
    auth.login("198.51.100.200", mine["nonce"], answer(mine))


def test_without_a_password_nobody_gets_in() -> None:
    auth = Login(None, Limiter(None, log=lambda m: None))
    refused(503, auth.challenge, "192.0.2.1")
    refused(503, auth.login, "192.0.2.1", "ab" * 32, "cd" * 32)


def test_the_answer_and_the_token_are_compared_in_constant_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compared: list[tuple[Any, Any]] = []
    real = hmac.compare_digest

    def spy(a: Any, b: Any) -> bool:
        compared.append((a, b))
        return real(a, b)

    monkeypatch.setattr(login.hmac, "compare_digest", spy)
    auth, _, _ = make()
    challenge = auth.challenge("192.0.2.1")
    good = answer(challenge)
    wrong_last = good[:-1] + ("0" if good[-1] != "0" else "1")
    refused(401, auth.login, "192.0.2.1", challenge["nonce"], wrong_last)
    assert compared[-1] == (good, wrong_last)
    challenge = auth.challenge("192.0.2.1")
    session = auth.login("192.0.2.1", challenge["nonce"], answer(challenge)).session
    other = "y" if session.csrf[0] == "x" else "x"
    assert not login.csrf_ok(session, other + session.csrf[1:])
    assert compared[-1][0] == session.csrf.encode()
    # Text that is not ASCII is compared as bytes, never raised on.
    assert not login.csrf_ok(session, "é" * 43)
    assert not login.csrf_ok(session, None)


# --- sessions


def test_a_session_ends_after_its_lifetime_on_the_monotonic_clock() -> None:
    auth, mono, wall = make()
    token = log_in(auth)
    wall.now -= 86400 * 365  # the wall clock going back changes nothing
    mono.now += login.SESSION_SECONDS - 1
    assert auth.session(token) is not None
    mono.now += 2
    assert auth.session(token) is None
    assert auth.session(token) is None, "and stays ended"


def test_logout_ends_that_session_only() -> None:
    auth, _, _ = make()
    phone, laptop = log_in(auth), log_in(auth, "198.51.100.3")
    auth.logout(phone)
    assert auth.session(phone) is None and auth.session(laptop) is not None
    auth.logout(phone)  # twice is fine
    auth.logout("not a token")
    auth.logout(None)


@pytest.mark.parametrize("cookie", [None, "", "x" * 42, "x" * 44, "é" * 43, "a b" * 14 + "c"])
def test_a_cookie_that_is_not_a_token_is_no_session(cookie: str | None) -> None:
    auth, _, _ = make()
    log_in(auth)
    assert auth.session(cookie) is None


def test_old_sessions_make_room_for_new_ones() -> None:
    auth, mono, _ = make()
    first = log_in(auth)
    for _ in range(login.MAX_SESSIONS):
        mono.now += 1
        last = log_in(auth)
    assert auth.session(first) is None and auth.session(last) is not None
    assert len(auth._sessions) <= login.MAX_SESSIONS


def test_a_restart_ends_every_session() -> None:
    auth, _, _ = make()
    token = log_in(auth)
    again, _, _ = make()
    assert again.session(token) is None


# --- failed logins


def fail(auth: Login, ip: str, times: int = 1) -> None:
    for _ in range(times):
        challenge = auth.challenge(ip)
        refused(401, auth.login, ip, challenge["nonce"], answer(challenge, "wrong password!"))


def test_an_address_that_guesses_wrong_too_often_waits_even_with_the_right_password() -> None:
    auth, _, wall = make()
    fail(auth, "192.0.2.1", login.FAILS_PER_ADDRESS)
    error = refused(429, auth.challenge, "192.0.2.1")
    assert 0 < error.retry_after <= login.FAIL_SECONDS
    # A nonce taken before the lockout does not get around it either.
    auth2, _, _ = make()
    early = auth2.challenge("192.0.2.1")
    fail(auth2, "192.0.2.1", login.FAILS_PER_ADDRESS)
    refused(429, auth2.login, "192.0.2.1", early["nonce"], answer(early))
    # Another address is not held up.
    log_in(auth, "198.51.100.1")
    wall.now += login.FAIL_SECONDS + 1
    log_in(auth, "192.0.2.1")


def test_the_right_password_clears_that_address_only() -> None:
    auth, _, _ = make()
    fail(auth, "192.0.2.1", login.FAILS_PER_ADDRESS - 1)
    fail(auth, "198.51.100.1", login.FAILS_PER_ADDRESS - 1)
    log_in(auth, "192.0.2.1")
    fail(auth, "192.0.2.1", login.FAILS_PER_ADDRESS - 1)
    auth.challenge("192.0.2.1")
    fail(auth, "198.51.100.1")
    refused(429, auth.challenge, "198.51.100.1")


def test_guessing_from_many_addresses_is_limited_as_a_whole() -> None:
    auth, _, wall = make()
    for n in range(login.FAILS_ALL):
        fail(auth, f"10.1.{n // 200}.{n % 200}")
    refused(429, auth.challenge, "203.0.113.200")
    wall.now += login.ALL_FAIL_SECONDS + 1
    log_in(auth, "203.0.113.200")


def test_ipv6_addresses_of_one_network_count_together() -> None:
    auth, _, _ = make()
    for n in range(login.FAILS_PER_ADDRESS):
        fail(auth, f"2001:db8:1:2::{n + 1:x}")
    refused(429, auth.challenge, "2001:db8:1:2:ffff::1")
    auth.challenge("2001:db8:1:3::1")
    assert login.address_key("::ffff:192.0.2.1") == "192.0.2.1"
    assert login.address_key("not an address") == login.address_key(None) == "unknown"


def test_failed_logins_outlive_a_restart(tmp_path: Path) -> None:
    record = tmp_path / "dashboard-logins.json"
    wall = Clock()
    auth, _, _ = make(record, wall)
    fail(auth, "192.0.2.1", login.FAILS_PER_ADDRESS)
    assert "192.0.2.1" in record.read_text()
    restarted, _, _ = make(record, wall)
    refused(429, restarted.challenge, "192.0.2.1")
    wall.now += login.FAIL_SECONDS + 1
    log_in(make(record, wall)[0], "192.0.2.1")


def test_the_whole_count_outlives_a_restart_too(tmp_path: Path) -> None:
    record = tmp_path / "dashboard-logins.json"
    wall = Clock()
    auth, _, _ = make(record, wall)
    for n in range(login.FAILS_ALL):
        fail(auth, f"10.2.{n // 200}.{n % 200}")
    refused(429, make(record, wall)[0].challenge, "203.0.113.9")


@pytest.mark.parametrize(
    "text",
    [
        "{not json",
        "[1, 2]",
        '{"addresses": [1], "all": {"x": 1}}',
        '{"addresses": {"192.0.2.1": ["a", null, 1e400]}, "all": "x"}',
    ],
)
def test_a_broken_record_starts_from_nothing_and_says_so(tmp_path: Path, text: str) -> None:
    record = tmp_path / "dashboard-logins.json"
    record.write_text(text)
    said: list[str] = []
    limiter = Limiter(record, clock=Clock(), log=said.append)
    assert limiter.wait(login.Who("192.0.2.1")) == 0
    Login(verifier(), limiter).challenge("192.0.2.1")
    if text.startswith("{not"):
        assert said and "cannot be read" in said[0]


def test_a_clock_set_back_does_not_lock_an_address_out_for_longer_than_a_window(
    tmp_path: Path,
) -> None:
    record = tmp_path / "dashboard-logins.json"
    wall = Clock()
    auth, _, _ = make(record, wall)
    fail(auth, "192.0.2.1", login.FAILS_PER_ADDRESS)
    wall.now -= 30 * 86400  # set back a month: the failures now lie in the future
    restarted, _, _ = make(record, wall)
    error = refused(429, restarted.challenge, "192.0.2.1")
    assert error.retry_after <= login.FAIL_SECONDS
    wall.now += login.FAIL_SECONDS + 1
    log_in(restarted, "192.0.2.1")


def test_the_record_stays_small_when_many_addresses_fail(tmp_path: Path) -> None:
    record = tmp_path / "dashboard-logins.json"
    limiter = Limiter(record, clock=Clock(), log=lambda m: None)
    for _ in range(login.FAILS_PER_ADDRESS * 3):
        limiter.failed(login.Who("192.0.2.1"))
    data = json.loads(record.read_text())
    assert len(data["addresses"]["192.0.2.1"]) == login.FAILS_PER_ADDRESS
    assert len(data["all"]) <= login.FAILS_ALL


def test_the_addresses_kept_are_bounded_and_the_quietest_go_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(login, "MAX_ADDRESSES", 3)
    wall = Clock()
    limiter = Limiter(None, clock=wall, log=lambda m: None)
    for n in range(6):
        wall.now += 1
        limiter.failed(login.Who(f"192.0.2.{n}"))
    assert sorted(limiter._by_key) == ["192.0.2.3", "192.0.2.4", "192.0.2.5"]


def test_a_record_that_cannot_be_written_does_not_stop_the_count(tmp_path: Path) -> None:
    folder = tmp_path / "gone"
    said: list[str] = []
    auth = Login(verifier(), Limiter(folder / "x.json", clock=Clock(), log=said.append))
    fail(auth, "192.0.2.1", login.FAILS_PER_ADDRESS)
    refused(429, auth.challenge, "192.0.2.1")
    assert said and "could not be written" in said[0]


# --- the page's JavaScript derives the same key and proof


NODE_CRYPTO = """
const fs = require("fs"), vm = require("vm");
const context = { window: {}, Uint8Array, Int32Array, Math };
vm.createContext(context);
vm.runInContext(fs.readFileSync(process.argv[1], "utf8"), context);
const C = context.window.vpCrypto;
const out = JSON.parse(fs.readFileSync(0, "utf8")).map((c) => {
  const pw = C.utf8(c.password.normalize("NFC"));
  const key = C.pbkdf2(pw, C.unhex(c.salt), c.iterations, c.length);
  return {
    key: C.hex(key),
    sha: C.hex(C.sha256(C.utf8(c.password))),
    hmac: C.hex(C.hmac(C.utf8(c.password), C.unhex(c.salt))),
    proof: c.nonce ? C.hex(C.hmac(key, C.utf8(c.label + c.nonce))) : null,
  };
});
console.log(JSON.stringify(out));
"""


def node_crypto(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    script = resources.files("vpstream.assets").joinpath("pbkdf2.js")
    res = subprocess.run(
        [node, "-e", NODE_CRYPTO, str(script)],
        input=json.dumps(cases),
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    result: list[dict[str, Any]] = json.loads(res.stdout)
    return result


def test_the_pages_pbkdf2_matches_python_on_known_and_random_vectors() -> None:
    known = [  # PBKDF2-HMAC-SHA256 vectors published with RFC 7914 and widely reused
        (
            "password",
            b"salt",
            1,
            32,
            "120fb6cffcf8b32c43e7225256c4f837a86548c92ccc35480805987cb70be17b",
        ),
        (
            "password",
            b"salt",
            2,
            32,
            "ae4d0c95af6b46d32d0adff928f06dd02a303f8ef3c251dfd6e2d85a95474c43",
        ),
        (
            "passwordPASSWORDpassword",
            b"saltSALTsaltSALTsaltSALTsaltSALTsalt",
            4096,
            40,
            "348c89dbcbd32b2f32d814b8116e84cf2b17347ebc1800181c4e2a1fb8dd53e1c635518c7dac47e9",
        ),
    ]
    cases: list[dict[str, Any]] = [
        {"password": p, "salt": s.hex(), "iterations": i, "length": n} for p, s, i, n, _ in known
    ]
    passwords = ["x" * 70, "café \U0001f600 中文", "café", "", "a" * 64, "b" * 65]
    for n, password in enumerate(passwords):
        nonce = os.urandom(32).hex()
        salt = os.urandom(n + 1 if n else 16)
        cases.append(
            {
                "password": password,
                "salt": salt.hex(),
                "iterations": [1, 3, 64, 1000, 2, 7][n],
                "length": 32,
                "nonce": nonce,
                "label": login.LABEL,
            }
        )
    got = node_crypto(cases)
    for (_, _, _, _, expect), out in zip(known, got, strict=False):
        assert out["key"] == expect
    for case, out in zip(cases, got, strict=True):
        salt = bytes.fromhex(case["salt"])
        raw = case["password"].encode()
        assert out["sha"] == hashlib.sha256(raw).hexdigest(), case
        assert out["hmac"] == hmac.new(raw, salt, hashlib.sha256).hexdigest(), case
        key = login.derive_key(case["password"], salt, case["iterations"])
        if case["length"] == 32:
            assert out["key"] == key.hex(), case
        if case.get("nonce"):
            assert out["proof"] == login.proof(key, case["nonce"]), case


def test_a_proof_made_by_the_page_logs_in() -> None:
    auth, _, _ = make()
    challenge = auth.challenge("192.0.2.1")
    case = {
        "password": PASSWORD,
        "salt": challenge["salt"],
        "iterations": challenge["iterations"],
        "length": 32,
        "nonce": challenge["nonce"],
        "label": challenge["label"],
    }
    [out] = node_crypto([case])
    granted = auth.login("192.0.2.1", challenge["nonce"], out["proof"])
    assert auth.session(granted.token) is not None


# --- a browser that logged in before


def granted(auth: Login, ip: str, device: str | None = None) -> login.Granted:
    challenge = auth.challenge(ip, device)
    return auth.login(ip, challenge["nonce"], answer(challenge), device)


def test_a_known_browser_is_not_locked_out_by_guessing_from_elsewhere() -> None:
    auth, _, _ = make()
    device = granted(auth, "198.51.100.200").device
    # Ten networks send junk answers: enough for the limit over everyone.
    for n in range(login.FAILS_ALL // login.FAILS_PER_ADDRESS):
        fail(auth, f"2001:db8:0:{n}::1", login.FAILS_PER_ADDRESS)
    refused(429, auth.challenge, "203.0.113.50")  # a browser not known yet waits
    # The owner's browser, even from the attackers' own address, gets in.
    again = granted(auth, "2001:db8:0:0::1", device)
    assert again.device == device, "the same device keeps its id"


def test_a_known_browser_is_counted_on_its_own() -> None:
    auth, _, _ = make()
    device = granted(auth, "198.51.100.200").device
    for _ in range(login.FAILS_PER_ADDRESS):
        challenge = auth.challenge("198.51.100.200", device)
        refused(401, auth.login, "198.51.100.200", challenge["nonce"], "0" * 64, device)
    refused(429, auth.challenge, "198.51.100.200", device)
    # Its failures do not count against its address, nor against everyone.
    auth.challenge("198.51.100.200")
    assert auth.limiter._all == []


@pytest.mark.parametrize(
    "forge",
    [
        lambda d: d[:-1] + ("0" if d[-1] != "0" else "1"),  # a changed signature
        lambda d: "A" * 22 + d[22:],  # another id under the same signature
        lambda d: d.split(".")[0],
        lambda d: "\u00e9" * 87,
        lambda d: None,
    ],
)
def test_a_forged_device_cookie_is_a_stranger(forge: Any) -> None:
    auth, _, _ = make()
    device = granted(auth, "198.51.100.200").device
    assert not auth.who("198.51.100.200", forge(device)).known
    assert auth.who("198.51.100.200", device).known


def test_a_device_cookie_of_another_daemon_or_an_old_password_is_a_stranger() -> None:
    auth, _, _ = make()
    device = granted(auth, "198.51.100.200").device
    other = Login(verifier(), Limiter(None, log=lambda m: None))
    assert not other.who("198.51.100.200", device).known
    same_secret = Login(
        verifier(), Limiter(None, log=lambda m: None), device_secret=auth.device_secret
    )
    assert same_secret.who("198.51.100.200", device).known, "a restart keeps the secret"
    token = granted(auth, "198.51.100.200", device).token
    auth.reset(verifier("another password!"), b"n" * 32)
    assert not auth.who("198.51.100.200", device).known
    assert auth.session(token) is None
    refused(401, lambda: granted(auth, "198.51.100.200"))


def test_a_flood_of_challenges_leaves_room_for_a_known_browser() -> None:
    auth, _, _ = make()
    device = granted(auth, "198.51.100.200").device
    for n in range(login.MAX_NONCES):
        auth.challenge(f"2001:db8:{n // 256}:{n % 256}::1")
    refused(503, auth.challenge, "203.0.113.50")
    granted(auth, "198.51.100.200", device)


def test_unblock_forgets_every_failure_and_every_challenge(tmp_path: Path) -> None:
    record = tmp_path / "dashboard-logins.json"
    auth, _, _ = make(record)
    for n in range(login.FAILS_ALL // login.FAILS_PER_ADDRESS):
        fail(auth, f"10.9.{n}.1", login.FAILS_PER_ADDRESS)
    refused(429, auth.challenge, "203.0.113.50")
    auth.unblock()
    log_in(auth, "203.0.113.50")
    assert make(record)[0].limiter.wait(login.Who("10.9.0.1")) == 0, "and the record with them"


def test_the_device_secret_is_made_once_and_kept_private(tmp_path: Path) -> None:
    path = tmp_path / "dashboard-device.key"
    old_mask = os.umask(0)
    try:
        first = login.load_secret(path)
    finally:
        os.umask(old_mask)
    assert len(first) == login.SECRET_BYTES and stat.S_IMODE(path.stat().st_mode) == 0o600
    assert login.load_secret(path) == first
    path.write_bytes(b"short")
    assert login.load_secret(path) != first and len(path.read_bytes()) == login.SECRET_BYTES


def test_the_verifier_is_given_to_its_owner_before_it_takes_the_old_ones_place(
    tmp_path: Path,
) -> None:
    path = tmp_path / "dashboard.key"
    path.write_text("old")
    seen: list[tuple[str, bool]] = []
    login.write_verifier(
        path, verifier(), before=lambda tmp: seen.append((path.read_text(), tmp.exists()))
    )
    assert seen == [("old", True)]

    def cut_off(tmp: Path) -> None:
        raise OSError("chown failed")

    with pytest.raises(OSError):
        login.write_verifier(path, verifier(), before=cut_off)
    assert login.load_verifier(path) is not None, "the earlier one stays, whole"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["dashboard.key"]
