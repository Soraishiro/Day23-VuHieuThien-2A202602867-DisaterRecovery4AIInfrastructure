"""BƯỚC 3a — Health checker cho 2 region.

Poll /readyz của CẢ HAI region mỗi `interval` giây, chỉ đổi trạng thái sau
`threshold` lần fail LIÊN TIẾP, và ghi 1 dòng JSONL mỗi lần trạng thái đổi.
"""
import argparse
import json
import pathlib
import time

import httpx

URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}

# Trạng thái ban đầu của mỗi region. Không ghi 1 dòng "to":HEALTHY lúc khởi động
# vì đó không phải transition — log phải chỉ chứa những lần TRẠNG THÁI ĐỔI, và
# tools/measure_rto.py đọc dòng đầu tiên có to=="UNHEALTHY".
_state = {r: "HEALTHY" for r in URL}
_fails = {r: 0 for r in URL}


def probe(region: str, timeout: float) -> tuple[bool, str]:
    """Kiểm tra /readyz của một region.

    Trả về (ready, reason):
    - (True, "ready") nếu status_code == 200
    - (False, ...) nếu timeout, lỗi mạng, hoặc status != 200

    PHẢI có timeout: --mode netblock là SIGSTOP, TCP handshake vẫn xong nên
    request sẽ TREO vô hạn nếu không có timeout. Không có timeout thì health
    checker treo theo region chết và không bao giờ phát hiện ra outage.
    """
    try:
        r = httpx.get(f"{URL[region]}/readyz", timeout=timeout)
    except httpx.TimeoutException:
        return False, "timeout"
    except Exception as e:  # ConnectError, RemoteProtocolError, ...
        return False, type(e).__name__
    if r.status_code == 200:
        return True, "ready"
    # 503 -> region sống nhưng KHÔNG serve được. Nêu lý do cụ thể từ /readyz
    # để log đọc ra được nguyên nhân thật, không chỉ "không 200".
    try:
        reasons = ",".join(r.json().get("reasons", []))
    except Exception:
        reasons = ""
    return False, f"status_{r.status_code}" + (f":{reasons}" if reasons else "")


def emit(out: pathlib.Path, region: str, prev: str, to: str, reason: str,
         interval: float, threshold: int, consecutive_fails: int) -> None:
    """Ghi 1 dòng JSONL cho MỘT lần đổi trạng thái.

    `prev` phải được truyền vào từ TRƯỚC khi _state[region] bị ghi đè, nếu
    không dòng log sẽ đọc ra "UNHEALTHY -> UNHEALTHY" và mất thông tin
    trạng thái trước đó.
    """
    rec = {
        "ts": time.time(),
        "iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "event": "state_change",
        "region": region,
        "from": prev,
        "to": to,
        "reason": reason,
        "interval_s": interval,
        "threshold": threshold,
        # detect floor = interval x threshold. Con so nay NAM TRONG RTO.
        "detect_floor_s": round(interval * threshold, 3),
        "consecutive_fails": consecutive_fails,
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print("HEALTH", json.dumps(rec, ensure_ascii=False))


def run(interval: float, timeout: float, threshold: int, duration: float,
        out: pathlib.Path):
    """Vòng lặp poll + phát hiện transition + ghi JSONL.

    - Mỗi vòng poll /readyz của cả 2 region.
    - Đếm fail LIÊN TIẾP; 1 lần pass reset counter về 0 (đây là chống flapping).
    - Chỉ ghi log khi state thật sự đổi, không ghi mỗi lần poll.
    """
    out = pathlib.Path(out)
    for r in URL:                      # reset module state giữa các lần chạy
        _state[r], _fails[r] = "HEALTHY", 0
    end = time.time() + duration
    while time.time() < end:
        # `interval` là CHU KỲ của vòng poll, không phải thời gian nằm ngủ thêm
        # sau khi probe xong. Nếu sleep(interval) chạy SAU probe, một probe bị
        # treo (netblock/SIGSTOP) sẽ làm chu kỳ thật = interval + timeout, và
        # detect floor thật sẽ lớn hơn interval*threshold mà log vẫn ghi
        # interval*threshold -> so liệu tự mâu thuẫn với chính nó.
        cycle_start = time.time()
        for region in URL:
            ready, reason = probe(region, timeout)
            if ready:
                _fails[region] = 0
                if _state[region] != "HEALTHY":
                    prev, _state[region] = _state[region], "HEALTHY"
                    emit(out, region, prev, "HEALTHY", reason, interval, threshold, 0)
                continue

            _fails[region] += 1
            # CHƯA đủ threshold -> KHÔNG đổi trạng thái. Một lần fail có thể
            # chỉ là network lag, không phải outage.
            if _fails[region] >= threshold and _state[region] != "UNHEALTHY":
                prev, _state[region] = _state[region], "UNHEALTHY"
                emit(out, region, prev, "UNHEALTHY", reason, interval, threshold,
                     _fails[region])

        left = end - time.time()
        if left <= 0:
            break
        # chừa đúng phần interval CHƯA dùng của chu kỳ này. max(0, ...) vì một
        # vòng có thể đã vượt quá interval (probe chậm, máy bị nghẽn) — lúc đó
        # lấy chu kỳ tiếp theo ngay, tuyệt đối không sleep số âm.
        time.sleep(max(0.0, min(interval - (time.time() - cycle_start), left)))


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--interval", type=float, default=5.0)
    p.add_argument("--timeout", type=float, default=2.0)
    p.add_argument("--threshold", type=int, default=3)
    p.add_argument("--duration", type=float, default=300)
    p.add_argument("--out", default="reports/health-events.jsonl")
    a = p.parse_args()
    run(a.interval, a.timeout, a.threshold, a.duration, pathlib.Path(a.out))