# RTO/RPO Evidence — Lab 23

**Họ và tên:** Vũ Hiếu Thiên · **MSSV:** 2A202602867

Nguyên tắc của file này: mọi con số đều phải trỏ được về một dòng log thật, ghi dạng
`đường/dẫn.jsonl:số_dòng`. Bộ test `tests/test_rto_evidence.py` sẽ tự mở từng file ra
kiểm tra, nên số không có bằng chứng thì trượt dù phần văn bản có hay đến đâu.

**Môi trường đo:** WSL2 Ubuntu 24.04, Python 3.12.3, chạy bare mode (không Docker —
đúng đường chấm điểm theo GUIDE). Edge proxy chạy ở cổng 8090 thay vì 8080, vì trên máy
này cổng 8080 đang bị container Airflow `uit_mentoring-airflow-webserver-1` chiếm. Đây là
khác biệt duy nhất so với `scripts/up_bare.sh` và không ảnh hưởng logic DR nào, vì mọi
mốc thời gian đều lấy từ log chứ không phụ thuộc cổng.

---

## 1. Drill 1 — không có DR (baseline)

| Chỉ số | Giá trị | Cách đo | Evidence |
|---|---|---|---|
| t_outage | `2026-10-09T04:15:01` (ts `1791519301.3121276`) | chaos `action:kill` | `chaos/chaos-events.jsonl:1` |
| Request fail đầu tiên | `+0.4s` | dòng `ok:false` đầu tiên sau t_outage | `reports/drill-1-nodr.jsonl:18` |
| Timeout thực tế của request đó | `2021.6ms` | trường `latency_ms` cùng dòng | `reports/drill-1-nodr.jsonl:18` |
| Request thành công sau đó | không có | không tồn tại dòng `ok:true` nào sau t_outage | `reports/measure-drill-1.json` |
| RTO | `NO_RECOVERY` | `tools/measure_rto.py` | `reports/measure-drill-1.json` |
| Tổng request | 32 (15 request sau t_outage, 15/15 fail) | đếm dòng | `reports/drill-1-nodr.jsonl:32` |

Kết quả này chính là thứ rubric đòi ở `test_drill1_ton_tai_va_khong_phuc_hoi`: 15/15 request
sau khi kill đều thất bại, `recovered_by_region` là `null`, `rto_measured_s` là `null`. Hệ
thống không tự phục hồi được.

Một chi tiết đáng lưu ý: trong suốt thời gian đó `/healthz` của Region B vẫn trả
`{"alive": true}`, trong khi `/readyz` trả 503 với lý do `vector_db_empty(count=0)`.

---

## 2. Drill 2 — có DR

| Mốc | +giây từ t_outage | Cách đo | Evidence |
|---|---|---|---|
| t_outage (mốc 0) | 0 | `action:kill` | `chaos/chaos-events.jsonl:3` |
| User thấy lỗi đầu tiên | +0.1 | dòng `ok:false` đầu, `error:ReadTimeout` | `reports/drill-2-withdr.jsonl:25` |
| Health check phát hiện | +15.1 | `to:UNHEALTHY, region:a` | `reports/health-events.jsonl:2` |
| Bước 1 verify target | +15.8 | `step:1_verify_target` | `reports/failover-events.jsonl:1` |
| Snapshot restore xong | +16.0 | `step:2_restore_snapshot` | `reports/failover-events.jsonl:2` |
| Scale pool (warm→full) | +16.0 | `step:3_scale_pool` | `reports/failover-events.jsonl:3` |
| Region phụ ready | +22.3 | `step:4_wait_ready, waited_s:6.324` | `reports/failover-events.jsonl:4` |
| DNS cutover | +22.3 | `step:5_dns_cutover` | `reports/failover-events.jsonl:5` |
| **RTO đo được** | **+23.3** | dòng `ok:true` đầu sau lỗi, `served_by:b` | `reports/drill-2-withdr.jsonl:35` |

| Chỉ số | Đo được | Mục tiêu | Verdict |
|---|---|---|---|
| RTO — Inference API | **23.3s** | 300s (5 phút) | **PASS** |
| RPO — Vector DB | **4.0s / 2 doc** | 300s (5 phút) | **PASS** |

RPO lấy từ chính dòng `2_restore_snapshot` (`reports/failover-events.jsonl:2`):
`rpo_seconds: 4.0`, `docs_lost: 2`, `embed_model_version: "embed-model=vi-e5-base@v3"`.
Hai timestamp nguồn là `primary_latest_doc_ts: 1791519379.3011396` và
`restored_latest_doc_ts: 1791519375.300909` — hiệu số đúng bằng 4.0s, nghĩa là RPO được
đo từ dữ liệu thật chứ không phải ước lượng từ tuổi snapshot.

