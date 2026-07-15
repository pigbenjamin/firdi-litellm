# GPU 使用率優化與自動擴縮（討論文件）

> **狀態：討論中，未定案。** 本文件整理可行方案與決策點，作為之後施作方向的參考，
> 尚未進行任何變更。定案後應更新本文件的「決策點」章節並記錄結論。
>
> 整理日期：2026-07-06

## 目標

1. 盡可能吃滿 GPU（提高單卡吞吐，不讓算力閒置）
2. 當某個 vLLM server 負載過高時，K8s 自動為它擴出副本；副本之間可靈活調度

## 現況盤點（2026-07-06）

**硬體**
- 目標環境：6× NVIDIA RTX PRO 6000 Blackwell Workstation Edition 96GB
- 目前 dev 機（ai-x-dev, k3s 單節點）只有 2 張卡，跑著 gemma-4-26b
- 工作站卡無 NVLink，`NCCL_P2P_DISABLE=1`，TP 的 all-reduce 走 host memory（PCIe 稅）

**既有 GPU 配置（4 卡規劃）**
| 服務 | GPU | 模型 | 關鍵參數 |
|---|---|---|---|
| gemma-4-31b-vllm | 2（TP=2） | google/gemma-4-31B-it（思考型） | max-num-seqs=2, gpu-mem-util=0.85, max-model-len=65536 |
| gemma-4-26b-vllm | 1 | google/gemma-4-26B-A4B-it（MoE 快捷型） | max-num-seqs=256, gpu-mem-util=0.85 |
| embed-vllm | 1 | google/embeddinggemma-300m | gpu-mem-util=0.15（300M 小模型，卡上大量餘裕） |

**基礎設施缺口**
- 叢集只有 metrics-server（僅支援 CPU/RAM 的 HPA），**沒有 Prometheus、沒有 KEDA**
- vLLM deployment 上的 `prometheus.io/scrape` annotation 目前沒有東西在收
- 要做「按負載擴副本」必須先補監控/擴縮基礎設施（見第三層）

## 觀念校正：vLLM 的「塞滿」是什麼

這直接決定監控與擴縮訊號的選擇（「監控 request 還是 GPU？」→ **request**）：

1. **GPU memory 不能當訊號**：vLLM 啟動時就按 `gpu-memory-utilization` 一次性預留 VRAM
   當 KV cache。不管有沒有請求，nvidia-smi 看到的記憶體永遠是「滿」的。
2. **GPU % 也不好用**：continuous batching 下只要有任何 decode 在跑，SM 利用率就衝高。
   1 個請求和 50 個請求看起來差不多，區分不出「忙」和「爆」。
3. **正確訊號是 vLLM 的 request-level metrics**（`/metrics` 端點已內建）：
   - `vllm:num_requests_waiting` — 排隊中請求數，**業界標準擴縮訊號**
   - `vllm:gpu_cache_usage_perc` — KV cache 用量，>90% 代表快要開始搶佔（preemption）
   - `vllm:num_requests_running` — 在跑的併發數

因此「讓 GPU 塞滿」的手段依序是：**先讓每張卡同時吃更多請求（加大 batch）**，
其次才是加副本。

---

## 第一層：單副本吞吐調參（最大槓桿，零基礎設施成本）

### 1a. gemma-4-31b 的 `--max-num-seqs=2` 是最大瓶頸

現值代表這台 server **最多同時處理 2 個請求**，第 3 個開始排隊。
估算：兩張 96GB、bf16 權重約 62GB（TP=2 每卡 ~31GB），0.85 預留下每卡還有 ~50GB
KV cache 空間，撐 2 個併發是嚴重浪費。

- 建議：拉到 **32–64**，讓 vLLM scheduler 自己按 KV cache 空間決定實際併發。
- 在談任何 autoscaling 之前，先改這個。

### 1b. `gpu-memory-utilization` 0.85 → 0.90–0.92

卡是獨佔的，沒理由留 15%。KV cache 越大，可併發序列越多。
不建議超過 0.92–0.95：要留空間給 CUDA graph 與碎片。

