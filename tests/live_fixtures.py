"""Deterministic fixtures for the live monitor tests.

These are CONSTRUCTED payloads that follow the documented mempool.space / Esplora schemas
(mempool backend ``mempool.interfaces.ts`` and ``esplora-api.interface.ts``). They are not
real network observations and no test claims they are.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


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
) -> dict[str, Any]:
    vin = [
        {
            "txid": txid(f"{tid}-parent-{k}"),
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
                "scriptpubkey_address": "bc1qfixture",
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
            "scriptpubkey_address": f"bc1qfixtureout{j}",
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
