# GPU 使用率優化與自動擴縮（討論文件）

> **狀態：核心方向已定案（2026-07-21），細節待下一階段施作。** 本文件整理可行方案與決策點；
> 「決策點」章節已記錄目前結論，尚未進行對應的部署變更。
>
> 整理日期：2026-07-06，決策更新：2026-07-21

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

## 第五層：節點拓樸與落地細節（2026-07-21 確認）

### 節點拓樸

- 確定為 2 節點：**GPU01**（4 張卡）、**GPU02**（2 張卡），共 6 張；不排除之後再加節點。
- TP 不能跨節點（沒有 NVLink/RDMA 等級的節點間互連），任何 TP>1 的模型必須整組落在同一節點。
- 31b（TP=2，或改 FP8 後 TP=1）**建議釘在 GPU01**：GPU01 有 4 張卡，扣掉 31b 用量後還能留浮動空間；
  若釘在只有 2 張卡的 GPU02，會把該節點的浮動池直接歸零。

### nodeSelector 要寫在哪裡

分兩層，不要混在一起：

1. **Node 物件本身要先貼 label**——不是寫在 Deployment yaml 裡，是對 Node 下的一次性操作：
   ```
   kubectl label node <GPU01-hostname> gpu-pool=shared
   kubectl label node <GPU02-hostname> gpu-pool=shared
   ```
   建議包成 `scripts/label-nodes.sh`（或併入 `deploy.sh` 初始化步驟）寫進 repo，避免變成沒人記得的
   手動指令；未來加新節點只要多跑一次。
2. **Deployment 的 `spec.template.spec.nodeSelector`**——位置跟現在完全一樣，只是 value 依模型是否
   要浮動而不同：
   - **固定釘死的模型**（31b、light-models——light-models 的 hostPath PVC 已用 nodeAffinity 釘死
     節點，本來就不能浮動）：維持 `kubernetes.io/hostname: <特定值>`，但要把 `.env` 的單一
     `K8S_GPU_NODE_HOSTNAME` 拆成 `K8S_GPU01_HOSTNAME` / `K8S_GPU02_HOSTNAME`。
   - **可浮動的模型**（26b、未來新服務）：改用 `nodeSelector: {gpu-pool: shared}`，符合這個 label
     的節點都能落，由 K8s scheduler 挑有空卡的節點排。

### 冷啟動快取的生效範圍（再次確認）

「付過一次冷啟動、之後都是暖啟動」**成立，但有範圍限制，不是一次永久有效**：

- **只在同一節點成立**：`VLLM_CACHE_ROOT` 指向 hostPath（節點本地磁碟），GPU02 沒跑過的模型，就算
  GPU01 已經暖過，GPU02 照樣要重新付一次冷啟動。多節點下等於「每個節點各自要暖一次」。
- **只在同一份「模型+編譯相關參數」組合成立**：vLLM 依模型+參數雜湊分快取子目錄，任何影響 compile
  graph 的參數改變（量化方式、`max-num-seqs`、`max-model-len` 等）都會變成 cache miss，等同一次新的
  冷啟動。**這次的 FP8 轉換就會觸發一次全額重編**，是預期成本，換完之後才會回到暖啟動；
  `enable_thinking` 預設值理論上不影響 engine 的 compile graph（只是 chat template 參數），但部署後
  建議留意首次請求是否又觸發重編。
- 前提是 hostPath 目錄沒被清過（磁碟壓力清 cache 的前科要記得）。

### 浮動池優先權的實作落差

「誰的需求大，誰能佔用更多資源」這個原則，K8s 原生工具沒有直接對應的機制，落差在於：

- KEDA/HPA 只回答「這個模型現在該有幾個副本」，多個模型的 ScaledObject 之間**不會互相比較需求
  大小**，資源分配結果其實是「誰的擴容請求先送到 scheduler、誰就先佔到空卡」（搶快，不是搶需求量）。
- 要接近「需求大者得」，K8s 原生能做到的是**靜態優先權 + 搶佔**：用 `PriorityClass` 給每個模型的
  Deployment 定優先權，優先權高的 pod 在資源不足時可以搶佔（驅逐）優先權低的 pod。這是「重要程度」而
  非「即時需求量」的排序——例如定 31b > 26b > 未來新服務，需要時 31b 的新副本能踢掉浮動池上正在跑的
  低優先權副本。
- 真正「即時依佇列長度動態分配」需要自寫 controller，比較各模型的 `num_requests_waiting`，動態調整各
  ScaledObject 的 `maxReplicaCount`——工程量明顯較大。**建議先用 PriorityClass 這個近似版本**，不夠用
  再考慮自訂 controller。
- 為保留「未來加新服務」的彈性：floating pool 用通用 `gpu-pool: shared` label，不針對特定模型寫死節
  點分配；新服務只要掛 GPU request + PriorityClass + ScaledObject 就能自然加入競爭，不用改動既有模型
  的設定。