### 1c. `--max-num-batched-tokens=8192`（31b）偏保守

拉高（16384–32768）可提升吞吐；代價是尖峰時 TTFT 略增。快捷型 26b 已是 32768。

### 1d. 【關鍵提案】31b 改 FP8 量化 + TP=1

Blackwell 原生支援 FP8。31B FP8 權重約 31GB，單張 96GB 卡放得下且還有 50GB+ 給
KV cache。好處有三：

1. 免除 TP=2 的 PCIe all-reduce 開銷（`NCCL_P2P_DISABLE=1` 下每層都在付），
   TP=1 反而可能更快
2. **擴縮顆粒度從「2 卡」變「1 卡」**——任何一張空卡都能起任何模型的副本，
   這是「靈活調度」的關鍵
3. 同樣 6 張卡能養的副本數翻倍

代價：輕微精度損失，**需先做品質評測**。可用 `--quantization fp8`（線上量化）直接試，
若品質可接受再考慮找官方/社群 FP8 checkpoint。

### 1e. 其他

- 確認 prefix caching 已啟用（vLLM V1 預設開）
- 調參後 `max-num-seqs`/`max-num-batched-tokens` 改變會使 torch.compile 快取失效，
  預期一次全額重編（冷啟動 ~14 分鐘，之後恢復暖啟動）

---

## 第二層：6 卡布局

| 方案 | 固定配置 | 浮動池 | 擴縮彈性 |
|---|---|---|---|
| **A（保守，31b 維持 TP=2）** | GPU 0-1: 31b（TP=2）<br>GPU 2: 26b<br>GPU 3: embed | GPU 4-5（2 張） | 26b 可 +2 副本；31b +1 副本要一次佔兩張浮動卡，會和 26b 互搶 |
| **B（推薦，31b 改 FP8 TP=1）** | GPU 0: 31b<br>GPU 1: 26b<br>GPU 2: embed | GPU 3-5（3 張） | 三張浮動卡，31b/26b 任意組合，最多 +3 副本 |

方案 B 依賴第一層 1d 的品質評測通過。

**浮動池競爭**：26b 的第 N 副本和 31b 的第 2 副本會搶同一張卡。解法擇一：
- K8s `PriorityClass` 定優先序（誰重要誰先搶到）
- 每個模型設定各自的擴縮上限，讓總和不超過浮動卡數（簡單、可預測，建議先用這個）

---

## 第三層：自動擴縮機制

### 選型：Prometheus + KEDA

- **Prometheus**（kube-prometheus-stack helm chart）：收 vLLM `/metrics`
  （scrape annotation 已就緒）
- **KEDA**（helm chart）：比 prometheus-adapter 簡單，每個模型一個 `ScaledObject`，
  直接用 PromQL 當觸發條件；底層仍是產生 HPA
- **DCGM-exporter**（可選）：GPU% / VRAM 儀表板用，**不拿它做擴縮決策**

### 政策示意（以 26b 為例）

- **Scale up**：`avg(vllm:num_requests_waiting{app="gemma-4-26b-vllm"}) > 5`
  持續 1–2 分鐘 → 副本 +1（上限 = 分配到的浮動卡數）
- **Scale down**：等待佇列歸零**持續 20–30 分鐘**才縮回（stabilization window 拉長）

ScaledObject 草稿（示意，未定案）：

```yaml
apiVersion: keda.sh/v1alpha1
kind: ScaledObject
metadata:
  name: gemma-4-26b-vllm
  namespace: ai-platform
spec:
  scaleTargetRef:
    name: gemma-4-26b-vllm
  minReplicaCount: 1
  maxReplicaCount: 3          # = 1 固定 + 分配到的浮動卡數
  cooldownPeriod: 1800        # 30 分鐘慢縮
  triggers:
    - type: prometheus
      metadata:
        serverAddress: http://prometheus.monitoring:9090
        query: avg(vllm:num_requests_waiting{app="gemma-4-26b-vllm"})
        threshold: "5"
```

### 必須正視：冷啟動延遲

