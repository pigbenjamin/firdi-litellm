# 非 LiteLLM 的 GPU 需求（批次腳本 / fine-tuning）

LiteLLM + vLLM 以外、但一樣要吃 GPU 的工作要怎麼跟線上服務共用這批卡。

解決順序在 2026-07-24 定案、2026-07-29 補上實作骨架：

- **A 案（優先）**：能包成 K8s Job/Pod 的，用 `nvidia.com/gpu` 申請資源 + `gpu-priority-batch`
  加入浮動池排隊，不需要任何手動介入。**這是預設路徑**，範本見同目錄 `job-template.yaml`。
- **B 案**：真的沒辦法包成 Pod、得直接在 host 上跑時才用的手動「拉卡」流程，見下方。

## A 案：包成 K8s Job

```bash
source .env

# 第一次跑之前先建好 workspace PVC（只需一次，見下方「儲存」）
envsubst < k8s/batch/workspace-pvc.yaml | kubectl apply -f -

cp k8s/batch/job-template.yaml k8s/batch/job-my-finetune.yaml
# 改掉檔案裡標了 CHANGE-ME 的 name / image / command / GPU 張數
envsubst < k8s/batch/job-my-finetune.yaml | kubectl apply -f -

kubectl get pods -n ai-platform -l workload=batch-gpu
kubectl logs -n ai-platform -f job/my-finetune
```

範本已經處理好的事情：浮動池 `nodeSelector`、`runtimeClassName: nvidia`、GPU 節點的
tolerations、跟 vLLM 共用同一份 HF 快取、失敗不自動重跑、完成 24 小時後自動清掉 Job 物件。

### 優先權：只吃空卡，永不搶佔（2026-07-29 決定）

`k8s/priorityclasses.yaml` 現在有四層：

| PriorityClass | value | preemptionPolicy | 用途 |
|---|---|---|---|
| `gpu-priority-high` | 1000 | PreemptLowerPriority | gemma-4-31b |
| `gpu-priority-medium` | 500 | PreemptLowerPriority | gemma-4-26b |
| `gpu-priority-low` | 100 | PreemptLowerPriority | light-models（embed + marker） |
| **`gpu-priority-batch`** | **50** | **Never** | **批次 / fine-tuning** |

`preemptionPolicy: Never` 比 value 數字更重要：批次工作只會**排隊等**浮動池出現空卡，
永遠不會為了自己排上去而驅逐任何線上服務。這是實測過代價後的選擇——31b 擴副本搶掉 26b
那次，26b 離線 12 分鐘（排隊等 GPU + 暖啟動），批次工作不值得付這個代價。

方向性是刻意單向的：`Never` 只擋「批次主動搶別人」，不擋「別人搶批次」。value=50 低於
所有 vLLM class，所以 31b/26b/light-models 要卡時可以隨時把批次工作踢掉。**線上服務永遠贏。**

實務後果，送工作前要有心理準備：

- GPU 全滿時，Job 會**無限期停在 `Pending`**（`Insufficient nvidia.com/gpu`），不會有任何
  東西幫它讓位。`kubectl describe pod` 看得到原因。
- 跑到一半可能**隨時被驅逐**。長時間訓練請自己做 checkpoint／斷點續跑；範本給了 60 秒
  `terminationGracePeriodSeconds` 讓 SIGTERM 後有機會存檔。
- `backoffLimit: 0` 表示被驅逐後**不會自動重跑**（跑一半的 checkpoint 自動重試容易變髒狀態）。
  確定 idempotent 的工作再自己調高。

### 儲存：動態佈建 PVC（2026-07-29 決定）

資料集與 checkpoint 走 `k8s/batch/workspace-pvc.yaml` 的 `batch-workspace` PVC，
`storageClassName` 比照專案其他 PVC 讀 `.env` 的 `K8S_PVC_STORAGE_CLASS`（開發機
`local-path`、公司叢集 `rook-ceph-block`）。範本已經掛好在 `/workspace`。

不用 hostPath（hf-cache 那種）的原因：批次工作走浮動池，可能被排到任一個
`gpu-pool=shared` 節點；hostPath 綁死單一機器，job 換節點就讀不到自己的資料。Ceph RBD
是網路儲存，PVC 會跟著 pod 移動，這才配得上浮動池。

**ReadWriteOnce 的實際限制**（`rook-ceph-block` 是 Ceph RBD block 儲存，不支援 RWX）：

| 情境 | 做法 |
|---|---|
| 一次跑一個批次工作 | 直接用 `batch-workspace`，不用改 |
| 同時跑多個工作 | 複製 `workspace-pvc.yaml` 改 `name`，各自一塊。**不能共用同一塊** |
| 多個工作同時讀同一份資料集 | RBD 做不到，需要 CephFS 之類支援 RWX 的 storage class，屆時再評估（跟 `marker-ingest-pvc` 卡在同一個限制） |

容量預設 50Gi，是還沒決定第一個工作要跑什麼之前的起始值——fine-tuning 的資料集加上多份
checkpoint 很容易超過，確定工作內容後直接改 `workspace-pvc.yaml` 再 apply
（PVC 擴容需要 storage class 有 `allowVolumeExpansion`，`rook-ceph-block` 通常有開）。

HF 模型權重不用另外處理，範本已經掛了跟 vLLM 共用的 `hf-cache` hostPath。

## B 案：手動拉卡（無法包成 Pod 時）

兩級粒度：

- **B-1 整節點拉出**（現成、優先用）：
  ```bash
  kubectl label node <name> gpu-pool-      # 移除 label，新的浮動 pod 不再排過去
  ```
  **注意這不會趕走已經在跑的 pod**——既有的浮動副本會留在原地，要另外手動驅逐才會真的
  空出 GPU。做完記得 `scripts/label-nodes.sh` 貼回去。
- **B-2 單張卡拉出**（尚無工具）：這個叢集用標準 NVIDIA device plugin，`nvidia.com/gpu`
  是 count-based 總數、不支援指定卡號。要拉單張卡得改該節點 device plugin 的可見裝置
  清單並重啟它的 pod。目前沒有腳本，等真的常態需要時才值得寫（例如
  `scripts/gpu-pool.sh cordon-gpu <node> <index>`）。

## 刻意還沒做的事（2026-07-29 決定：先跑第一個 job 再說）

以下三項評估過、確認是真實缺口，但決定等實際需求浮現再補，不預先做：

1. **沒有任何 `ResourceQuota`**：`PriorityClass` 是 cluster-scoped，**目前任何 namespace 的
   任何 pod 都可以宣告 `gpu-priority-high` 去搶 26b 的卡**。要限制的話是用 `ResourceQuota`
   的 `scopeSelector` 綁 PriorityClass。批次工作只要乖乖用 `gpu-priority-batch` 就沒事，
   但這條規則現在**沒有任何機制強制執行**，靠約定。
2. **沒有 dcgm-exporter**：現有 Prometheus 只收 vLLM 自報的指標（排隊數、KV cache）。批次
   工作不會吐這些，等於**跑起來之後 GPU 層面是全黑的**——看不到使用率、溫度、ECC/Xid。
   （2026-07-28 的 Xid 120 事故就是現有監控偵測不到的類型。）
3. **Prometheus scrape 範圍鎖在 `ai-platform` namespace**：批次工作若放到別的 namespace，
   連 pod 都不會被收，得同時放寬 RBAC 與 `k8s/monitoring/configmap.yaml` 的 scrape config。
