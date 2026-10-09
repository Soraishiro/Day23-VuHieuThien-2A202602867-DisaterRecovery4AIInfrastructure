# Runbook 1 trang — Region chính down

**Họ và tên:** Vũ Hiếu Thiên · **MSSV:** 2A202602867

Viết để người **không** phải tác giả cũng làm được lúc 3h sáng. Mỗi bước cần đủ ba thứ:
lệnh copy-paste được, tín hiệu biết bước đó xong, và người chịu trách nhiệm.

**Lưu ý cổng:** edge proxy ở đây chạy cổng **8090** (8080 bị container Airflow chiếm). Nếu
bạn chạy `scripts/up_bare.sh` trên máy không bị chiếm cổng, thay `8090` bằng `8080` ở mọi
lệnh dưới đây.

---

## 7 bước

| # | Bước | Lệnh | Biết là xong khi | Ai làm |
|---|---|---|---|---|
| 1 | Xác nhận outage | `python chaos/kill_region.py status` | `a.alive=false` **3 lần liên tiếp**, và `b.alive=true` | on-call |
| 2 | Mở incident + bấm giờ RTO | `python dr/runbook.py --primary a --target b --backend fs` (hỏi `y`) | Dòng `2_thong_bao_incident` xuất hiện trong `reports/runbook-run.jsonl` | on-call |
| 3 | Restore state ở region phụ | tự động trong bước 2 (`2_restore_snapshot`) | `reports/failover-events.jsonl` có dòng `2_restore_snapshot` kèm `rpo_seconds` và `docs_lost` | automation |
| 4 | Scale pool warm→full | tự động (`3_scale_pool` rồi poll `4_wait_ready`) | `curl localhost:8002/readyz` trả 200 — mất khoảng 6.3s vì warm-up | automation |
| 5 | DNS/LB cutover | tự động (`5_dns_cutover`) | `curl localhost:8090/edge/state` cho `"active_region":"b"` | automation |
| 6 | Verify golden signals | tự động (`6_verify_golden_signals`) | `error_rate` = 0.0 **và** `p95_latency_ms` < 2000 trong `reports/runbook-run.jsonl` | automation |
| 7 | Đo RTO + postmortem | `python tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300` | `"rto_verdict":"PASS"` và `warnings` là mảng rỗng | on-call |

Bước 2–6 chạy bằng **một** lệnh duy nhất ở dòng 2. Không chạy lại `dr/failover.py` riêng:
`dr/runbook.py` tự lo 5 bước con, và chạy hai lần sẽ tạo hai vòng restore/warm-up thừa.

**Kỳ vọng về thời gian** (rút từ lần chạy đã chấm): detect ~15s, restore <1s, warm-up ~6s,
TTL ~1s. Nếu `4_wait_ready` mất quá 60s thì đó là sự cố thật, không phải chuyện bình thường.

## Chạy không cần hỏi (chỉ dùng cho drill/CI)

```bash
python dr/runbook.py --primary a --target b --backend fs --auto
```

Không dùng `--auto` khi sự cố thật: full-auto không có circuit breaker sẽ khiến hai region
flap liên tục nếu cả hai cùng chập chờn.

## Đọc nhanh kết quả

```bash
cat reports/failover-events.jsonl    # 5 bước con của failover, đúng thứ tự
cat reports/runbook-run.jsonl        # timeline 7 bước của runbook
python tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300
```

Kết quả tham chiếu của lần chạy đã chấm: RTO **23.3s** (PASS), RPO **4.0s / 2 doc**,
`warnings: []`, golden signals error rate 0.0 với p95 80.6ms. Chi tiết ở
`reports/rto-evidence.md`.

## Khi runbook trả về `ok: false`

| `abort_reason` chứa | Nghĩa là | Xử lý |
|---|---|---|
| `4_wait_ready timeout` | Region B không lên được trong `wait=60s` | **Không cutover tay.** Kiểm `curl localhost:8002/readyz` xem thiếu `model_weights_missing` hay `vector_db_empty` |
| `2_restore_snapshot failed` | Chưa từng có snapshot nào được `put` | Chạy `python state/snapshot.py put --region a --backend fs`, rồi chạy lại runbook |
| `khong xac nhan duoc region chinh da chet` | Region A còn probe trả `ready` | Có thể chỉ chậm chứ chưa chết — đừng failover. Kiểm `curl localhost:8001/readyz` |
| operator từ chối | Bạn đã gõ `n` | Cố ý, thường vì nghi ngờ Region A chỉ chậm |

---

## Rollback (failover ngược về Region A)

**Điều kiện để trả traffic về Region A** — cần đủ cả hai:

1. `curl localhost:8001/readyz` trả **200** (pool `full`, warm-up xong, weights có, vector
   count > 0). Không chỉ `/healthz` trả `alive:true` — đó chính là cái bẫy đã làm baseline
   drill hỏng.
2. Đã kiểm Region A có dữ liệu mới hơn Region B, hoặc đã chấp nhận mất dữ liệu phát sinh
   trong thời gian failover.

**Lệnh rollback:**

```bash
python chaos/kill_region.py restore --region a --backend bare   # SIGCONT nếu chỉ bị netblock
curl -s localhost:8001/readyz                                   # phải 200 trước bước sau
python dr/failover.py --target a --backend fs                   # verify → restore → scale → wait → cutover
curl -s localhost:8090/edge/state                               # phải trả "active_region":"a"
```

**Ai quyết định:** Incident Commander — không phải người đang trực một mình, và không phải
automation. Rollback cần người chịu trách nhiệm vì nó đổi traffic về đúng một region vừa
được chứng minh là không ổn định.

**Chống flap:** sau mỗi lần failover hoặc rollback, đặt cooldown tối thiểu 5 phút trước khi
đổi hướng lần nữa. Không có cooldown thì hai region chập chờn sẽ làm hệ thống flap liên tục,
và mỗi lần flap đều làm RTO của *lần sau* dài hơn nữa — vì detect floor và warm-up phải chạy
lại từ đầu.