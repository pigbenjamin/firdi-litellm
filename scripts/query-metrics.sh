#!/usr/bin/env bash
# 查詢 Prometheus（k8s/monitoring/）的 metrics，不用 port-forward，直接透過
# kubectl exec 進 Prometheus pod 打它自己的 localhost API。
#
# 用法：
#   ./scripts/query-metrics.sh query <promql>
#       即時查詢，印出每條序列現在的數值
#   ./scripts/query-metrics.sh range <promql> [start] [end] [step秒]
#       區間查詢，印出每條序列在這段時間內的數值變化
#       start/end 可用 RFC3339（例如 2026-07-28T00:00:00Z）或省略
#       省略時預設查最近 1 小時、step=60 秒
#   ./scripts/query-metrics.sh help
#       印出常用 PromQL 查詢參考（下方 print_promql_reference）
#
# 範例：
#   ./scripts/query-metrics.sh query 'vllm:num_requests_waiting'
#   ./scripts/query-metrics.sh range 'sum(rate(vllm:request_success_total{app="gemma-4-31b-vllm"}[5m])) by (app)'
#   ./scripts/query-metrics.sh range 'vllm:kv_cache_usage_perc' 2026-07-28T00:00:00Z 2026-07-28T13:00:00Z 300
set -euo pipefail

NS="ai-platform"
MODE="${1:-}"
QUERY="${2:-}"

print_promql_reference() {
    cat <<'EOF'
常用查詢參考——都是可以直接複製貼上執行的完整指令
（app 標籤可用值：gemma-4-31b-vllm / gemma-4-26b-vllm / light-models-vllm）

── KEDA 擴縮訊號（gauge，數值就是字面上的意思，不用 rate()）──────────────────

排隊中的請求數。KEDA 門檻是 5（見 k8s/keda/scaledobject-*.yaml）：
  ./scripts/query-metrics.sh query 'vllm:num_requests_waiting{app="gemma-4-31b-vllm"}'

正在跑的請求數（併發忙碌程度）：
  ./scripts/query-metrics.sh query 'vllm:num_requests_running{app="gemma-4-31b-vllm"}'

KV cache 用量，0.0~1.0。KEDA 門檻是 0.9：
  ./scripts/query-metrics.sh query 'vllm:kv_cache_usage_perc{app="gemma-4-31b-vllm"}'

── 流量/吞吐量（counter，一定要包 rate()，否則看到的是「從開機累加到現在」的總數）──

每秒完成請求數過去 1 小時趨勢（會依 finished_reason 拆成好幾條，想看單一條乾淨
的線就用下面「聚合成一條」那個版本）：
  ./scripts/query-metrics.sh range 'rate(vllm:request_success_total{app="gemma-4-31b-vllm"}[5m])'

同上，但聚合成一條乾淨的線：
  ./scripts/query-metrics.sh range 'sum(rate(vllm:request_success_total{app="gemma-4-31b-vllm"}[5m])) by (app)'

每秒處理的 prompt（prefill）token 數：
  ./scripts/query-metrics.sh range 'rate(vllm:prompt_tokens_total{app="gemma-4-31b-vllm"}[5m])'

每秒生成的 token 數——跟請求數是不同維度，長回覆會拉高這個但不一定拉高請求數：
  ./scripts/query-metrics.sh range 'rate(vllm:generation_tokens_total{app="gemma-4-31b-vllm"}[5m])'

── 延遲（histogram，要用 histogram_quantile + le 標籤聚合）────────────────────

p95 端到端延遲（從收到請求到回完整個 response）：
  ./scripts/query-metrics.sh range 'histogram_quantile(0.95, sum(rate(vllm:e2e_request_latency_seconds_bucket{app="gemma-4-31b-vllm"}[5m])) by (le))'

p95 首字延遲（TTFT，使用者感受到的「反應快不快」）：
  ./scripts/query-metrics.sh range 'histogram_quantile(0.95, sum(rate(vllm:time_to_first_token_seconds_bucket{app="gemma-4-31b-vllm"}[5m])) by (le))'

p95 排隊等待時間——這個如果變高，代表 KEDA 該擴容了或已經來不及：
  ./scripts/query-metrics.sh range 'histogram_quantile(0.95, sum(rate(vllm:request_queue_time_seconds_bucket{app="gemma-4-31b-vllm"}[5m])) by (le))'

── 跨模型比較（拿掉 {app=...} 篩選，改用 by (app) 聚合）───────────────────────

31b/26b/light-models 的流量並列比較：
  ./scripts/query-metrics.sh range 'sum(rate(vllm:request_success_total[5m])) by (app)'

各模型排隊情況並列比較：
  ./scripts/query-metrics.sh query 'sum(vllm:num_requests_waiting) by (app)'

── 磁碟（node-exporter，跟 disk-alerts.yml 的告警規則同一組資料）─────────────

根目錄磁碟使用率百分比，>80 會觸發 NodeDiskUsageHigh 告警：
  ./scripts/query-metrics.sh query '(1 - node_filesystem_avail_bytes{mountpoint="/"} / node_filesystem_size_bytes{mountpoint="/"}) * 100'

── 自訂時間區間（range 預設查最近 1 小時、step=60 秒）────────────────────────

  ./scripts/query-metrics.sh range '<PromQL>' 2026-07-28T00:00:00Z 2026-07-28T13:00:00Z 300

提醒：PromQL 一定要用單引號整條包起來，不然裡面的雙引號會被 shell 吃掉、變成不合
法語法（例如 {app="x"} 變成 {app=x}），Prometheus 會回 400 錯誤。
EOF
}

