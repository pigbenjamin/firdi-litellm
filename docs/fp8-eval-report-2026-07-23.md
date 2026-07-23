# 31b FP8 品質評測報告（2026-07-23）

> 對應 `docs/gpu-optimization.md` 決策點 2 的前置作業。評測方法、腳本見
> `scripts/fp8_eval/`（`prompts.json` / `run_eval.py` / `judge.py`）。

## 結論：品質關卡 **通過**，建議可以進入正式切換

18 題涵蓋一般問答、thinking 開關、tool-calling、長文本理解、程式碼/數學、格式邊界情況，
FP8（`--quantization fp8 --tensor-parallel-size=1`）與現行 bf16（`--tensor-parallel-size=2`）
在功能正確性上沒有發現任何退化；差異都落在人工可判斷為「風格偏好」的範圍內。

## 評測方式

- **基準（baseline）**：`gemma-4-31b-vllm` 正式 deployment（bf16, TP=2, gpu-memory-utilization=0.9,
  max-num-seqs=20, max-num-batched-tokens=16384, enable_thinking 預設 false）
- **候選（candidate）**：獨立臨時 deployment `gemma-4-31b-fp8-test`（同一組 max-num-seqs/
  max-num-batched-tokens，只有 quantization+TP 不同；gpu-memory-utilization 因為當時測試共用
  GPU1 上還有 ollama 佔用而保守設 0.75——見下方「數據侷限」）
- 18 題各自跑一次，**兩邊都是全新冷啟動後**執行；輸出交給本地 `gemma-4-26b-vllm` 當 judge 做
  pairwise 比較（順序隨機避免位置偏誤），再人工複核所有非 tie 案例 + 抽樣 3 題 tie 案例

## Judge 結果

| 結果 | 題數 |
|---|---|
| Tie（無差異） | 14 |
| bf16 勝 | 3 |
| FP8 勝 | 1 |
| 需人工複核（judge JSON 解析失敗） | 0 |
| 生成本身失敗 | 0 |

## 人工複核：4 個非 tie 案例逐一檢視

| ID | Judge 判定 | 人工複核結論 |
|---|---|---|
| `think-04`（TP 通訊延遲說明題） | FP8 勝 | FP8 回答邏輯結構確實更清楚，屬真實優勢 |
| `tool-03`（閒聊，不該觸發 tool） | bf16 勝，但 judge 標記 `language_mixing` | **複核發現這個瑕疵其實出在 bf16**：bf16 輸出裡混入了一個無意義英文字「aprove」（`拍美照（ aprove 良好光線）`），FP8 輸出完全乾淨。Judge 仍判 bf16 小勝是因為它推薦了更具體的地標（象山看101），這是主觀偏好判斷，**不代表 FP8 有功能性退化**——方向剛好相反 |
| `code-01`（union function 實作） | bf16 勝 | 兩邊程式邏輯都正確，差異只在 bf16 的測試案例多考慮了 `set` 轉 `list` 順序不固定的比較方式，屬於程式碼風格細節，不是功能缺陷 |
| `edge-01`（強制純 JSON 輸出） | bf16 勝 | 兩邊都正確符合「純 JSON、只有 summary/risk_level 兩欄」的格式要求；bf16 的 summary 內容多提了一句「需要支援 FP8 的硬體（如 H100）」，但這台機器用的是 RTX PRO 6000 不是 H100，這句話對這個情境其實沒有太大意義，只是內容比較長，不是內容比較對 |

抽樣複核的 3 個 tie 案例（`qa-01`、`think-02`、`long-01`，含中文問答、數學推理、長文本摘要）確認
兩邊輸出都正確、完整，僅措辭不同。

**修正一個腳本標籤問題**：`judge.py` 目前把所有帶 `regression_flags` 的案例都印成
`fp8_flagged_regression_ids`，但 flag 描述的是「judge 發現的瑕疵」不分左右邊——這次 `tool-03`
的瑕疵其實在 bf16 那邊，腳本標籤方向反了。之後如果要重複使用這個腳本評測其他模型，建議把
regression_flags 的歸屬（baseline 側/candidate 側）也記錄進去，而不是只記一個全域列表。

## 效能對比（僅供參考，非最終基準）

| 指標 | bf16 TP=2 | FP8 TP=1 |
|---|---|---|
| 平均 TTFT | 120.0ms | 99.0ms（快 ~17%） |
| 平均 tokens/s（單一請求，非併發） | 41.8 | 41.8（持平） |

**數據侷限**：
1. 這次 18 題是**逐題序列送出，非併發**，只測到單一請求的生成速度，測不到
   `docs/gpu-optimization.md` 真正在意的「同時吃更多請求」的併發吞吐——那要等正式切換、接上
   `vllm:num_requests_waiting` 等 KEDA 訊號後才看得出來。
2. FP8 測試 pod 的 `gpu-memory-utilization` 設 0.75（因為當時和 ollama 共用 GPU1），比正式 bf16
   的 0.9 低，KV cache 空間較小；正式切換後若拿到完整一張卡、調到跟 bf16 一樣的 0.9~0.92，
   併發表現預期會比這次測到的更好。
3. TTFT 變快與原文件假設一致：TP=1 免除 `NCCL_P2P_DISABLE=1` 下 TP=2 的 PCIe all-reduce 開銷。

## 這次評測過程中發現並處理的兩個非預期問題

這兩項跟 FP8 品質本身無關，但值得記錄：

1. **`enable_thinking` git/叢集漂移**：commit `f973b05`（2026-07-22）把 yaml 改成
   `enable_thinking: false`，但當時只改了檔案沒有 `kubectl apply`，叢集上一直跑著改動前的
   `true`。這次跑 bf16 基準前發現並用 `kubectl apply` 補上，現在兩邊一致。
2. **GPU1 硬體故障**：bf16 基準第一次嘗試跑到第 3 題（`qa-03`，一個普通翻譯請求）時卡了 9.4
   分鐘後 EngineCore 崩潰，之後 `nvidia-smi` 顯示 GPU1 進入 `ERR!`／`Product Brand: GPU requires
   reset` 狀態，需要重開機才清除。**值得注意的是：這次故障發生在 bf16 TP=2 的跑測時，FP8
   TP=1 那次完整 18 題全過、沒有觸發任何異常**——不足以下定論說是 TP=2 導致（樣本數只有 1
   次事件，也可能是巧合的硬體隨機故障），但方向上與文件原本「TP=2 在無 NVLink 環境下要付
   PCIe all-reduce 稅」的疑慮相符，可以當作額外一個支持換 FP8+TP=1 的觀察點，而非決定性證據。
   重開機後 GPU1 恢復正常（`Recovery Action: None`），重跑 bf16 基準 18 題全過。

## 建議

- **品質關卡：通過。** 可以進入 `docs/gpu-optimization.md` 下一階段施作順序第 2 項的後半段
  （正式把 `gemma-4-31b-vllm` 切換成 `--quantization fp8 --tensor-parallel-size=1`）——但那是
  需要使用者另外拍板的動作，這次任務範圍只到評測為止。
- 正式切換後建議：維持 `gpu-memory-utilization=0.9`（跟 bf16 一致，不用像這次測試一樣保守），
  並透過 Langfuse 觀察真實流量下的輸出品質，作為這 18 題小樣本評測的延伸驗證。
- GPU1 那次故障建議留意是否再發生；如果之後 TP=2 相關負載又觸發類似 `ERR!` 狀態，那會是比這次
  更強的訊號。
