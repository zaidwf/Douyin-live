#!/bin/bash
# 在服务器上以**隧道**方式跑 WS 生命周期探针：读 root:root 600 的 proxy.env 再转交。
# 单独落成脚本是为了避开 `sudo -n bash -c "...&"` 那种引号/cwd 全是坑的写法。
cd /opt/douyin-live || exit 1
set -a
. config/proxy.env
set +a
exec ./venv/bin/python -u test/probe_ws_lifetime.py "$@"