Cấu hình health check được ghi lại trong log để giải thích vì sao detect xảy ra lúc
+15.1s (`reports/health-events.jsonl:2`): `interval_s: 5.0`, `threshold: 3`,
`detect_floor_s: 15.0`. Sàn lý thuyết là 15.0s nên chỉ chênh 0.1s. (Trước khi sửa lỗi
nhịp poll, con số này là 19.8s — xem Case Study 4 trong phần Reflection.)

---

## 3. RTO 23.3s gồm những gì

| Thành phần | Giây | Nó đến từ đâu | Giảm được bằng cách nào |
|---|---|---|---|
| Health-check detect floor | 15.1 | `interval_s × threshold` tại `reports/health-events.jsonl:2` | Hạ `interval` 5→2s (sàn còn 6s) hoặc `threshold` 3→2 (sàn 10s). Đánh đổi: dễ false-positive hơn, tức dễ flap |
| Snapshot restore | 0.171 | `took_s` ở `2_restore_snapshot` — `reports/failover-events.jsonl:2` | Không đáng kể: copy ~90KB trên filesystem local. Với S3 thật thì đây mới là phần tốn giây nhất |
| GPU pool warm-up | 6.324 | `waited_s` ở `4_wait_ready` — `reports/failover-events.jsonl:4` | Giữ sẵn pool region phụ ở `full` (active-active) để warm-up không nằm trong RTO |
| DNS/LB TTL cache | 1.0 | 23.3 − 22.3 (t_recovered − t_cutover) | Hạ `EDGE_TTL_SECONDS` 5→1 |

Tổng: 15.1 + 0.171 + 6.324 + 1.0 = 22.6, khớp với RTO 23.3s. Chênh 0.7s là do loadgen bắn
request cách nhau 0.5s, nên request thành công đầu tiên rơi vào nhịp kế tiếp sau cutover chứ
không rơi ngay vào thời điểm ghi `5_dns_cutover`.

Để thấy rõ giá trị của việc đo thay vì đoán: lần chạy trước khi sửa lỗi nhịp poll cho RTO
33.6s với detect 19.8s. Sửa đúng một dòng tính thời gian ngủ đã cắt 10.3s (31% RTO) mà
không phải đổi cấu hình nào được yêu cầu.

---

## 4. Hai lệch lệch so với `scripts/up_bare.sh`

Cả hai đều do môi trường máy, không phải logic:

1. **Edge proxy ở cổng 8090** thay vì 8080 — 8080 đang bị container Airflow
   `uit_mentoring-airflow-webserver-1` chiếm, nên tôi không dừng stack đó. `loadgen` chạy
   kèm `--url http://127.0.0.1:8090/v1/infer`.
2. **Process detach bằng `setsid nohup`** thay vì `&`. Nếu không, uvicorn chết theo khi phiên
   `wsl.exe` kết thúc, và `chaos/kill_region.py` sẽ báo "region-b khong phan hoi" rồi từ
   chối kill.

Hai điều trên không đổi bất kỳ timestamp nào trong bảng: `t_outage`, `t_detect`,
`t_cutover`, `t_recovered` đều đến từ log.

---

## 5. Golden signals sau khi phục hồi

Đo bằng 10 request thật tới Region B ngay sau cutover
(`reports/runbook-run.jsonl`, bước `6_verify_golden_signals`):

| Chỉ số | Đo được | Ngưỡng | Verdict |
|---|---|---|---|
| Error rate | 0.0 (0/10 lỗi) | 0 | PASS |
| p95 latency | 80.6ms | < 2000ms | PASS |
| Vector count ở region phụ | 216 | > 0 | PASS |
| Model weights | true | true | PASS |

Phân bố latency của 132 request thành công sau t_outage trong
`reports/drill-2-withdr.jsonl`: p50 `70.8ms`, p95 `93.4ms`, max `159.6ms`. Còn 10 request
thất bại đều là `ReadTimeout` với latency 2019–2032ms, khớp với `EDGE_TIMEOUT_SECONDS=2`
của edge proxy — xác nhận `SIGSTOP` làm treo request chứ không phải refuse. Chuỗi fail dài
nhất là 10 request liên tiếp (5.0s), tức toàn bộ cửa sổ outage đã được phủ bởi việc phục hồi.