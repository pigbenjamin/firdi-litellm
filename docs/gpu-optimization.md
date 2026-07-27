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
   - `vllm:kv_cache_usage_perc` — KV cache 用量，>90% 代表快要開始搶佔（preemption）
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

1. **vLLM 的 KV cache 用量**（`vllm:kv_cache_usage_perc`）：適合當 KEDA 的**輔助擴容觸發**，跟
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
| 4 | 浮動池優先權 | **已實作（2026-07-24）**：`k8s/priorityclasses.yaml` 建了 `gpu-priority-high`(1000, 31b) / `gpu-priority-medium`(500, 26b) / `gpu-priority-low`(100, light-models) 三層 PriorityClass；31b/26b 的 `nodeSelector` 已改為 `gpu-pool: shared` 並掛對應優先權，見下方「下一階段施作順序」第 1、3 項 |
| 5 | 是否引入 Prometheus + KEDA | **兩者皆已於 2026-07-24 在 ai-x-dev 上線**：Prometheus 精簡版（無 Operator/Grafana）；KEDA 先接 31b（`minReplicaCount=maxReplicaCount=1`，只驗證接線不真的擴容，順序跟原規劃的「先接 26b」相反），雙 trigger 疊加 `vllm:kv_cache_usage_perc`（原文件誤植為 `gpu_cache_usage_perc`，已修正），hostPath 磁碟用量另外做告警（非擴縮訊號） |

## 下一階段施作順序（2026-07-21 定案，待執行）

1. **節點 label 化**：**已於 2026-07-24 在 ai-x-dev（單節點）完成**，實作與原規劃有一處調整：
   `31b` 也改成 `gpu-pool` label 浮動（不只 26b），`light-models` 則維持 hostname 硬釘不變——
   因為 `marker-ingest-pvc` 的 PV 用 `nodeAffinity` 綁死一個 hostname（hostPath 儲存限制），就算
   Deployment 改用 label 選擇器，pod 實際上仍只能落在那個節點，沒有真正的浮動效果，故暫不改動，
   等 marker-ingest 換成 RWX 網路儲存後再一起處理。因此 `.env` 的 `K8S_GPU_NODE_HOSTNAME`
   **沒有**拆成 `K8S_GPU01_HOSTNAME`/`K8S_GPU02_HOSTNAME`（原規劃這步已不需要，因為現在只剩
   light-models 這一個 hostname 硬釘的消費者），維持單一變數即可。新增 `scripts/label-nodes.sh`
   包裝 `kubectl label node <hostname> gpu-pool=shared --overwrite`，多節點時對每台新節點各跑一次。
   到了 GPU01(×4)+GPU02(×2) 正式兩節點拓樸時，兩台都要跑這支腳本；31b 目前沒有硬性規定只能落
   GPU01，會由 scheduler 依浮動池空卡狀況決定，這點與最初「31b 建議釘 GPU01」的構想不同，之後上
   兩節點正式環境前應重新評估是否要收斂回硬性拓樸限制。
2. **31b FP8 品質評測 + 正式切換**：**已於 2026-07-23 完成**。評測見
   [fp8-eval-report-2026-07-23.md](fp8-eval-report-2026-07-23.md)（先用獨立臨時
   `gemma-4-31b-fp8-test` deployment 測試，通過後才收編進正式版、刪除臨時目錄）；正式
   `k8s/vllm/gemma-4-31b/deployment.yaml` 現在是 `--quantization fp8` +
   `--tensor-parallel-size=1`，`gpu-memory-utilization=0.9`、GPU request 從 2 降到 1；
   `enable_thinking` 預設 `false`。舊 bf16 TP=2 設定備份在同目錄的
   `deployment.bf16-tp2.yaml.bak`（不會被 `deploy.sh` 的 `*.yaml` glob 誤套用）。
3. **定 PriorityClass**：**已於 2026-07-24 完成**，`k8s/priorityclasses.yaml`：
   `gpu-priority-high`(1000, 31b) > `gpu-priority-medium`(500, 26b) > `gpu-priority-low`(100,
   light-models，先設定好以便日後真正浮動時直接生效)，作為「需求大者得」的靜態近似版本
   （K8s 原生沒有動態依佇列長度分配的機制，見「浮動池優先權的實作落差」）。注意
   `PriorityClass` 是 cluster-scoped、不分 namespace，任何 namespace 的 pod 都能引用同一組
   class 加入搶佔序列（例如未來的批次/fine-tuning job），目前未加 RBAC/`ResourceQuota`
   限制哪個 namespace 能用哪個 class。
4. **裝監控**：**已於 2026-07-24 在 ai-x-dev 上線，但走精簡版而非 kube-prometheus-stack**——
   使用者當時決定這台開發機磁碟已 91% 滿、且只需要「盯 GPU pod + 磁碟」，不需要完整
   Operator/CRD/Grafana/Alertmanager/kube-state-metrics，改成：
   - 純 `prom/prometheus` Deployment（無 Operator），靠 `kubernetes_sd_configs: role: pod` +
     `relabel_configs` 直接吃現有 vLLM deployment 上早就有的 `prometheus.io/scrape`
     annotation（不需要 PodMonitor CRD）；RBAC 用 namespace-scoped `Role`（非
     `ClusterRole`），scrape 範圍鎖在 `ai-platform` namespace 內
   - `node-exporter` DaemonSet 監控磁碟（`NodeDiskUsageHigh` 告警規則，>80% 觸發，見
     `k8s/monitoring/configmap.yaml`）；**沒裝 Alertmanager**，告警只能在 Prometheus 自己的
     `/alerts` 頁面看，沒有 Slack/email 等通知路由
   - Retention `--storage.tsdb.retention.time=5d` + `--storage.tsdb.retention.size=4GB`
     雙上限、PVC 只給 5Gi——刻意保守，因為磁碟緊
   - 檔案位置：`k8s/monitoring/`（rbac.yaml、configmap.yaml、prometheus-deployment.yaml、
     node-exporter-daemonset.yaml），`./scripts/deploy.sh monitoring` 套用（**不在
     `deploy_all()` 裡**，比照 `openwebui-functions` 視為選配元件）
   - **之後若要正式上 GPU01+GPU02 兩節點且想要 Grafana 儀表板/多方告警路由**，這個精簡版
     不會自動長成 kube-prometheus-stack，需要另外評估是否要換裝（兩者資料模型相容，PromQL
     query 不用改，但物件/RBAC/CRD 要重新規劃）