if [[ -z "$MODE" || "$MODE" == "help" || "$MODE" == "-h" || "$MODE" == "--help" ]]; then
    print_promql_reference
    exit 0
fi

if [[ -z "$QUERY" ]]; then
    echo "用法："
    echo "  $0 query <promql>"
    echo "  $0 range <promql> [start] [end] [step秒]"
    echo "  $0 help    # 印出常用 PromQL 參考"
    exit 1
fi

# URL encode 交給 python3（比 jq @uri 更保證這台機器上一定有）
urlencode() {
    python3 -c 'import urllib.parse, sys; print(urllib.parse.quote(sys.argv[1]))' "$1"
}

ENC_QUERY="$(urlencode "$QUERY")"

case "$MODE" in
    query)
        URL="http://localhost:9090/api/v1/query?query=${ENC_QUERY}"
        ;;
    range)
        END="${4:-$(date -u +%Y-%m-%dT%H:%M:%SZ)}"
        START="${3:-$(date -u -d '-1 hour' +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u -v-1H +%Y-%m-%dT%H:%M:%SZ)}"
        STEP="${5:-60}"
        URL="http://localhost:9090/api/v1/query_range?query=${ENC_QUERY}&start=${START}&end=${END}&step=${STEP}"
        ;;
    *)
        echo "第一個參數要是 query 或 range，不是「$MODE」"
        exit 1
        ;;
esac

if ! RESULT="$(kubectl exec -n "$NS" deploy/prometheus -- wget -qO- "$URL" 2>&1)"; then
    echo "查詢失敗，Prometheus 收到的實際 query 字串是："
    echo "  $QUERY"
    echo "$RESULT"
    echo ""
    echo "最常見原因：忘記把整條 PromQL 用單引號包起來——"
    echo "shell 會吃掉裡面的雙引號（例如 {app=\"x\"} 會變成 {app=x}，PromQL 語法不合法）。"
    echo "上面印出來的 query 字串裡如果 label 的值沒有被雙引號包住（像 {app=x} 而不是"
    echo "{app=\"x\"}），就是這個問題——重打一次時記得把整條 PromQL 包在單引號裡："
    echo "  $0 $MODE '<完整 PromQL，例如 vllm:num_requests_waiting{app=\"gemma-4-31b-vllm\"}>'"
    exit 1
fi

# 用 python3 把結果整理成人類看得懂的表格（不依賴 jq）
echo "$RESULT" | python3 -c '
import json, sys, datetime

data = json.load(sys.stdin)
if data.get("status") != "success":
    print("查詢失敗：", data)
    sys.exit(1)

result_type = data["data"]["resultType"]
results = data["data"]["result"]

if not results:
    print("（沒有資料——確認 query 語法，或這段時間內該序列不存在）")
    sys.exit(0)

def label(m):
    # 優先顯示 app，沒有的話印出完整 label 組合
    if "app" in m:
        extra = ",".join(f"{k}={v}" for k, v in m.items() if k not in ("app", "__name__"))
        return m["app"] + (f" [{extra}]" if extra else "")
    return ",".join(f"{k}={v}" for k, v in m.items()) or "(no labels)"

if result_type == "vector":
    for series in results:
        ts, val = series["value"]
        t = datetime.datetime.fromtimestamp(float(ts), tz=datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        lbl = label(series["metric"])
        print(f"{lbl:50s} value={val:>12s}  ({t})")
elif result_type == "matrix":
    for series in results:
        lbl = label(series["metric"])
        print(f"=== {lbl} ===")
        for ts, val in series["values"]:
            t = datetime.datetime.fromtimestamp(float(ts), tz=datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            print(f"  {t}  {val}")
else:
    print(json.dumps(data, indent=2, ensure_ascii=False))
'