實測：暖啟動（compile cache 已在 hostPath）2–4 分鐘、冷啟動 ~14 分鐘。
**反應式擴容起來時，短促的流量尖峰可能已經結束。** 對策三檔，按複雜度排：

| 檔位 | 做法 | 適用 | 代價 |
|---|---|---|---|
| 1. 快擴慢縮 | 門檻設低早點擴、縮回設 20–30 分鐘 | 忙碌期以小時計的日間負載 | 尖峰前 2–4 分鐘仍要排隊 |
| 2. 常駐超額配置 | 26b 直接固定 2 副本，放棄反應式 | 流量型態穩定 | 佔掉一張浮動卡（反正卡閒著也是閒著） |
| 3. vLLM sleep mode（進階） | `--enable-sleep-mode` 讓待命副本把權重 offload 到 CPU RAM，喚醒秒級 | 第二階段題目 | 要自寫小 controller 整合擴縮訊號 |

### 其他注意

- 副本擴縮與 `strategy: Recreate` 不衝突（strategy 只影響滾動更新，不影響 HPA 加減副本）
- 新副本共用 hostPath 的 HF cache 與 compile cache，不會重複下載/重編——**僅單節點成立**
- 過去有 DiskPressure 驅逐前科：擴副本不增加磁碟壓力（共用 cache），但裝
  Prometheus 要注意其 TSDB 保留期設小（如 15d）

---

## 第四層：多副本之後的連帶問題

- **負載分配**：LiteLLM → K8s Service 是按 TCP 連線分配，LiteLLM 的 HTTP client
  有 keep-alive 連線池，可能黏在同一個 pod。2–3 副本規模先用普通 Service、
  觀察 per-pod metrics；若明顯偏斜再在前面加 least-request LB（叢集已有 Traefik 可用）。
  更進階的 prefix-aware routing（vLLM production-stack / llm-d）等規模長大再說。
- **Prefix cache 稀釋**：請求分散多副本後各副本命中率下降，多副本邊際效益略低於線性。
  正常現象，先知道就好。
- **LiteLLM `num_retries: 2`**：後端飽和時重試會放大負載（雪崩）。建議改成只對
  連線錯誤重試、對 timeout 不重試。
- **部門層 in-memory rate limit**：LiteLLM 仍單副本所以不受影響；若哪天 LiteLLM
  也要多副本，要先把 rate limit 改成共享存儲（如 Redis）。

---

## 決策點（待拍板）

| # | 問題 | 選項 / 建議 | 結論 |
|---|---|---|---|
| 1 | 6 張卡是單機還是多節點？ | 影響最大：多節點 hostPath cache 不共享（每節點重下載+重編譯）、TP 不能跨節點。本文件方案假設**單機** | （未定） |
| 2 | 31b 是否接受 FP8 換 TP=1？ | 強烈建議至少測一輪品質（線上量化即可試） | （未定） |
| 3 | 擴縮策略檔位？ | 反應式（快擴慢縮）／常駐 2 副本／混合（26b 常駐 2、31b 反應式） | （未定） |
| 4 | 浮動池優先權 | 搶卡時 26b vs 31b 誰優先；建議先用「各自上限」制 | （未定） |
| 5 | 是否引入 Prometheus + KEDA | 約兩個 helm chart，資源開銷不大；做反應式擴縮的前提 | （未定） |

## 建議組合與施作順序（僅建議，未定案）

**組合：方案 B + 26b 常駐 2 副本 + 31b/26b 反應式擴到浮動池 + KEDA 快擴慢縮**

1. **第一層調參**（改幾個參數就有感）：31b `max-num-seqs` 2→32+、
   `gpu-memory-utilization` 0.85→0.90；同時做 FP8 品質評測（決策點 2）
2. **裝監控**：kube-prometheus-stack（+ 可選 DCGM-exporter），先看清楚實際負載型態
   再定擴縮門檻
3. **上 KEDA**：按觀察到的負載型態寫 ScaledObject，決定檔位（決策點 3）
4. **視偏斜情況**補 least-request LB / 進階路由（第四層）
