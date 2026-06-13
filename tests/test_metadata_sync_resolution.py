from __future__ import annotations

import json

from pm.core import db
from pm.ingestion.metadata_sync import parse_market


def test_parse_market_stores_resolution_metadata(tmp_path):
    market = parse_market(
        {
            "conditionId": "M1",
            "question": "Will A win?",
            "slug": "will-a-win",
            "endDate": "2026-06-11T19:00:00Z",
            "active": True,
            "closed": True,
            "acceptingOrders": False,
            "outcomePrices": '["1", "0"]',
            "umaResolutionStatus": "resolved",
            "closedTime": "2026-06-11T23:32:03Z",
            "clobTokenIds": '["YES", "NO"]',
            "liquidity": "1000.5",
            "volume24hr": "12.3",
            "negRisk": True,
        },
        category="sports",
        neg_risk_id="G1",
    )

    assert market is not None
    conn = db.connect(tmp_path / "state.db")
    db.upsert_market(conn, market)
    row = conn.execute("SELECT * FROM markets WHERE market_id='M1'").fetchone()

    assert row["closed"] == 1
    assert row["accepting_orders"] == 0
    assert json.loads(row["outcome_prices_json"]) == ["1", "0"]
    assert row["resolution_status"] == "resolved"
    assert row["closed_time"] == "2026-06-11T23:32:03Z"
