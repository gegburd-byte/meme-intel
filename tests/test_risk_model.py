from risk_model import build_safety_profile


def test_safety_profile_flags_active_authorities_and_concentration():
    profile = build_safety_profile(
        security={
            "state": "READY",
            "mint_authority": True,
            "freeze_authority": True,
            "coverage_ratio": 1.0,
            "top_holder_share": 0.60,
            "top10_holder_share": 0.80,
            "token_extensions": ["transferHook"],
        },
        rugcheck={
            "score_normalised": 52,
            "risks": [{"level": "danger", "name": "test"}],
            "creatorBalance": 300_000_000,
            "token": {"supply": 1_000_000_000},
        },
        overview={"liquidity": 1_000},
    )

    assert profile["rug_risk_percent"] >= 70
    assert profile["safety_percent"] <= 30
    assert profile["confidence_percent"] >= 60
    assert any(x["name"] == "Mint authority" and x["status"] == "danger" for x in profile["checks"])
    assert any(x["name"] == "Token controls" and x["status"] == "danger" for x in profile["checks"])


def test_safety_profile_does_not_treat_missing_liquidity_as_automatic_rug():
    profile = build_safety_profile(
        security={
            "state": "READY",
            "mint_authority": False,
            "freeze_authority": False,
            "coverage_ratio": 1.0,
            "top_holder_share": 0.02,
            "top10_holder_share": 0.12,
            "token_extensions": [],
        },
        rugcheck={},
        overview={},
    )

    assert profile["rug_risk_percent"] < 30
    assert profile["safety_percent"] > 70
    assert not any(x["name"] == "Liquidity" and x["status"] == "danger" for x in profile["checks"])


def test_sell_route_is_positive_evidence():
    profile = build_safety_profile(
        security={
            "state": "READY",
            "mint_authority": False,
            "freeze_authority": False,
            "coverage_ratio": 1.0,
            "top_holder_share": 0.02,
            "top10_holder_share": 0.12,
            "token_extensions": [],
        },
        rugcheck={},
        overview={"liquidity": 50000},
        sell_probe={"state": "ROUTE_FOUND"},
    )

    assert any(x["name"] == "Sell route" and x["status"] == "safe" for x in profile["checks"])
