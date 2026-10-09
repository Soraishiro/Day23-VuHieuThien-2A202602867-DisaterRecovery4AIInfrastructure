"""BƯỚC 3c — Tự động hoá runbook "Region chính Down", 7 bước.

BÁN TỰ ĐỘNG, KHÔNG FULL-AUTO: mặc định hỏi confirm, --auto chỉ dùng cho CI/khi
chấm điểm. Full-auto failover không có circuit breaker sẽ flap giữa 2 region.
"""
import argparse
import json
import pathlib
import sys
import time

import httpx

sys.path.insert(0, ".")
from dr import failover as fo  # noqa: E402
from dr import health_checker as hc  # noqa: E402

LOG = pathlib.Path("reports/runbook-run.jsonl")
HEALTH_LOG = pathlib.Path("reports/health-events.jsonl")
URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}


def _health_says_unhealthy(region: str) -> bool:
    """Health checker đã CHÍNH THỨC kết luận region này UNHEALTHY chưa?

    Runbook phải đi sau health checker, không tự ý kết luận sớm hơn. Nếu
    runbook cutover trước lúc health check phát hiện, con số RTO ghi ra là do
    tay người chạy, không phải do automation -> không tái lập được, và
    tools/measure_rto.py sẽ cảnh báo t_cutover < t_detect.
    """
    if not HEALTH_LOG.exists():
        return False
    for line in HEALTH_LOG.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if (e.get("event") == "state_change" and e.get("region") == region
                and e.get("to") == "UNHEALTHY"):
            return True
    return False


