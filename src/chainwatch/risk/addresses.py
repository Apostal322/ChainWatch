"""Bitcoin mainnet address validation and normalisation (no third-party dependencies).

Screening compares address *strings*, so both sides must be valid and canonical:

* Base58Check (P2PKH version 0x00, P2SH version 0x05): case-sensitive; checksum verified.
* Bech32 (BIP173, witness v0: 20-byte P2WPKH or 32-byte P2WSH) and Bech32m (BIP350,
  witness v1-16, e.g. 32-byte Taproot): case-insensitive; normalised to lower case.

An address identifies a *script*, not a person or organisation. Nothing in ChainWatch
infers ownership from addresses. Outputs without an address (bare multisig, P2PK, OP_RETURN,
non-standard scripts) cannot be screened by address and are counted as such.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_B58_INDEX = {c: i for i, c in enumerate(_B58)}
_BECH32 = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
_BECH32_INDEX = {c: i for i, c in enumerate(_BECH32)}
_BECH32_CONST, _BECH32M_CONST = 1, 0x2BC830A3
MAX_ADDRESS_LEN = 90  # BIP173 limit; anything longer is not an address


@dataclass(frozen=True)
class Address:
    value: str  # canonical form (bech32 lower-cased)
    kind: str  # p2pkh | p2sh | p2wpkh | p2wsh | p2tr | witness_v{n}


class InvalidAddress(ValueError):
    """Not a valid Bitcoin mainnet address; the message says why."""


def _b58decode_check(s: str) -> bytes:
    n = 0
    for c in s:
        if c not in _B58_INDEX:
            raise InvalidAddress(f"invalid base58 character {c!r}")
        n = n * 58 + _B58_INDEX[c]
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    raw = b"\x00" * (len(s) - len(s.lstrip("1"))) + raw
    if len(raw) < 5:
        raise InvalidAddress("base58 payload too short")
    body, check = raw[:-4], raw[-4:]
    if hashlib.sha256(hashlib.sha256(body).digest()).digest()[:4] != check:
        raise InvalidAddress("base58check checksum mismatch")
    return body


def _polymod(values: list[int]) -> int:
    gen = (0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3)
    chk = 1
    for v in values:
        top = chk >> 25
        chk = (chk & 0x1FFFFFF) << 5 ^ v
        for i in range(5):
            chk ^= gen[i] if (top >> i) & 1 else 0
    return chk


def _convertbits(data: list[int], frombits: int, tobits: int) -> list[int]:
    acc = bits = 0
    out: list[int] = []
    maxv = (1 << tobits) - 1
    for value in data:
        acc = (acc << frombits) | value
        bits += frombits
        while bits >= tobits:
            bits -= tobits
            out.append((acc >> bits) & maxv)
    if bits >= frombits or ((acc << (tobits - bits)) & maxv):
        raise InvalidAddress("invalid bech32 padding")
    return out


def _segwit(s: str) -> Address:
    if s.lower() != s and s.upper() != s:
        raise InvalidAddress("mixed-case bech32")
    s = s.lower()
    pos = s.rfind("1")
    hrp, data_part = s[:pos], s[pos + 1 :]
    if hrp != "bc":
        raise InvalidAddress(f"not a mainnet bech32 address (hrp {hrp!r})")
    if len(data_part) < 6 or any(c not in _BECH32_INDEX for c in data_part):
        raise InvalidAddress("invalid bech32 data")
    data = [_BECH32_INDEX[c] for c in data_part]
    expanded = [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp]
    const = _polymod(expanded + data)
    version, program = data[0], _convertbits(data[1:-6], 5, 8)
    if version > 16 or not 2 <= len(program) <= 40:
        raise InvalidAddress("invalid witness version or program length")
    if version == 0:
        if const != _BECH32_CONST:
            raise InvalidAddress("bech32 checksum mismatch")
        if len(program) not in (20, 32):
            raise InvalidAddress("invalid v0 witness program length")
        return Address(s, "p2wpkh" if len(program) == 20 else "p2wsh")
    if const != _BECH32M_CONST:
        raise InvalidAddress("bech32m checksum mismatch")
    return Address(s, "p2tr" if version == 1 and len(program) == 32 else f"witness_v{version}")


def parse_address(text: str) -> Address:
    """Validate a Bitcoin mainnet address and return its canonical form. Raises
    ``InvalidAddress`` with the reason (wrong network, bad checksum, not Bitcoin, ...)."""
    if not isinstance(text, str):
        raise InvalidAddress("not a string")
    s = text.strip()
    if not s or len(s) > MAX_ADDRESS_LEN:
        raise InvalidAddress("empty or too long")
    if s[:3].lower() == "bc1":
        return _segwit(s)
    if s[0] in "13":
        body = _b58decode_check(s)
        if len(body) != 21:
            raise InvalidAddress("base58 payload is not 21 bytes")
        if body[0] == 0x00:
            return Address(s, "p2pkh")
        if body[0] == 0x05:
            return Address(s, "p2sh")
        raise InvalidAddress(f"unknown base58 version byte {body[0]:#04x}")
    raise InvalidAddress("not a Bitcoin mainnet address prefix (1, 3 or bc1)")


def canonical(text: str) -> str | None:
    """Canonical form, or None if ``text`` is not a valid mainnet address."""
    try:
        return parse_address(text).value
    except InvalidAddress:
        return None
