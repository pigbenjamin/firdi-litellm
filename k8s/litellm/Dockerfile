# 官方 litellm image 是 Wolfi-based,刻意不含 pip(kubectl exec 進正式 pod 查證過,
# python -m pip 直接報 No module named pip)。但 base image 沒把 stdlib 的
# ensurepip 拿掉(bundled wheel還在),可以就地生出 pip 來裝 psycopg2-binary——
# Wolfi 是 glibc 底(不是 musl),manylinux wheel 裝完 import 測試過可以動,不用
# 額外裝 libpq-dev(psycopg2-binary 是靜態連結)。裝完卸掉 pip,image 攻擊面
# 盡量貼近原本 Wolfi 的設計初衷。
FROM ghcr.io/berriai/litellm:main-latest

RUN python -m ensurepip --upgrade \
    && python -m pip install --no-cache-dir psycopg2-binary \
    && python -m pip uninstall -y pip
