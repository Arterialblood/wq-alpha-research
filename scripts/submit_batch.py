"""Batch submit alphas to WorldQuant BRAIN for testing.

Usage:
    cd <skill-dir>
    pyenv exec python scripts/submit_batch.py

Reads credentials from WQ_BRAIN_USERNAME/WQ_BRAIN_PASSWORD or an untracked
credential.txt (JSON array ["username", "password"]).
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import requests
from requests.auth import HTTPBasicAuth

SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
CREDENTIAL_PATH = SKILL_DIR / "credential.txt"
API_BASE = "https://api.worldquantbrain.com"

HEADERS = {
    "Accept": "application/json;version=2.0",
    "Content-Type": "application/json",
}


ALPHAS = [
    # ====== 技术+基本面混合因子（高 decay 控制换手） ======
    # 1. 经典反转+ROE趋势（SKILL 模板 E）
    {
        "expression": "0.5 * rank(-(close / open - 1)) + 0.5 * rank(ts_rank(operating_income / equity, 126))",
        "decay": 12,
        "truncation": 0.08,
        "neutralization": "INDUSTRY",
    },
    # 2. 反转+ROE趋势，更高decay
    {
        "expression": "0.5 * rank(-(close / open - 1)) + 0.5 * rank(ts_rank(operating_income / equity, 126))",
        "decay": 20,
        "truncation": 0.08,
        "neutralization": "INDUSTRY",
    },
    # 3. VWAP偏离+分析师EPS（技术+基本面，不同信号源降低相关）
    {
        "expression": "0.5 * rank(-(close / vwap - 1)) + 0.5 * rank(ts_rank(est_eps / close, 126))",
        "decay": 15,
        "truncation": 0.08,
        "neutralization": "INDUSTRY",
    },
    # 4. 日内反转+FCF收益率
    {
        "expression": "0.5 * rank(-(close / open - 1)) + 0.5 * rank(ts_rank(free_cash_flow_reported_value / equity, 126))",
        "decay": 12,
        "truncation": 0.08,
        "neutralization": "INDUSTRY",
    },
    # 5. 短期动量+ROE趋势（动量替代反转，不同信号逻辑）
    {
        "expression": "0.5 * rank(ts_rank(close / open - 1, 5)) + 0.5 * rank(ts_rank(operating_income / equity, 126))",
        "decay": 10,
        "truncation": 0.08,
        "neutralization": "INDUSTRY",
    },
    # 6. 双基本面+技术三因子混合
    {
        "expression": "0.33 * rank(-(close / open - 1)) + 0.33 * rank(ts_rank(operating_income / equity, 126)) + 0.34 * rank(ts_rank(est_eps / close, 126))",
        "decay": 15,
        "truncation": 0.08,
        "neutralization": "INDUSTRY",
    },
    # 7. 成交量加权反转+ROE（加入volume过滤，减少噪音交易）
    {
        "expression": "0.5 * rank(-(close / open - 1) * volume) + 0.5 * rank(ts_rank(operating_income / equity, 126))",
        "decay": 15,
        "truncation": 0.08,
        "neutralization": "INDUSTRY",
    },
    # 8. 日内振幅反转+分析师（不同技术信号）
    {
        "expression": "0.5 * rank(-(high / low - 1)) + 0.5 * rank(ts_rank(est_eps / close, 126))",
        "decay": 18,
        "truncation": 0.08,
        "neutralization": "INDUSTRY",
    },
]


def load_credentials() -> tuple[str, str]:
    env_user = os.getenv("WQ_BRAIN_USERNAME")
    env_password = os.getenv("WQ_BRAIN_PASSWORD")
    if env_user and env_password:
        return env_user, env_password

    candidates = [
        CREDENTIAL_PATH,
        Path.cwd() / "credential.txt",
    ]
    for p in candidates:
        if p.exists():
            username, password = json.loads(p.read_text(encoding="utf-8"))
            return str(username), str(password)
    raise FileNotFoundError(
        "BRAIN credentials not found. Set WQ_BRAIN_USERNAME/WQ_BRAIN_PASSWORD "
        'or create an untracked credential.txt with ["your_username", "your_password"].'
    )


def create_session() -> requests.Session:
    username, password = load_credentials()
    session = requests.Session()
    session.auth = HTTPBasicAuth(username, password)
    session.headers.update(HEADERS)
    resp = session.post(f"{API_BASE}/authentication")
    if resp.status_code != 201:
        raise RuntimeError(f"Auth failed: {resp.status_code} {resp.text}")
    print(f"Authenticated: {resp.status_code}")
    return session


def build_payload(expr: str, decay: int, truncation: float, neutralization: str) -> dict:
    return {
        "type": "REGULAR",
        "settings": {
            "instrumentType": "EQUITY",
            "region": "USA",
            "universe": "TOP3000",
            "delay": 1,
            "decay": decay,
            "neutralization": neutralization,
            "truncation": truncation,
            "pasteurization": "ON",
            "unitHandling": "VERIFY",
            "nanHandling": "ON",
            "maxTrade": "OFF",
            "maxPosition": "OFF",
            "language": "FASTEXPR",
            "visualization": False,
        },
        "regular": expr,
    }


def submit_alpha(session: requests.Session, idx: int, alpha: dict) -> dict:
    payload = build_payload(
        alpha["expression"],
        alpha["decay"],
        alpha["truncation"],
        alpha["neutralization"],
    )
    print(f"\n[{idx}] Simulating: {alpha['expression'][:60]}...")
    resp = session.post(f"{API_BASE}/simulations", json=payload)
    print(f"    POST /simulations -> {resp.status_code}")
    if resp.status_code != 201:
        print(f"    Error: {resp.text[:300]}")
        return {"error": resp.text[:500], "status_code": resp.status_code}
    location = resp.headers.get("Location", "")
    sim_id = location.rstrip("/").split("/")[-1]
    print(f"    Simulation ID: {sim_id}")
    return {"simulation_id": sim_id}


def poll_simulation(session: requests.Session, sim_id: str, timeout: int = 600) -> dict:
    print(f"    Polling simulation {sim_id}...")
    start = time.time()
    while time.time() - start < timeout:
        resp = session.get(f"{API_BASE}/simulations/{sim_id}")
        if resp.status_code != 200:
            print(f"    GET /simulations/{sim_id} -> {resp.status_code}")
            time.sleep(8)
            continue
        data = resp.json()
        status = data.get("status", "UNKNOWN")
        print(f"    Sim status: {status}")
        if status == "COMPLETE":
            alpha_id = data.get("alpha")
            print(f"    Alpha ID: {alpha_id}")
            return {"status": "COMPLETE", "alpha_id": alpha_id, "sim_data": data}
        if status in ("ERROR", "FAILED"):
            return {"status": "ERROR", "sim_data": data}
        time.sleep(8)
    return {"status": "TIMEOUT", "simulation_id": sim_id}


def submit_if_passed(session: requests.Session, alpha_id: str) -> dict:
    """Submit alpha and poll until ACTIVE or SELF_CORRELATION result known."""
    print(f"    Submitting alpha {alpha_id}...")
    sub = session.post(f"{API_BASE}/alphas/{alpha_id}/submit")
    print(f"    POST /alphas/{alpha_id}/submit -> {sub.status_code}")
    if sub.status_code not in (200, 201):
        return {"submitted": False, "status_code": sub.status_code, "text": sub.text[:300]}

    for _ in range(30):
        time.sleep(10)
        resp = session.get(f"{API_BASE}/alphas/{alpha_id}")
        if resp.status_code != 200:
            continue
        alpha = resp.json()
        status = alpha.get("status")
        print(f"    Alpha status: {status}")
        if status == "ACTIVE":
            return {"submitted": True, "status": "ACTIVE", "alpha": alpha}
        checks = alpha.get("is", {}).get("checks", [])
        sc = next((c for c in checks if c.get("name") == "SELF_CORRELATION"), {})
        if sc.get("result") == "FAIL":
            return {"submitted": True, "status": alpha.get("status"), "self_correlation": "FAIL", "alpha": alpha}
        if status == "UNSUBMITTED" and sc.get("result") == "PASS":
            # sometimes needs more time to become ACTIVE
            continue
    return {"submitted": True, "status": "PENDING", "alpha": alpha}


def get_alpha_metrics(session: requests.Session, alpha_id: str) -> dict:
    resp = session.get(f"{API_BASE}/alphas/{alpha_id}")
    if resp.status_code == 200:
        return resp.json()
    return {}


def main():
    session = create_session()
    results = []
    for i, alpha in enumerate(ALPHAS, 1):
        try:
            submit_data = submit_alpha(session, i, alpha)
            sim_id = submit_data.get("simulation_id")
            if not sim_id:
                results.append({"idx": i, "expression": alpha["expression"], "submit": submit_data, "sim": None})
                continue
            sim_result = poll_simulation(session, sim_id)
            alpha_id = sim_result.get("alpha_id")
            if not alpha_id:
                results.append({"idx": i, "expression": alpha["expression"], "sim": sim_result})
                continue
            metrics = get_alpha_metrics(session, alpha_id)
            # Auto-submit if fitness looks reasonable
            is_ = metrics.get("is", {})
            fitness = is_.get("fitness", 0)
            sharpe = is_.get("sharpe", 0)
            turnover = is_.get("turnover", 1)
            print(f"    IS metrics: Sharpe={sharpe:.2f}, Fitness={fitness:.2f}, TO={turnover*100:.2f}%")
            if fitness >= 1.0 and sharpe >= 1.25 and turnover <= 0.50:
                sub_result = submit_if_passed(session, alpha_id)
                results.append({"idx": i, "expression": alpha["expression"], "sim": sim_result, "metrics": metrics, "submission": sub_result})
            else:
                results.append({"idx": i, "expression": alpha["expression"], "sim": sim_result, "metrics": metrics, "submission": {"submitted": False, "reason": "metrics_threshold"}})
        except Exception as e:
            print(f"[{i}] Exception: {e}")
            results.append({"idx": i, "expression": alpha["expression"], "error": str(e)})
        time.sleep(3)

    out_path = SKILL_DIR / "batch_submit_results.json"
    out_path.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nResults saved to: {out_path}")

    # Summary
    print("\n=== Summary ===")
    active = [r for r in results if r.get("submission", {}).get("status") == "ACTIVE"]
    submitted_pending = [r for r in results if r.get("submission", {}).get("submitted") and r.get("submission", {}).get("status") != "ACTIVE"]
    skipped = [r for r in results if r.get("submission", {}).get("submitted") is False]
    sim_errors = [r for r in results if (r.get("sim") or {}).get("status") == "ERROR"]
    request_errors = [r for r in results if "error" in r]
    print(f"ACTIVE: {len(active)}")
    print(f"Submitted but not ACTIVE: {len(submitted_pending)}")
    print(f"Skipped by metrics threshold: {len(skipped)}")
    print(f"Simulation errors: {len(sim_errors)}")
    print(f"Request errors: {len(request_errors)}")

    for r in active:
        m = r.get("metrics", {}).get("is", {})
        print(f"  ACTIVE {r['submission']['alpha'].get('id')}: Sharpe={m.get('sharpe',0):.2f}, Fitness={m.get('fitness',0):.2f}, TO={m.get('turnover',0)*100:.2f}%")


if __name__ == "__main__":
    main()
