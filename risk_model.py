from __future__ import annotations

from typing import Any


def _num(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def build_safety_profile(
    security: Any,
    rugcheck: Any,
    overview: Any,
) -> dict[str, Any]:
    security = security if isinstance(security, dict) else {}
    rugcheck = rugcheck if isinstance(rugcheck, dict) else {}
    overview = overview if isinstance(overview, dict) else {}

    risk = 4.0
    evidence = 0
    checks: list[dict[str, Any]] = []

    def add_check(
        name: str,
        status: str,
        detail: str,
        weight: float = 0.0,
        confidence: float = 0.0,
    ) -> None:
        nonlocal risk, evidence
        if weight:
            risk += weight
        if confidence:
            evidence += confidence
        checks.append({
            "name": name,
            "status": status,
            "detail": detail,
        })

    rc_score = _num(rugcheck.get("score_normalised"))
    if rc_score is not None:
        rc_score = max(0.0, min(100.0, rc_score))
        risk += rc_score * 0.45
        evidence += 24

        rc_status = "safe" if rc_score < 15 else "warn" if rc_score < 35 else "danger"
        checks.append({
            "name": "RugCheck aggregate",
            "status": rc_status,
            "detail": f"RugCheck normalized risk score: {rc_score:.0f}/100.",
        })

    rc_risks = rugcheck.get("risks") or []
    rc_danger = 0
    rc_warn = 0
    for item in rc_risks:
        if not isinstance(item, dict):
            continue
        level = str(item.get("level") or "").lower()
        if level in {"danger", "critical", "high"}:
            rc_danger += 1
        elif level in {"warn", "warning", "medium"}:
            rc_warn += 1

    if rc_risks:
        risk += min(18.0, rc_danger * 6 + rc_warn * 2)
        evidence += 8

    if rugcheck.get("lpLockedPct") is not None:
        lp_locked = max(
            0.0,
            min(100.0, _num(rugcheck.get("lpLockedPct")) or 0.0),
        )
        if lp_locked <= 0:
            add_check(
                "Liquidity lock",
                "danger",
                "No LP lock was reported.",
                weight=10,
                confidence=6,
            )
        elif lp_locked < 50:
            add_check(
                "Liquidity lock",
                "warn",
                f"Only {lp_locked:.0f}% of reported LP is locked.",
                weight=5,
                confidence=6,
            )
        else:
            add_check(
                "Liquidity lock",
                "safe",
                f"{lp_locked:.0f}% of reported LP is locked.",
                confidence=6,
            )

    if security.get("state") == "READY":
        evidence += 16

        if security.get("mint_authority"):
            add_check(
                "Mint authority",
                "danger",
                "Mint authority is active, so additional supply can still be created.",
                weight=18,
                confidence=6,
            )
        else:
            add_check(
                "Mint authority",
                "safe",
                "Mint authority is off.",
                confidence=6,
            )

        if security.get("freeze_authority"):
            add_check(
                "Freeze authority",
                "danger",
                "Freeze authority is active and can restrict token accounts.",
                weight=14,
                confidence=6,
            )
        else:
            add_check(
                "Freeze authority",
                "safe",
                "Freeze authority is off.",
                confidence=6,
            )

        coverage = _num(security.get("coverage_ratio"))
        if coverage is not None:
            coverage_pct = max(0.0, min(100.0, coverage * 100))
            if coverage_pct >= 95:
                add_check(
                    "Holder data",
                    "safe",
                    f"{coverage_pct:.0f}% of reported supply was sampled.",
                    confidence=8,
                )
            elif coverage_pct >= 70:
                add_check(
                    "Holder data",
                    "warn",
                    f"Only {coverage_pct:.0f}% of reported supply was sampled.",
                    weight=3,
                    confidence=4,
                )

        top = _num(security.get("top_holder_share"))
        top10 = _num(security.get("top10_holder_share"))

        if top is not None:
            top_pct = max(0.0, min(100.0, top * 100))
            if top_pct > 50:
                add_check(
                    "Largest holder",
                    "danger",
                    f"One wallet controls about {top_pct:.1f}% of supply.",
                    weight=18,
                )
            elif top_pct > 25:
                add_check(
                    "Largest holder",
                    "warn",
                    f"One wallet controls about {top_pct:.1f}% of supply.",
                    weight=9,
                )
            else:
                add_check(
                    "Largest holder",
                    "safe",
                    f"Largest sampled holder is about {top_pct:.1f}% of supply.",
                )

        if top10 is not None:
            top10_pct = max(0.0, min(100.0, top10 * 100))
            if top10_pct > 70:
                add_check(
                    "Top 10 concentration",
                    "danger",
                    f"Top 10 wallets control about {top10_pct:.1f}% of supply.",
                    weight=16,
                )
            elif top10_pct > 50:
                add_check(
                    "Top 10 concentration",
                    "warn",
                    f"Top 10 wallets control about {top10_pct:.1f}% of supply.",
                    weight=8,
                )
            else:
                add_check(
                    "Top 10 concentration",
                    "safe",
                    f"Top 10 wallets control about {top10_pct:.1f}% of supply.",
                )

    if security.get("state") != "READY":
        holders = rugcheck.get("topHolders") or []
        holder_pcts = []

        for holder in holders:
            if isinstance(holder, dict):
                pct = _num(holder.get("pct"))
                if pct is not None:
                    holder_pcts.append(max(0.0, pct))

        if holder_pcts:
            top10_pct = sum(holder_pcts[:10])

            if top10_pct > 70:
                risk += 16
                checks.append({
                    "name": "Top 10 concentration",
                    "status": "danger",
                    "detail": f"RugCheck reports about {top10_pct:.1f}% in its top 10 holders.",
                })
            elif top10_pct > 50:
                risk += 8
                checks.append({
                    "name": "Top 10 concentration",
                    "status": "warn",
                    "detail": f"RugCheck reports about {top10_pct:.1f}% in its top 10 holders.",
                })
            else:
                checks.append({
                    "name": "Top 10 concentration",
                    "status": "safe",
                    "detail": f"RugCheck reports about {top10_pct:.1f}% in its top 10 holders.",
                })

            evidence += 8

    extensions = [
        str(x).lower()
        for x in (security.get("token_extensions") or [])
        if x
    ]
    dangerous_extensions = {
        "permanentdelegate",
        "transferhook",
        "defaultaccountstate",
        "confidentialtransfermint",
        "nontransferable",
    }

    if extensions:
        hits = [x for x in extensions if x in dangerous_extensions]
        if hits:
            pretty = ", ".join(sorted(set(hits)))
            add_check(
                "Token controls",
                "danger",
                f"Special token controls detected: {pretty}.",
                weight=min(20, 7 * len(set(hits))),
                confidence=5,
            )
        else:
            add_check(
                "Token controls",
                "safe",
                "No high-impact Token-2022 control extensions were detected.",
                confidence=5,
            )

    liquidity = _num(
        overview.get("liquidity")
        if overview.get("liquidity") is not None
        else overview.get("liquidityUsd")
    )
    market_cap = _num(overview.get("marketCap"))

    if liquidity is not None and liquidity > 0:
        evidence += 8
        if liquidity < 2000:
            add_check(
                "Liquidity",
                "danger",
                "Reported liquidity is $" + f"{liquidity:,.0f}" + ".",
                weight=12,
                confidence=5,
            )
        elif liquidity < 10000:
            add_check(
                "Liquidity",
                "warn",
                "Reported liquidity is $" + f"{liquidity:,.0f}" + ".",
                weight=5,
                confidence=5,
            )
        else:
            add_check(
                "Liquidity",
                "safe",
                "Reported liquidity is $" + f"{liquidity:,.0f}" + ".",
                confidence=5,
            )

        if market_cap and market_cap > 0:
            ratio = market_cap / liquidity
            if ratio > 150:
                add_check(
                    "Liquidity depth",
                    "warn",
                    f"Market cap is about {ratio:.0f}× reported liquidity.",
                    weight=5,
                )

    creator_balance = _num(rugcheck.get("creatorBalance"))
    supply = _num((rugcheck.get("token") or {}).get("supply"))

    if creator_balance is not None and supply and supply > 0:
        creator_pct = max(
            0.0,
            min(100.0, creator_balance / supply * 100),
        )
        evidence += 6

        if creator_pct > 10:
            add_check(
                "Creator holding",
                "danger",
                f"Creator still controls about {creator_pct:.1f}% of supply.",
                weight=12,
                confidence=5,
            )
        elif creator_pct > 5:
            add_check(
                "Creator holding",
                "warn",
                f"Creator still controls about {creator_pct:.1f}% of supply.",
                weight=6,
                confidence=5,
            )
        else:
            add_check(
                "Creator holding",
                "safe",
                f"Creator controls about {creator_pct:.1f}% of supply.",
                confidence=5,
            )

    insider_networks = rugcheck.get("insiderNetworks") or []

    if insider_networks:
        sizes = []
        for network in insider_networks:
            if isinstance(network, dict):
                size = _num(network.get("size"))
                if size is not None:
                    sizes.append(size)

        largest_network = max(sizes, default=0)

        detail = (
            f"RugCheck reports {len(insider_networks)} connected "
            "insider/trading network(s)."
        )
        if largest_network:
            detail += f" Largest reported size: {largest_network:.0f}."

        add_check(
            "Insider network",
            "warn",
            detail,
            weight=min(12, 4 + len(insider_networks) * 2),
            confidence=5,
        )

    token_meta = rugcheck.get("tokenMeta") or {}
    if token_meta.get("mutable") is True:
        add_check(
            "Metadata",
            "warn",
            "Token metadata is still mutable.",
            weight=3,
            confidence=3,
        )

    confidence = int(max(0, min(100, evidence)))
    risk_pct = int(round(max(0, min(99, risk))))
    safety_pct = 100 - risk_pct

    if risk_pct <= 14:
        status = "LOW RUG RISK"
    elif risk_pct <= 29:
        status = "GUARDED"
    elif risk_pct <= 49:
        status = "HIGH RISK"
    else:
        status = "VERY HIGH RISK"

    danger_count = sum(
        1 for c in checks
        if c.get("status") == "danger"
    )
    warn_count = sum(
        1 for c in checks
        if c.get("status") == "warn"
    )

    if danger_count:
        status_detail = (
            f"{danger_count} major red flag(s) detected"
            + (
                f" and {warn_count} caution(s)."
                if warn_count else "."
            )
        )
    elif warn_count:
        status_detail = (
            f"{warn_count} caution(s); "
            "no major hard-control red flag detected."
        )
    else:
        status_detail = (
            "No major hard-control red flag detected "
            "in available data."
        )

    return {
        "safety_percent": safety_pct,
        "rug_risk_percent": risk_pct,
        "confidence_percent": confidence,
        "status": status,
        "status_detail": status_detail,
        "checks": checks,
        "sources": {
            "helius": security.get("state") == "READY",
            "rugcheck": bool(rugcheck),
        },
        "method": (
            "Heuristic risk estimate combining Helius on-chain controls, "
            "holder concentration, market/liquidity data, and RugCheck findings. "
            "The percentage is an estimate, not a statistically calibrated probability."
        ),
    }
