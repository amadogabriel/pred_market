"""Decoders for Polymarket Conditional Tokens Framework (CTF) events on Polygon.

The CTF is an ERC-1155 contract; the events we care about are TransferSingle
and TransferBatch. A "position" change on a Polymarket market corresponds to
the wallet's CTF balance for that market's outcome token id moving.

We decode the minimum needed for wallet/token attribution. We do not parse
full TransferBatch arrays here — when we see one we emit one event per
(token_id, value) pair via `decode_batch`.

The canonical CTF contract address on Polygon, at writing, is
`0x4D97DCd97eC945f40cF65F87097ACe5EA0476045`. Configurable via env var.

References:
- ERC-1155 TransferSingle topic: keccak256("TransferSingle(address,address,address,uint256,uint256)")
  = 0xc3d58168c5ae7397731d063d5bbf3d657854427343f4c083240f7aacaa2d0f62
- TransferBatch topic:
  = 0x4a39dc06d4c0dbc64b70af90fd698a233a518aa5d07e595d983b8c0526c8f7fb
"""
from __future__ import annotations

from dataclasses import dataclass

from pm.onchain.polygon_rpc import decode_address, decode_uint

TOPIC_TRANSFER_SINGLE = "0xc3d58168c5ae7397731d063d5bbf3d657854427343f4c083240f7aacaa2d0f62"
TOPIC_TRANSFER_BATCH = "0x4a39dc06d4c0dbc64b70af90fd698a233a518aa5d07e595d983b8c0526c8f7fb"


@dataclass(frozen=True)
class CtfTransfer:
    block_number: int
    tx_hash: str
    operator: str    # 0x...
    from_addr: str   # 0x... (zero address = mint)
    to_addr: str     # 0x... (zero address = burn)
    token_id: int    # CTF position id (large uint)
    value: int       # ERC-1155 amount, in raw token units (CTF uses 6 decimals on USDC-backed)

    @property
    def is_mint(self) -> bool:
        return self.from_addr == "0x" + "0" * 40

    @property
    def is_burn(self) -> bool:
        return self.to_addr == "0x" + "0" * 40

    @property
    def is_trade(self) -> bool:
        """Wallet-to-wallet (not mint/burn) — closest to a real position transfer."""
        return not self.is_mint and not self.is_burn


def decode_single(log: dict) -> CtfTransfer | None:
    """Decode an ERC-1155 TransferSingle log into a CtfTransfer.

    Returns None if topic[0] does not match.
    """
    topics = log.get("topics") or []
    if not topics or topics[0].lower() != TOPIC_TRANSFER_SINGLE:
        return None
    if len(topics) < 4:
        return None
    data = log.get("data") or "0x"
    raw = data.removeprefix("0x")
    if len(raw) < 128:
        return None
    token_id = int(raw[:64], 16)
    value = int(raw[64:128], 16)
    return CtfTransfer(
        block_number=int(log.get("blockNumber", "0x0"), 16) if isinstance(log.get("blockNumber"), str) else int(log.get("blockNumber") or 0),
        tx_hash=str(log.get("transactionHash", "")),
        operator=decode_address(topics[1]),
        from_addr=decode_address(topics[2]),
        to_addr=decode_address(topics[3]),
        token_id=token_id,
        value=value,
    )


def decode_batch(log: dict) -> list[CtfTransfer]:
    """Decode an ERC-1155 TransferBatch log into a list of CtfTransfers.

    Returns empty list if topic[0] does not match.
    """
    topics = log.get("topics") or []
    if not topics or topics[0].lower() != TOPIC_TRANSFER_BATCH:
        return []
    if len(topics) < 4:
        return []
    data = log.get("data") or "0x"
    raw = data.removeprefix("0x")
    # Layout: offset_to_ids (32), offset_to_values (32), ids_len (32), ids..., values_len (32), values...
    if len(raw) < 256:
        return []
    ids_off = int(raw[:64], 16) * 2
    vals_off = int(raw[64:128], 16) * 2
    ids_len = int(raw[ids_off:ids_off + 64], 16)
    vals_len = int(raw[vals_off:vals_off + 64], 16)
    if ids_len != vals_len:
        return []
    ids = [int(raw[ids_off + 64 + i * 64:ids_off + 64 + (i + 1) * 64], 16) for i in range(ids_len)]
    vals = [int(raw[vals_off + 64 + i * 64:vals_off + 64 + (i + 1) * 64], 16) for i in range(vals_len)]
    block_num = int(log.get("blockNumber", "0x0"), 16) if isinstance(log.get("blockNumber"), str) else int(log.get("blockNumber") or 0)
    return [CtfTransfer(
        block_number=block_num,
        tx_hash=str(log.get("transactionHash", "")),
        operator=decode_address(topics[1]),
        from_addr=decode_address(topics[2]),
        to_addr=decode_address(topics[3]),
        token_id=tid, value=val) for tid, val in zip(ids, vals)]
