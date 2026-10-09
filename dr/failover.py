"""BƯỚC 3b — Cutover sang region phụ, đúng 5 bước đúng thứ tự.

  1_verify_target    — /v1/state của region phụ
  2_restore_snapshot — state/snapshot.py get + rpo()
  3_scale_pool       — ghi "full" vào state/region-<t>/pool_state
  4_wait_ready       — poll /readyz tới khi 200 (chứa GPU pool warm-up)
  5_dns_cutover      — ghi region đích vào edge/active_region

BẪY: nếu đổi edge/active_region TRƯỚC bước 4, user nhận 503 từ CẢ HAI region
và RTO dài hơn. Bước 4 timeout -> ABORT, KHÔNG cutover.
"""
import argparse
import json
import pathlib
import sys
import time

import httpx

sys.path.insert(0, ".")
from state import snapshot  # noqa: E402

URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}
LOG = pathlib.Path("reports/failover-events.jsonl")
ACTIVE = pathlib.Path("edge/active_region")


def emit(**kw):
    """Append 1 dòng JSONL có ts + iso vào LOG, và print ra stdout."""
    rec = {"ts": time.time(), "iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **kw}
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print("FAILOVER", json.dumps(rec, ensure_ascii=False))
    return rec


def state_of(region: str) -> dict:
    """Đọc /v1/state của một region (dùng cho bước 1 và bước 4)."""
    return httpx.get(f"{URL[region]}/v1/state", timeout=3.0).json()


def _pool_file(target: str) -> pathlib.Path:
    return pathlib.Path(f"state/region-{target}/pool_state")


def failover(target: str, backend: str, wait: float) -> dict:
    """5 bước ở trên, đúng thứ tự. Trả về dict có ok / rpo_seconds / docs_lost."""
    other = "a" if target == "b" else "b"
    res = {"ok": False, "target": target, "backend": backend, "steps": [],
           "rpo_seconds": None, "docs_lost": None, "abort_reason": None}

    # ---- BƯỚC 1: verify_target -------------------------------------------
    t0 = time.time()
    try:
        st = state_of(target)
        emit(step="1_verify_target", target=target, reachable=True,
             pool_state=st.get("pool_state"), count=st.get("count"),
             weights=st.get("weights"), took_s=round(time.time() - t0, 3))
        res["steps"].append("1_verify_target")
    except Exception as e:
        # Region phụ không trả lời /v1/state. Ghi lại lý rồi vẫn đi tiếp: bước 2
        # (restore snapshot) có thể vẫn cứu được nó. Việc region phụ có serve
        # được hay không sẽ bị BƯỚC 4 chặn lại — đó mới là hàng rào quyết định.
        emit(step="1_verify_target", target=target, reachable=False,
             reason=type(e).__name__, took_s=round(time.time() - t0, 3))
        res["steps"].append("1_verify_target")

    # ---- BƯỚC 2: restore_snapshot ---------------------------------------
    t0 = time.time()
    prim_db = pathlib.Path(f"state/region-{other}/vectors.sqlite")
    rest_db = pathlib.Path(f"state/region-{target}/vectors.sqlite")
    try:
        meta = snapshot.get(target, backend)
        # RPO phải đo, không đoán: so timestamp doc mới nhất ở primary với bản
        # vừa restore. embed_model_version phải đi kèm để phát hiện index
        # không tương thích khi restore (snapshot.py put ghi version này).
        rp = snapshot.rpo(prim_db, rest_db)
        res["rpo_seconds"] = rp["rpo_seconds"]
        res["docs_lost"] = rp["docs_lost"]
        res["embed_model_version"] = meta.get("embed_model_version")
        emit(step="2_restore_snapshot", target=target, backend=backend,
             rpo_seconds=rp["rpo_seconds"], docs_lost=rp["docs_lost"],
             embed_model_version=meta.get("embed_model_version"),
             primary_latest_doc_ts=rp["primary_latest_doc_ts"],
             restored_latest_doc_ts=rp["restored_latest_doc_ts"],
             took_s=round(time.time() - t0, 3))
        res["steps"].append("2_restore_snapshot")
    except Exception as e:
        emit(step="2_restore_snapshot", target=target, ok=False,
             reason=f"{type(e).__name__}: {e}", took_s=round(time.time() - t0, 3))
        res["abort_reason"] = f"2_restore_snapshot failed: {type(e).__name__}"
        res["failed_step"] = "2_restore_snapshot"
        return res

    # ---- BƯỚC 3: scale_pool ---------------------------------------------
    # Ghi "full" SAU khi snapshot đã vào đĩa. Đây là transition warm -> full
    # lúc process ĐANG CHẠY, nên serving/app.py bắt đầu đếm warm-up ở đây.
    t0 = time.time()
    pf = _pool_file(target)
    pf.parent.mkdir(parents=True, exist_ok=True)
    pf.write_text("full")
    emit(step="3_scale_pool", target=target, pool_state="full",
         note="warm->full transition bat dau GPU pool warm-up",
         took_s=round(time.time() - t0, 3))
    res["steps"].append("3_scale_pool")

    # ---- BƯỚC 4: wait_ready ---------------------------------------------
    # HÀNG RÀO QUYẾT ĐỊNH. Phải đợi /readyz == 200 (tức là warm-up xong).
    t0 = time.time()
    deadline = t0 + wait
    ready, last_reason = False, "never_polled"
    while time.time() < deadline:
        try:
            r = httpx.get(f"{URL[target]}/readyz", timeout=2.0)
            if r.status_code == 200:
                ready = True
                last_reason = "ready"
                break
            try:
                last_reason = ",".join(r.json().get("reasons", []))
            except Exception:
                last_reason = f"status_{r.status_code}"
        except Exception as e:
            last_reason = type(e).__name__
        time.sleep(0.25)
    waited = round(time.time() - t0, 3)
    if ready:
        emit(step="4_wait_ready", target=target, ok=True, waited_s=waited,
             note="GPU pool warm-up xong")
        res["steps"].append("4_wait_ready")
        res["warmup_waited_s"] = waited
    else:
        emit(step="4_wait_ready", target=target, ok=False, waited_s=waited,
             reason=last_reason, note="TIMEOUT -> ABORT, khong cutover")
        # KHÔNG cutover. Quay về giữ nguyên routing hiện tại.
        res["abort_reason"] = f"4_wait_ready timeout sau {waited}s ({last_reason})"
        res["failed_step"] = "4_wait_ready"
        return res

    # ---- BƯỚC 5: dns_cutover -------------------------------------------
    # Chỉ tới đây, SAU khi target thật sự ready.
    t0 = time.time()
    ACTIVE.parent.mkdir(parents=True, exist_ok=True)
    # Ghi file KHÔNG BOM (encoding='utf-8'), nếu không edge đọc ra "ï»¿b".
    ACTIVE.write_text(target, encoding="utf-8")
    emit(step="5_dns_cutover", active_region=target, target=target,
         took_s=round(time.time() - t0, 3))
    res["steps"].append("5_dns_cutover")

    res["ok"] = True
    res["cutover_at"] = time.time()
    return res


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--target", default="b", choices=["a", "b"])
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--wait", type=float, default=60)
    a = p.parse_args()
    print(json.dumps(failover(a.target, a.backend, a.wait), indent=2,
                     ensure_ascii=False))