5. **上 KEDA**：**已於 2026-07-24 在 ai-x-dev 上線，但跟原規劃順序相反**——使用者這次決定
   先接 **31b**（原規劃是先接 26b、31b 先手動觀察）。實作與注意事項：
   - KEDA operator 用 Helm 裝（`helm install keda kedacore/keda --namespace keda
     --create-namespace`），獨立 `keda` namespace，不在 `deploy.sh` 管理範圍內（一次性
     cluster bootstrap）
   - `k8s/keda/scaledobject-gemma-4-31b.yaml`：雙 trigger（`vllm:num_requests_waiting`
     threshold=5 + `vllm:kv_cache_usage_perc` threshold=0.9），`./scripts/deploy.sh keda`
     套用（**不納入 `deploy_all()`**，跟 monitoring/openwebui-functions 一樣是選配步驟）
   - **`minReplicaCount = maxReplicaCount = 1`，刻意不放大**：這台開發機只有 2 張 GPU，
     31b/26b 各佔一張已滿載；31b 的 `priorityClassName`（`gpu-priority-high`）比 26b
     （`gpu-priority-medium`）高，若放大 `maxReplicaCount` 讓 31b 真的擴出第 2 副本，
     資源不足時 K8s 會直接搶佔驅逐 26b 來騰出 GPU，中斷正在使用 26b 的使用者。目前只是把
     KEDA/HPA/Prometheus 查詢這條線接通、驗證數值正確讀得到，不會真的觸發任何擴容；
     之後要真的放大 `maxReplicaCount`，要先想清楚跟 26b 搶卡這件事怎麼處理（回頭看「浮動池
     優先權的實作落差」）
   - **修正一個文件錯誤**：本文件先前多處寫的 `vllm:gpu_cache_usage_perc` 是錯的，實測這版
     vLLM（v0.23.0）暴露的正確 metric 名稱是 **`vllm:kv_cache_usage_perc`**，已全文修正
   - **26b 已於同日跟進接上**（`k8s/keda/scaledobject-gemma-4-26b.yaml`），同樣
     `minReplicaCount=maxReplicaCount=1`，只驗證接線不真的擴容；兩個 ScaledObject
     現在都是 `Ready=True`，HPA 都能正確讀到即時數值，兩邊既有 pod 皆未被動到

   ### 之後要真的測試觸發擴容時（2026-07-24 討論，尚未執行）

   目前兩個 ScaledObject 都是 `minReplicaCount=maxReplicaCount=1`，不會真的擴容。之後想實際驗證
   KEDA 有沒有真的接對、觸發時的行為，先看清楚兩邊風險不對稱：

   | 放大對象 | 觸發後果 | 風險 |
   |---|---|---|
   | **26b**（`gpu-priority-medium`）想擴第 2 副本 | 這台機器沒有空 GPU，26b 搶不贏 31b（優先權更高），新副本卡在 `Pending`，**不會**踢掉任何人 | 安全，頂多擴容失敗 |
   | **31b**（`gpu-priority-high`）想擴第 2 副本 | 一樣沒有空 GPU，但 31b 優先權更高，K8s 會**直接搶佔驅逐正在跑的 26b pod** 來騰出卡 | **會中斷正在用 26b 的使用者**——這是 PriorityClass 設計上刻意的行為，不是 bug |

   **建議：先測 26b（安全），31b 的觸發測試要挑維護時段**（因為會真的踢掉 26b）。測試步驟：

   1. 把要測的模型的 `maxReplicaCount` 調大（例如 26b 改成 2），`kubectl apply -f
      k8s/keda/scaledobject-gemma-4-26b.yaml` 套用即可，不用重啟現有 pod
   2. 產生足夠併發請求，把 `vllm:num_requests_waiting` 推過 5、或 `vllm:kv_cache_usage_perc` 推過
      0.9——併發數要超過該模型的 `max-num-seqs`（26b 是 256，門檻較高；31b 較容易觸發），可以寫
      小腳本背景併發打長回覆的 chat completion，或用 `hey`/`vegeta` 這類工具直接打 vLLM service
   3. 一邊用 `kubectl get hpa -n ai-platform -w` 或 Prometheus UI
      （`kubectl port-forward -n ai-platform svc/prometheus 9090:9090`）盯著數值，觀察 KEDA
      是否真的把 replica 數推上去、新 pod 排到哪裡；測 31b 的話同時觀察 26b 是否真的被驅逐
      （`kubectl get pods -n ai-platform -w`）
   4. 測完把 `maxReplicaCount` 改回 1，恢復現在這個安全狀態
6. **視偏斜情況**補 least-request LB / 進階路由（第四層）
