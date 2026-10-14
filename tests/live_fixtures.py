"""Deterministic fixtures for the live monitor tests.

These are CONSTRUCTED payloads that follow the documented mempool.space / Esplora schemas
(mempool backend ``mempool.interfaces.ts`` and ``esplora-api.interface.ts``). They are not
real network observations and no test claims they are.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

_BECH32 = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


def _polymod(values) -> int:
    gen = (0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3)
    chk = 1
    for v in values:
        top = chk >> 25
        chk = (chk & 0x1FFFFFF) << 5 ^ v
        for i in range(5):
            chk ^= gen[i] if (top >> i) & 1 else 0
    return chk


def p2wpkh_address(seed: int | str) -> str:
    """A VALID mainnet P2WPKH address (BIP173) derived from a seed. Constructed for tests:
    nobody holds keys for these programs; they are not real users' addresses."""
    program = hashlib.sha256(f"chainwatch-fixture-addr-{seed}".encode()).digest()[:20]
    acc = bits = 0
    data = [0]
    for b in program:
        acc = (acc << 8) | b
        bits += 8
        while bits >= 5:
            bits -= 5
            data.append((acc >> bits) & 31)
    if bits:
        data.append((acc << (5 - bits)) & 31)
    hrp = "bc"
    values = [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp] + data
    mod = _polymod([*values, 0, 0, 0, 0, 0, 0]) ^ 1
    checksum = [(mod >> 5 * (5 - i)) & 31 for i in range(6)]
    return hrp + "1" + "".join(_BECH32[d] for d in data + checksum)


def txid(i: int | str) -> str:
    return hashlib.sha256(f"chainwatch-fixture-{i}".encode()).hexdigest()


def delta(added, *, seq=1, removed=(), replaced=()) -> str:
    return json.dumps(
        {
            "mempool-txids": {
                "sequence": seq,
                "added": list(added),
                "removed": list(removed),
                "mined": [],
                "replaced": [{"replaced": a, "by": b} for a, b in replaced],
            }
        }
    )


def esplora_tx(
    tid: str,
    *,
    n_in: int = 1,
    out_values=(70_000, 29_000),
    fee: int = 1_000,
    weight: int = 561,
    size: int = 222,
    version: int = 2,
    locktime: int = 0,
    sequence: int = 0xFFFFFFFF,
    op_return: bool = False,
    witness: bool = True,
    confirmed: bool = False,
    out_addresses: list[str] | None = None,
    in_addresses: list[str] | None = None,
    parents: list[str] | None = None,
) -> dict[str, Any]:
    vin = [
        {
            "txid": (parents or [])[k]
            if parents and k < len(parents)
            else txid(f"{tid}-parent-{k}"),
            "vout": k,
            "is_coinbase": False,
            "scriptsig": "",
            "scriptsig_asm": "",
            "witness": ["30440220aa", "02bb"] if witness else [],
            "sequence": sequence,
            "prevout": {
                "scriptpubkey": "0014aa",
                "scriptpubkey_asm": "OP_0 OP_PUSHBYTES_20 aa",
                "scriptpubkey_type": "v0_p2wpkh",
                "scriptpubkey_address": (in_addresses or [])[k]
                if in_addresses and k < len(in_addresses)
                else p2wpkh_address(f"{tid}-in-{k}"),
                "value": (sum(out_values) + fee) // n_in + (k < (sum(out_values) + fee) % n_in),
            },
        }
        for k in range(n_in)
    ]
    vout = [
        {
            "scriptpubkey": "0014bb",
            "scriptpubkey_asm": "OP_0 OP_PUSHBYTES_20 bb",
            "scriptpubkey_type": "v0_p2wpkh",
            "scriptpubkey_address": (out_addresses or [])[j]
            if out_addresses and j < len(out_addresses)
            else p2wpkh_address(f"{tid}-out-{j}"),
            "value": v,
        }
        for j, v in enumerate(out_values)
    ]
    if op_return:
        vout.append(
            {
                "scriptpubkey": "6a04",
                "scriptpubkey_asm": "OP_RETURN",
                "scriptpubkey_type": "op_return",
                "value": 0,
            }
        )
    status = {"confirmed": confirmed}
    if confirmed:
        status.update(
            {"block_height": 900_000, "block_hash": "00" * 32, "block_time": 1_790_000_000}
        )
    return {
        "txid": tid,
        "version": version,
        "locktime": locktime,
        "size": size,
        "weight": weight,
        "fee": fee,
        "sigops": 1,
        "vin": vin,
        "vout": vout,
        "status": status,
    }
