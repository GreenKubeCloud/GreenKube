"""scrypt password hashing (roadmap F14)."""

from greenkube.auth.passwords import (
    SCRYPT_N,
    ScryptParameters,
    generate_password,
    hash_password,
    needs_rehash,
    validate_new_password,
    verify_password,
)


def test_hash_is_salted_self_describing_and_verifies():
    first = hash_password("correct horse battery staple")
    second = hash_password("correct horse battery staple")

    assert first != second
    assert first.startswith(f"scrypt${SCRYPT_N}$8$1$")
    assert "correct horse" not in first
    assert verify_password("correct horse battery staple", first)
    assert not verify_password("correct horse battery stapler", first)


def test_malformed_hashes_never_verify():
    for encoded in ("", "plain", "bcrypt$1$2$3$4$5", "scrypt$x$8$1$salt$hash"):
        assert not verify_password("anything", encoded)


def test_weaker_parameters_are_flagged_for_rehash():
    weak = hash_password("password-12345", ScryptParameters(n=2**10))

    assert verify_password("password-12345", weak)
    assert needs_rehash(weak)
    assert not needs_rehash(hash_password("password-12345"))


def test_password_policy():
    assert validate_new_password("short") is not None
    assert validate_new_password("x" * 300) is not None
    assert validate_new_password("administrator", username="administrator") is not None
    assert validate_new_password("long-enough-password", username="admin") is None


def test_generated_passwords_are_random_and_alphanumeric():
    passwords = {generate_password() for _ in range(20)}

    assert len(passwords) == 20
    assert all(len(value) == 24 and value.isalnum() for value in passwords)
