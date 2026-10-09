# Postmortem — DR Drill Lab 23

**Họ và tên:** Vũ Hiếu Thiên · **MSSV:** 2A202602867

Blameless nghĩa là câu hỏi là *"hệ thống hoặc process nào cho phép chuyện này"*, không phải
*"ai làm sai"*.

**Sự kiện:** Region A bị `SIGSTOP` (`--mode netblock --mock`) lúc `2026-10-09T04:16:03`,
đang serve traffic thật qua edge proxy.

---

## 1. Timeline

| ISO time | +giây | Sự kiện | Evidence |
|---|---|---|---|
| `2026-10-09T04:15:01` | (drill trước) | kill Region A — baseline drill 1 | `chaos/chaos-events.jsonl:1` |
| `2026-10-09T04:16:03` | 0 | **outage bắt đầu** — SIGSTOP Region A | `chaos/chaos-events.jsonl:3` |
| `2026-10-09T04:16:03` | +0.1 | **user đầu tiên bị ảnh hưởng** — `ReadTimeout` sau 2022.2ms | `reports/drill-2-withdr.jsonl:25` |
| `2026-10-09T04:16:18` | +15.1 | **health check alert** — `HEALTHY → UNHEALTHY` sau 3 fail liên tiếp | `reports/health-events.jsonl:2` |
| `2026-10-09T04:16:18` | +15.7 | operator xác nhận outage, mở incident | `reports/runbook-run.jsonl:1` |
| `2026-10-09T04:16:19` | +15.8 | `1_verify_target` — Region B sống nhưng rỗng | `reports/failover-events.jsonl:1` |
| `2026-10-09T04:16:19` | +16.0 | `2_restore_snapshot` — RPO 4.0s / 2 doc | `reports/failover-events.jsonl:2` |
| `2026-10-09T04:16:19` | +16.0 | `3_scale_pool` — warm → full, bắt đầu warm-up | `reports/failover-events.jsonl:3` |
| `2026-10-09T04:16:25` | +22.3 | `4_wait_ready` — warm-up xong sau 6.324s | `reports/failover-events.jsonl:4` |
| `2026-10-09T04:16:26` | +22.3 | `5_dns_cutover` — `active_region` → `b` | `reports/failover-events.jsonl:5` |
| `2026-10-09T04:16:26` | +22.4 | xác nhận replica: 216 vector, weights=true | `reports/runbook-run.jsonl:4` |
| `2026-10-09T04:16:26` | +23.1 | golden signals: error rate 0.0, p95 80.6ms | `reports/runbook-run.jsonl:6` |
| `2026-10-09T04:16:27` | +23.3 | **resolved** — request `ok:true` đầu tiên, `served_by:b` | `reports/drill-2-withdr.jsonl:35` |

Độ trễ thông báo là 15.72s, ghi ở trường `notification_lag_s` tại `reports/runbook-run.jsonl:2`.

---

## 2. Gap analysis — RTO/RPO đo được so với mục tiêu

| Chỉ số | Mục tiêu | Đo được | Gap |
|---|---|---|---|
| RTO | 300s | **23.3s** | −276.7s (dư đạt) |
| RPO | 300s | **4.0s** (2 doc mất) | −296.0s (dư đạt) |

Gap là âm ở cả hai chỉ số — tức là hệ thống dư đạt so với cam kết, không có khoảng trống
cần bù. Nhưng điều đáng nói là **RTO 23.3s phân bố thế nào**:

| Thành phần | Giây | Tỷ lệ |
|---|---|---|
| Health-check detect floor | 15.1 | 65% |
| GPU pool warm-up | 6.324 | 27% |
| DNS/LB TTL cache | 1.0 | 4% |
| Snapshot restore | 0.171 | <1% |

**Bước tốn nhiều giây nhất là detect floor, chiếm 65% RTO.** Vì sao là 15.1s chứ không phải
đúng sàn 15.0s (`interval_s × threshold` = 5 × 3)? Chênh 0.1s là độ trễ thực thi.

Trước khi sửa lỗi nhịp poll, con số này là 19.8s — lớn hơn sàn 4.8s vì `sleep(interval)`
chạy *sau* khi probe đã treo hết 2.0s, biến chu kỳ 5s thành 7s. Chi tiết ở Case Study 4.

