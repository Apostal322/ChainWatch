"""Observation-time features of a live Bitcoin transaction (Esplora ``GET /api/tx/:txid``).

Semantics that matter:
* Output values are listed individually. Their sum is NOT a payment amount: outputs
  usually include change returned to the sender, and nothing here can tell which.
* Input references (previous txid:vout) are recorded as observable links between
  transactions. They say nothing about who owns which address; no wallet clustering or
  identity inference is done anywhere in ChainWatch.
* Only data available when the details are fetched is used. ``confirmed_at_fetch`` is the
  status at fetch time; later confirmations or replacements are never written back into
  the features.

``indicators`` are deterministic, rule-based flags for unusual *observable properties*.
They are not risk scores, fraud predictions or probabilities, and a flag says nothing
about legality. No Elliptic++ model is applied to live data (its features are anonymised
and cannot be computed for new transactions).
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from typing import Any

from chainwatch.live.protocol import valid_txid

DUST_LIMIT_SAT = 546  # conventional P2PKH dust threshold; a heuristic, not consensus
BIP125_MAX_SEQUENCE = 0xFFFFFFFD  # inputs with nSequence <= this signal opt-in RBF
LOCKTIME_THRESHOLD = 500_000_000  # below: block height, at or above: Unix time
MAX_LISTED = 25  # outputs / input refs stored per transaction (memory bound)
FAN_OUT_MIN_OUTPUTS = 50
CONSOLIDATION_MIN_INPUTS = 50
FEE_OUTLIER_FACTOR = 10.0  # fee rate >= 10x the rolling median of recently fetched txs

INDICATORS = {
    "rbf_signal": "Signals BIP125 opt-in replace-by-fee (an input sequence <= 0xfffffffd). "
    "Nodes with full-RBF policies may replace transactions regardless of this signal.",
    "high_fan_out": f"Creates at least {FAN_OUT_MIN_OUTPUTS} outputs (e.g. batch payouts).",
    "consolidation": f"Spends at least {CONSOLIDATION_MIN_INPUTS} inputs into at most two outputs.",
    "dust_output": f"Has a non-OP_RETURN output below {DUST_LIMIT_SAT} sat.",
    "op_return": "Carries an OP_RETURN (data) output.",
    "timelocked": "Sets a non-zero nLockTime.",
    "nonstandard_version": "Transaction version is not 1, 2 or 3.",
    "fee_rate_outlier": f"Fee rate is at least {FEE_OUTLIER_FACTOR:g}x the rolling median of "
    "recently fetched transactions.",
}


class FeatureError(ValueError):
    """The REST payload does not have the documented shape."""


def _int(d: dict[str, Any], key: str, *, required: bool = True) -> int | None:
    v = d.get(key)
    if isinstance(v, bool) or not isinstance(v, int):
        if required:
            raise FeatureError(f"field {key!r} missing or not an integer")
        return None
    return v


def extract(tx: Any, *, txid: str, first_seen: float, fetched_at: float) -> dict[str, Any]:
    """Map one Esplora transaction to a flat feature record. Raises ``FeatureError``."""
    if not isinstance(tx, dict):
        raise FeatureError("transaction payload is not an object")
    if tx.get("txid") != txid:
        raise FeatureError("payload txid does not match the requested txid")
    vin, vout = tx.get("vin"), tx.get("vout")
    if not isinstance(vin, list) or not isinstance(vout, list) or not vin or not vout:
        raise FeatureError("vin/vout missing or empty")
    status = tx.get("status") if isinstance(tx.get("status"), dict) else {}

    size = _int(tx, "size")
    weight = _int(tx, "weight")
    fee = _int(tx, "fee")
    version = _int(tx, "version")
    locktime = _int(tx, "locktime")
    if size <= 0 or weight <= 0 or fee < 0:
        raise FeatureError("non-positive size/weight or negative fee")
    vsize = math.ceil(weight / 4)  # BIP141 virtual size

    out_values: list[int] = []
    out_types: dict[str, int] = {}
    op_return = dust = 0
    for o in vout:
        if not isinstance(o, dict) or not isinstance(o.get("value"), int):
            raise FeatureError("malformed output")
        kind = str(o.get("scriptpubkey_type") or "unknown")
        out_types[kind] = out_types.get(kind, 0) + 1
        out_values.append(o["value"])
        if kind == "op_return":
            op_return += 1
        elif o["value"] < DUST_LIMIT_SAT:
            dust += 1

    refs: list[str] = []
    in_values: list[int] = []
    in_types: dict[str, int] = {}
    rbf = segwit = False
    parents: set[str] = set()
    for i in vin:
        if not isinstance(i, dict):
            raise FeatureError("malformed input")
        seq = i.get("sequence")
        if isinstance(seq, int) and seq <= BIP125_MAX_SEQUENCE:
            rbf = True
        if isinstance(i.get("witness"), list) and i["witness"]:
            segwit = True
        prev_txid, prev_vout = i.get("txid"), i.get("vout")
        if valid_txid(prev_txid) and isinstance(prev_vout, int):
            parents.add(prev_txid)
            if len(refs) < MAX_LISTED:
                refs.append(f"{prev_txid}:{prev_vout}")
        prevout = i.get("prevout")
        if isinstance(prevout, dict) and isinstance(prevout.get("value"), int):
            in_values.append(prevout["value"])
            kind = str(prevout.get("scriptpubkey_type") or "unknown")
            in_types[kind] = in_types.get(kind, 0) + 1

    n_in, n_out = len(vin), len(vout)
    locktime_kind = (
        "none" if locktime == 0 else ("height" if locktime < LOCKTIME_THRESHOLD else "time")
    )
    return {
        "txid": txid,
        "first_seen": first_seen,
        "fetched_at": fetched_at,
        "confirmed_at_fetch": bool(status.get("confirmed", False)),
        "version": version,
        "locktime": locktime,
        "locktime_kind": locktime_kind,
        "size": size,
        "weight": weight,
        "vsize": vsize,
        "fee": fee,
        "fee_rate": round(fee / vsize, 3),
        "n_inputs": n_in,
        "n_outputs": n_out,
        "n_parent_txs": len(parents),
        "segwit": segwit,
        "rbf_signal": rbf,
        # sum of known input values; None unless every prevout value is present
        "input_value_total": sum(in_values) if len(in_values) == n_in else None,
        # sum of all outputs INCLUDING any change: not a payment amount
        "output_value_total": sum(out_values),
        "max_output_value": max(out_values),
        "output_values": out_values[:MAX_LISTED],
        "output_types": out_types,
        "input_types": in_types,
        "input_refs": refs,
        "n_op_return": op_return,
        "n_dust_outputs": dust,
    }


def indicators(f: dict[str, Any], recent_fee_rates: Iterable[float] = ()) -> list[str]:
    """Rule-based observable-property flags (see ``INDICATORS``); deterministic."""
    flags = []
    if f["rbf_signal"]:
        flags.append("rbf_signal")
    if f["n_outputs"] >= FAN_OUT_MIN_OUTPUTS:
        flags.append("high_fan_out")
    if f["n_inputs"] >= CONSOLIDATION_MIN_INPUTS and f["n_outputs"] <= 2:
        flags.append("consolidation")
    if f["n_dust_outputs"]:
        flags.append("dust_output")
    if f["n_op_return"]:
        flags.append("op_return")
    if f["locktime"]:
        flags.append("timelocked")
    if f["version"] not in (1, 2, 3):
        flags.append("nonstandard_version")
    rates = sorted(recent_fee_rates)
    if len(rates) >= 20:
        median = rates[len(rates) // 2]
        if median > 0 and f["fee_rate"] >= FEE_OUTLIER_FACTOR * median:
            flags.append("fee_rate_outlier")
    return flags