### 加碼監控 cache 有沒有幫助

分兩種「cache」，都有幫助，但用途不同：

1. **vLLM 的 KV cache 用量**（`vllm:gpu_cache_usage_perc`）：適合當 KEDA 的**輔助擴容觸發**，跟
   `num_requests_waiting` 用 OR 的方式疊加（一個 ScaledObject 可掛多個 trigger，任一超標就擴容）。它
   比排隊訊號更早——排隊之前 KV cache 用量逼近 90%（快搶佔）就能先擴容，縮短反應式擴容「來不及」的
   窗口。不建議單獨用它當唯一訊號：單一超長 context 請求也會把 KV cache 用量衝高，但不代表有排隊需
   求，加副本也解決不了那個請求本身。
2. **hostPath 的 HF cache / compile cache 磁碟用量**：跟 vLLM 無關，是 node-exporter 等級的磁碟監
   控。有幫助，但用途是**告警防再次 DiskPressure**（之前有過全 namespace 被驅逐的前科），不是拿來當
   擴縮訊號——建議裝 Prometheus 時順手接上 node-exporter 磁碟指標，掛「用量 > 80%」告警，不接進 KEDA。

---

## 決策點（2026-07-21 更新）

| # | 問題 | 結論 |
|---|---|---|
| 1 | 節點拓樸 | **已定案**：GPU01（×4）+ GPU02（×2）兩節點，未來可能再加節點；見上方「節點拓樸與 nodeSelector」 |
| 2 | 31b 是否接受 FP8 換 TP=1？ | **已完成**：2026-07-23 品質評測通過（[fp8-eval-report-2026-07-23.md](fp8-eval-report-2026-07-23.md)）後，正式 `gemma-4-31b-vllm` 已切換為 `--quantization fp8` + `--tensor-parallel-size=1`，`enable_thinking` 預設 `false`；舊 bf16 TP=2 設定備份在 `k8s/vllm/gemma-4-31b/deployment.bf16-tp2.yaml.bak` |
| 3 | 擴縮策略檔位？ | **已定案**：全面採「快擴慢縮」；31b 先手動固定副本觀察，26b 優先接 KEDA |
| 4 | 浮動池優先權 | **已定案（原則）**：需求大者得，並保留未來新服務加入的彈性；**實作**：PriorityClass + 搶佔（K8s 原生的近似版本），見上方「浮動池優先權的實作落差」 |
| 5 | 是否引入 Prometheus + KEDA | **已定案**：採用；追加建議疊加 `vllm:gpu_cache_usage_perc` 當輔助擴容 trigger，hostPath 磁碟用量另外做告警（非擴縮訊號） |

## 下一階段施作順序（2026-07-21 定案，待執行）

1. **節點 label 化**：`kubectl label node` 幫 GPU01/GPU02 貼 `gpu-pool=shared`；
   `.env` 的 `K8S_GPU_NODE_HOSTNAME` 拆成 `K8S_GPU01_HOSTNAME` / `K8S_GPU02_HOSTNAME`；
   31b、light-models 改用各自 hostname 釘死節點，26b 改用 `gpu-pool` label 浮動
2. **31b FP8 品質評測 + 正式切換**：**已於 2026-07-23 完成**。評測見
   [fp8-eval-report-2026-07-23.md](fp8-eval-report-2026-07-23.md)（先用獨立臨時
   `gemma-4-31b-fp8-test` deployment 測試，通過後才收編進正式版、刪除臨時目錄）；正式
   `k8s/vllm/gemma-4-31b/deployment.yaml` 現在是 `--quantization fp8` +
   `--tensor-parallel-size=1`，`gpu-memory-utilization=0.9`、GPU request 從 2 降到 1；
   `enable_thinking` 預設 `false`。舊 bf16 TP=2 設定備份在同目錄的
   `deployment.bf16-tp2.yaml.bak`（不會被 `deploy.sh` 的 `*.yaml` glob 誤套用）。
3. **定 PriorityClass**：31b > 26b > 未來新服務，作為「需求大者得」的靜態近似版本
   （K8s 原生沒有動態依佇列長度分配的機制，見「浮動池優先權的實作落差」）
4. **裝監控**：kube-prometheus-stack + node-exporter（磁碟用量告警，防
   DiskPressure 重演）；需另建 PodMonitor（預設不吃現有的
   `prometheus.io/scrape` annotation）
5. **上 KEDA**：先只接 26b，雙 trigger（`num_requests_waiting` +
   `vllm:gpu_cache_usage_perc`），快擴慢縮；31b 先手動固定副本數觀察，
   跑穩再評估要不要也接
6. **視偏斜情況**補 least-request LB / 進階路由（第四層）