Hai thành phần còn lại: warm-up 6.324s (27%) và DNS TTL 1.0s (4%). Snapshot restore chỉ
0.171s — không đáng kể trên filesystem local, dù với S3 thật thì ngược lại.

---

## 3. Root cause (5 whys)

Câu hỏi không phải "vì tôi chạy chaos script", mà là: *nếu đây là outage thật, bước nào trong
runbook sẽ thất bại?*

1. **Vì sao user mất 23.3s?** Vì Region A ngừng trả lời và không có ai chuyển traffic sang
   Region B trong khoảng đó.
2. **Vì sao không ai chuyển traffic?** Vì `edge/active_region` vẫn trỏ tới `a` và không có
   thành phần nào tự ghi đè nó.
3. **Vì sao không có thành phần nào ghi đè?** Vì trước Step 3 không tồn tại process nào theo
   dõi trạng thái hai region — stack chỉ có 3 process: 2 API và 1 proxy.
4. **Vì sao không có process theo dõi?** Vì thiết kế ban đầu coi `/healthz` là đủ, và coi
   "Region B còn sống" là đồng nghĩa với "Region B phục vụ được".
5. **Vì sao hai điều đó không tương đương?** Vì `/healthz` chỉ trả `{"alive": true}` khi
   process còn sống, còn `/readyz` mới kiểm tra cả pool state, model weights và vector count.
   Ở baseline, Region B sống nhưng `count:0, weights:false` — sống nhưng không serve được.

**Gốc rễ: liveness ≠ readiness.** Giám sát được thiết kế quanh "process còn sống" thay vì
quanh "region phục vụ được", nên cơ chế failover không tồn tại để khởi động.

Đây cũng là lý do runbook phải chờ tín hiệu `UNHEALTHY` của health checker (bước
`1_xac_nhan_outage`, `reports/runbook-run.jsonl:1`) thay vì tự kết luận. Nếu runbook quyết
định sớm hơn thì `t_cutover` sẽ nhỏ hơn `t_detect`, và con số RTO sẽ là số của tay người
chạy chứ không tái lập được — `tools/measure_rto.py` sẽ cảnh báo đúng điều đó.

### Hai lỗi tôi tự phát hiện và sửa trong quá trình làm lab

Cả hai đều là lỗi thiết kế của chính tôi, phát hiện được nhờ đọc log chứ không nhờ đoán:

1. **Runbook từ chối failover vì Region B chưa ready.** Drill đầu cho `NO_RECOVERY` dù
   health check, snapshot, warm-up đều đúng. Điều kiện đúng phải là *"region chính đã chết"*,
   không phải *"region phụ đã sẵn sàng"* — Region B rỗng là thiết kế có chủ đích, còn
   restore + warm pool chính là việc của `failover()`.
2. **Nhịp poll không đúng `interval`.** `sleep(interval)` chạy sau probe nên một chu kỳ bị
   probe treo kéo dài thành `interval + timeout`. Log vẫn ghi `detect_floor_s: 15.0` trong
   khi sàn thật là 19.8s — số liệu tự mâu thuẫn với chính nó. Sửa xong RTO giảm 33.6s →
   23.3s.

Phân tích định lượng của cả hai nằm ở Case Study 3 và 4 trong phần Reflection.

---

## 4. Action items

| # | Action | Owner | Deadline | Giảm RTO/RPO bao nhiêu giây |
|---|---|---|---|---|
| 1 | Hạ `interval` 5s → 2s, giữ `threshold=3` | SRE on-call | 2 tuần | ~9s detect (15.1 → ~6.1). Đổi lại: tải probe tăng ~2.5× |
| 2 | Giữ sẵn pool region phụ ở `full` (warm pool) để không nạp trong đường lỗi | ML Infra | 1 tháng | ~6.3s. Đổi lại: luôn giữ tài nguyên GPU cho region phụ, tốn ~50% capacity |
| 3 | `state/replicate.py --every 30` → `--every 10` | Data | 1 tháng | RPO 4.0s → ~1.3s. Đổi lại: 3× số lần copy vector DB |
| 4 | Hạ `timeout` probe 2.0 → 1.0s trong health checker | SRE on-call | 2 tuần | ~1–3s detect. Đổi lại: dễ coi là timeout khi region chậm |
| 5 | Hạ `EDGE_TTL_SECONDS` 5 → 1 | Platform | 2 tuần | ~0.8s. Lợi ích nhỏ vì TTL không còn là thành phần lớn |
| 6 | Bổ sung circuit breaker + cooldown cho failover | SRE | 1 tháng | Không giảm RTO, nhưng ngăn flap 2 chiều khi cả hai region chập chờn |
| 7 | Chạy chaos `--mode stop` (SIGKILL) song song với `netblock` | SRE | 2 tuần | Không giảm RTO — dùng để chứng minh RTO của `stop` thấp hơn `netblock` như thiết kế |
| 8 | Viết test hồi quy cho nhịp poll của health checker | SRE | 1 tháng | Không giảm RTO — ngăn tái phát lỗi Case Study 4 |

