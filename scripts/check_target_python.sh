#!/usr/bin/env bash
# check_target_python.sh — 用「容器實際會跑的 Python 版本」檢查語法
#
# 為什麼需要這支：開發機是 Python 3.12，admin-api 容器是 3.11（見
# admin-api/Dockerfile 的 FROM python:3.11-slim）。兩者的語法規則不完全一樣，
# 最容易中招的是「f-string 的 {} 表達式裡不能有反斜線」——3.12 放寬了，3.11 沒有。
# 本機 python3 -c "import ast; ast.parse(...)" 全過、離線測試全過，推上去卻是
# CrashLoopBackOff + SyntaxError，日誌還要進 pod 才看得到。實際發生過一次。
#
# config/ 底下的兩個 hook 跑在 litellm 容器裡（另一個 image），版本可能又不同，
# 但語法檢查用同一個 3.11 已經足夠擋住這類問題。
#
# 用法：
#   ./scripts/check_target_python.sh          # 需要 docker
#   TARGET_PY_IMAGE=python:3.11-slim ./scripts/check_target_python.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE="${TARGET_PY_IMAGE:-python:3.11-slim}"

if ! command -v docker >/dev/null 2>&1; then
    echo "略過語法檢查：本機沒有 docker（部署到叢集前請在有 docker 的機器上跑一次）" >&2
    exit 0
fi

docker run --rm -v "$REPO_ROOT:/src:ro" "$IMAGE" python -c "
import pathlib, sys
files = [f for d in ('admin-api', 'config', 'scripts')
         for f in pathlib.Path('/src', d).rglob('*.py')
         if '__pycache__' not in str(f)]
bad = []
for f in sorted(files):
    try:
        compile(f.read_text(encoding='utf-8'), str(f), 'exec')
    except SyntaxError as e:
        bad.append(f'{f}:{e.lineno}  {e.msg}')
print(f'Python {sys.version.split()[0]}：檢查 {len(files)} 個檔案', end='')
if bad:
    print(' — 有語法錯誤')
    for b in bad:
        print('  ✗', b)
    sys.exit(1)
print(' — 全部通過')
"
