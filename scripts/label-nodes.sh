#!/usr/bin/env bash
# 幫 GPU 節點貼上 gpu-pool=shared label，讓 nodeSelector: {gpu-pool: shared} 的
# Deployment（目前是 gemma-4-31b、gemma-4-26b）能被排程上去。這是一次性的 cluster
# 層級操作（Node 物件本身不分 namespace），不需要每次 deploy 都重跑；新增 GPU 節點時
# 才需要對那台新節點再跑一次。
#
# 用法：
#   ./scripts/label-nodes.sh                # 不帶參數：標記本機 hostname（單節點 k3s 開發機）
#   ./scripts/label-nodes.sh gpu01 gpu02    # 標記指定的一或多個節點

set -euo pipefail

hosts=("$@")
if [[ ${#hosts[@]} -eq 0 ]]; then
    hosts=("$(hostname)")
fi

for h in "${hosts[@]}"; do
    echo "[INFO] kubectl label node $h gpu-pool=shared --overwrite"
    kubectl label node "$h" gpu-pool=shared --overwrite
done