Hai hàng đầu là cú cắt lớn nhất còn lại: khoảng 15s nếu làm cả hai, đưa RTO từ 23.3s xuống
~8s. Hàng 2 đáng cân nhắc nhất nếu RTO phải < 10s.

---

## 5. Ba câu hỏi bắt buộc

**1. `interval × threshold` là bao nhiêu, chiếm bao nhiêu % RTO?**

Sàn lý thuyết `5s × 3 = 15.0s`. Run này detect lúc **+15.1s**
(`reports/health-events.jsonl:2`) — chiếm **15.1/23.3 ≈ 65% RTO**. Trước khi sửa lỗi nhịp
poll, số này là 19.8s (59% của RTO 33.6s) — tức **một dòng code sai làm RTO dài thêm 4.7s
mà log vẫn khai báo sàn là 15.0s.**

**2. Nếu hạ interval xuống 1s, RTO giảm bao nhiêu — và trả giá gì?**

Sàn mới `1s × 3 = 3s`, detect thực tế khoảng **3.1s** → giảm ~12s RTO (23.3 → ~11.3). Giá
phải trả: số probe tăng **5×** (từ 12 lên 60 lần/phút cho 2 region). Nguy hiểm hơn, khoảng
thời gian *quan sát được* trước khi kết luận ngắn lại khiến hệ thống dễ coi nhầm một lần
timeout chậm là outage thật. Nếu hạ thêm `threshold` xuống 1–2 thì false-positive tăng vọt
và failover sẽ flap giữa hai region — đúng anti-pattern mà §4 cảnh báo. Đây là lý do
runbook phải **bán tự động** thay vì full-auto.

Đo thực nghiệm còn cho thấy: với probe treo 2.0s, detect chỉ vượt sàn 0–5s, nên **hạ
`interval` hiệu quả hơn hạ `threshold`** — cùng làm giảm sàn, nhưng `threshold` tăng nguy cơ
flapping.

**3. Nếu outage kéo dài 6 giờ và region chính mất dữ liệu vĩnh viễn, `docs_lost` có nghĩa gì với khách hàng?**

Trong lab này `docs_lost = 2` và `rpo_seconds = 4.0` (`reports/failover-events.jsonl:2`) —
nhỏ, vì replication `--every 30` chạy liên tục nên chỉ vài giây dữ liệu nằm ngoài snapshot.
Nhưng con số đó **chỉ đúng khi snapshot còn tồn tại**. Nếu Region A mất dữ liệu vĩnh viễn
trong 6 giờ, `state/_replica/` thành nguồn dữ liệu duy nhất và `docs_lost` sẽ là *toàn bộ*
số ticket đã ingest trong 6 giờ đó — với `--rate 0.5` tức khoảng **10.800 ticket**.

Với hệ thống thật, "mất 2 doc" và "mất 10.800 ticket" là hai sự cố khác nhau về bản chất:
cái thứ nhất là sự cố hạ tầng xử lý được bằng runbook, cái thứ hai là mất dữ liệu kinh doanh
không hoàn tác được. Vì vậy RPO phải được đặt như **cam kết với khách hàng** (bao nhiêu phút
dữ liệu được cam kết không mất), không phải một con số kỹ thuật tự chọn — và đó là lý do
`state/replicate.py` phải chạy *trước* drill: không có snapshot thì bước 2 chết và RTO trở
thành vô hạn.