def step(num, name, **kw):
    """Ghi 1 dòng {ts, iso, step, name, ...} vào LOG.

    Tham số đặt tên là `num`/`name` để không đụng tên khóa của log data như
    `n`, `ts`, `event`, `region` (đụng tên sẽ ra TypeError lúc chạy chứ không
    phải lúc viết).
    """
    rec = {"ts": time.time(), "iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "step": f"{num}_{name}", "name": name, **kw}
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print("RUNBOOK", json.dumps(rec, ensure_ascii=False))
    return rec


def confirm(auto: bool, msg: str) -> bool:
    """auto=True -> True; ngược lại hỏi y/N. Đừng bỏ hàm này đi."""
    if auto:
        print(f"[auto-confirm] {msg}")
        return True
    print(f"\n=== XAC NHAN ===\n{msg}\n")
    try:
        ans = input("Tien hanh failover? (y/N): ").strip().lower()
    except EOFError:
        return False
    return ans in ("y", "yes")


def _p95(vals):
    if not vals:
        return None
    s = sorted(vals)
    return s[min(len(s) - 1, int(round(0.95 * (len(s) - 1))))]


def run(primary: str, target: str, backend: str, auto: bool) -> dict:
    """7 bước của runbook."""
    t_start = time.time()
    out = {"primary": primary, "target": target, "backend": backend,
           "auto": auto, "steps": [], "ok": False}

    # ---- 1. xac_nhan_outage ---------------------------------------------
    # KHÔNG tin 1 lần fail: probe nhiều lần, cùng nguyên tắc threshold của
    # health checker. Điều kiện để failover là "region CHÍNH đã chết", KHÔNG
    # phải "region phụ đã sẵn sàng" — region phụ rỗng là đúng thiết kế, việc
    # restore state + warm pool là việc của chính failover().
    # Ngoài ra phải đỢI health checker kết luận UNHEALTHY, để t_detect <
    # t_cutover và RTO là số của automation chứ không phải số của tay người.
    t0 = time.time()
    probes = {primary: [], target: []}
    for r in (primary, target):
        for _ in range(3):
            probes[r].append(hc.probe(r, 2.0))
            time.sleep(0.5)
    primary_fails = sum(1 for ok, _ in probes[primary] if not ok)
    primary_dead = primary_fails == len(probes[primary])

    # Chờ tín hiệu chính thức của health checker (tối đa 60s).
    waited_health = 0.0
    while not _health_says_unhealthy(primary) and waited_health < 60.0:
        time.sleep(1.0)
        waited_health = round(time.time() - t0, 1)
    hc_confirmed = _health_says_unhealthy(primary)

    outage_confirmed = primary_dead
    step(1, "xac_nhan_outage",
         probes={r: [{"ready": ok, "reason": rs} for ok, rs in v] for r, v in probes.items()},
         primary_fails=primary_fails, primary_confirmed_down=primary_dead,
         health_checker_confirmed_unhealthy=hc_confirmed,
         waited_health_check_s=waited_health,
         target_ready_before_failover=probes[target][-1][0],
         note="region phu rong la dung thiet ke; restore la viec cua failover()",
         took_s=round(time.time() - t0, 3))
    out["steps"].append("1_xac_nhan_outage")
    if not outage_confirmed:
        out["ok"] = False
        out["abort_reason"] = ("khong xac nhan duoc region chinh da chet "
                               "(van co probe tra ve ready) -> KHONG failover")
        step(7, "post_incident", ok=False, abort_reason=out["abort_reason"],
             elapsed_s=round(time.time() - t_start, 3))
        out["steps"].append("7_post_incident")
        return out

    # ---- 2. thong_bao_incident ------------------------------------------
    # ts của dòng này là mốc "operator biết tin" — LUÔN SAU t_outage trong
    # chaos-events (operator không thể biết ngay giây outage xảy ra).
    chaos = pathlib.Path("chaos/chaos-events.jsonl")
    t_outage = None
    if chaos.exists():
        for line in chaos.read_text().splitlines():
            if not line.strip():
                continue
            e = json.loads(line)
            if e.get("action") == "kill" and e.get("region") == primary:
                t_outage = e["ts"] if t_outage is None else max(t_outage, e["ts"])
    step(2, "thong_bao_incident", primary=primary, target=target,
         t_outage=t_outage, notification_lag_s=None if t_outage is None
         else round(time.time() - t_outage, 3),
         note="da mo incident channel + bat dong ho RTO")
    out["steps"].append("2_thong_bao_incident")
    out["t_outage"] = t_outage

    # ---- 3. scale_gpu_pool ----------------------------------------------
    # Gọi failover() MỘT LẦN DUY NHẤT. Hàm đó tự làm đủ 5 bước con.
    t0 = time.time()
    if not confirm(auto, f"Region {primary.upper()} DOWN da xac nhan. "
                         f"Failover sang region {target.upper()}?"):
        out["abort_reason"] = "operator tu choi (khong confirm)"
        step(3, "scale_gpu_pool", skipped=True, reason=out["abort_reason"])
        step(7, "post_incident", ok=False, abort_reason=out["abort_reason"],
             elapsed_s=round(time.time() - t_start, 3))
        out["steps"] += ["3_scale_gpu_pool", "7_post_incident"]
        return out
    fo_res = fo.failover(target, backend, wait=60.0)
    step(3, "scale_gpu_pool", ok=fo_res.get("ok"), target=target,
         steps_done=fo_res.get("steps"), rpo_seconds=fo_res.get("rpo_seconds"),
         docs_lost=fo_res.get("docs_lost"),
         embed_model_version=fo_res.get("embed_model_version"),
         abort_reason=fo_res.get("abort_reason"), took_s=round(time.time() - t0, 3))
    out["steps"].append("3_scale_gpu_pool")
    out["failover"] = fo_res
    out["rpo_seconds"] = fo_res.get("rpo_seconds")
    out["docs_lost"] = fo_res.get("docs_lost")

    if not fo_res.get("ok"):
        # KHÔNG gọi lại failover, KHÔNG cutover tay. Giữ nguyên routing và
        # báo cáo để operator xử lý.
        out["abort_reason"] = fo_res.get("abort_reason")
        step(4, "verify_state_replica", skipped=True, reason="failover khong thanh cong")
        step(5, "dns_cutover", skipped=True, ok=False)
        step(6, "verify_golden_signals", skipped=True)
        step(7, "post_incident", ok=False, abort_reason=out["abort_reason"],
             elapsed_s=round(time.time() - t_start, 3))
        out["steps"] += ["4_verify_state_replica", "5_dns_cutover",
                         "6_verify_golden_signals", "7_post_incident"]
        return out

    # ---- 4. verify_state_replica ----------------------------------------
    # CHỈ ĐỌC lại kết quả từ dict bước 3. Không gọi lại failover.
    t0 = time.time()
    try:
        st = fo.state_of(target)
        replica = {"vector_count": st.get("count"), "weights": st.get("weights"),
                   "pool_state": st.get("pool_state")}
    except Exception as e:
        replica = {"error": type(e).__name__}
    ok_replica = bool(replica.get("weights")) and (replica.get("vector_count") or 0) > 0
    step(4, "verify_state_replica", target=target, **replica, ok=ok_replica,
         took_s=round(time.time() - t0, 3))
    out["steps"].append("4_verify_state_replica")
    out["replica"] = replica

    # ---- 5. dns_cutover --------------------------------------------------
    # Cũng chỉ đọc lại kết quả cutover từ bước 3.
    t0 = time.time()
    active = pathlib.Path("edge/active_region")
    cur = active.read_text(encoding="utf-8").strip() if active.exists() else None
    cutover_ok = cur == target
    step(5, "dns_cutover", active_region=cur, expected=target, ok=cutover_ok,
         took_s=round(time.time() - t0, 3))
    out["steps"].append("5_dns_cutover")

    # ---- 6. verify_golden_signals ---------------------------------------
    # 10 request THẬT, đo p95 latency + error rate.
    t0 = time.time()
    lats, errors = [], 0
    for i in range(10):
        t1 = time.time()
        try:
            r = httpx.get(f"{URL[target]}/v1/infer",
                          params={"q": f"hoa don thang {i % 12 + 1}"}, timeout=3.0)
            if r.status_code == 200:
                lats.append((time.time() - t1) * 1000)
            else:
                errors += 1
        except Exception:
            errors += 1
    p95 = _p95(lats)
    rate = round(errors / 10.0, 3)
    golden_ok = errors == 0 and p95 is not None and p95 < 2000.0
    step(6, "verify_golden_signals", target=target, requests_sent=10,
         errors=errors,
         error_rate=rate, p95_latency_ms=None if p95 is None else round(p95, 1),
         ok=golden_ok, took_s=round(time.time() - t0, 3))
    out["steps"].append("6_verify_golden_signals")
    out["golden"] = {"p95_latency_ms": None if p95 is None else round(p95, 1),
                     "error_rate": rate, "ok": golden_ok}

    # ---- 7. post_incident ------------------------------------------------
    elapsed = round(time.time() - t_start, 3)
    out["ok"] = bool(fo_res.get("ok") and cutover_ok and golden_ok)
    step(7, "post_incident", ok=out["ok"], elapsed_s=elapsed,
         rpo_seconds=out.get("rpo_seconds"), docs_lost=out.get("docs_lost"),
         measure_cmd="python3 tools/measure_rto.py --loadgen "
                     "reports/drill-2-withdr.jsonl --target-rto 300",
         note="chay lenh tren de chot RTO roi ghi postmortem")
    out["steps"].append("7_post_incident")
    out["elapsed_s"] = elapsed
    return out


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--primary", default="a")
    p.add_argument("--target", default="b")
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--auto", action="store_true")
    a = p.parse_args()
    print(json.dumps(run(a.primary, a.target, a.backend, a.auto), indent=2,
                     ensure_ascii=False))