"""On-chain integration for Polymarket via the Polygon network.

Polymarket positions are ERC-1155 ConditionalToken balances on Polygon,
clearable by the CTF (Conditional Tokens Framework) contract. This module
provides:

- A minimal async JSON-RPC client (aiohttp) for read-only Polygon access.
- A decoder for the CTF events we care about (Transfer / TransferSingle).
- A wallet tracker that scores wallets by realised PnL on resolved markets
  and detects new large positions in real time.

Nothing here moves funds or signs transactions. The on-chain layer is
strictly read-only. The output is signals — same fail-closed envelope as
the rest of the research signals.
"""
