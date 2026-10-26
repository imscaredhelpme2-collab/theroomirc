"""Encrypt the IRC passwords that are kept in the database (standard library only).

Construction: a key is derived from the app's secret key; HMAC-SHA256 in counter mode produces a keystream
(a standard PRF-based stream cipher) and an HMAC-SHA256 tag over nonce + ciphertext is checked before anything is
decrypted (encrypt-then-MAC). A fresh random nonce is used for every value.

What this protects: someone who copies only chat.db (or a backup of it) cannot read the passwords.
What it does NOT protect: someone who also has the secret key (database/secret.key or ROOM_SECRET), or who
controls the running server. Set ROOM_SECRET in the environment if you want the key kept away from the data folder.
"""
import base64
import hashlib
import hmac
import os

PREFIX = "v1:"


def _keys(secret):
    master = hashlib.sha256(b"newroom-irc-credentials|" + str(secret).encode("utf-8")).digest()
    return (hmac.new(master, b"encrypt", hashlib.sha256).digest(),
            hmac.new(master, b"authenticate", hashlib.sha256).digest())


def _keystream(key, nonce, n):
    out, counter = bytearray(), 0
    while len(out) < n:
        out += hmac.new(key, nonce + counter.to_bytes(8, "big"), hashlib.sha256).digest()
        counter += 1
    return bytes(out[:n])


def encrypt(secret, plaintext):
    if plaintext == "" or plaintext is None:
        return ""
    enc_key, mac_key = _keys(secret)
    data = plaintext.encode("utf-8")
    nonce = os.urandom(16)
    ct = bytes(a ^ b for a, b in zip(data, _keystream(enc_key, nonce, len(data))))
    tag = hmac.new(mac_key, PREFIX.encode() + nonce + ct, hashlib.sha256).digest()
    return PREFIX + base64.urlsafe_b64encode(nonce + ct + tag).decode("ascii")


def decrypt(secret, token):
    """Returns the plaintext, or None if the token is missing, damaged, or was made with a different key."""
    if not token or not token.startswith(PREFIX):
        return None
    try:
        raw = base64.urlsafe_b64decode(token[len(PREFIX):].encode("ascii"))
    except (ValueError, TypeError):
        return None
    if len(raw) < 16 + 32:
        return None
    nonce, ct, tag = raw[:16], raw[16:-32], raw[-32:]
    enc_key, mac_key = _keys(secret)
    want = hmac.new(mac_key, PREFIX.encode() + nonce + ct, hashlib.sha256).digest()
    if not hmac.compare_digest(tag, want):
        return None
    try:
        return bytes(a ^ b for a, b in zip(ct, _keystream(enc_key, nonce, len(ct)))).decode("utf-8")
    except UnicodeDecodeError:
        return None
