#!/usr/bin/env bash
# 按 MANIFEST 打出服务器部署包：service/dist/douyin-live-<日期>.tar.gz
#
# 用法（在仓库根或任意处都行）：
#   bash service/pack.sh
#
# 设计意图（见 README.md）：
#   * 清单是唯一真相，脚本**不复制源码** —— 仓库里 liveMan.py 只有一份，不会漂移
#   * 少一个文件就**报错退出**，不静默漏传（漏 a_bogus 脚本的现象是
#     「连上了但一条数据都没有」，极难排查）
#   * 打包前扫一遍**带凭据的 URL**，防止代理账密被顺手写进某个配置文件带出去
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SVC="$ROOT/service"
MANIFEST="$SVC/MANIFEST"
OUT_DIR="$SVC/dist"
TARBALL="$OUT_DIR/douyin-live-$(date +%Y%m%d).tar.gz"

[ -f "$MANIFEST" ] || { echo "❌ 找不到清单: $MANIFEST" >&2; exit 1; }

# ---------- 1. 读清单（去行内注释、去首尾空白、去空行）----------
FILES=()
while IFS= read -r line || [ -n "$line" ]; do
    line="${line%%#*}"
    line="$(printf '%s' "$line" | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')"
    [ -n "$line" ] && FILES+=("$line")
done < "$MANIFEST"

if [ "${#FILES[@]}" -eq 0 ]; then
    echo "❌ 清单里一个文件都没有：$MANIFEST" >&2
    exit 1
fi

# ---------- 2. 存在性核对：缺一个就不打 ----------
missing=()
for f in "${FILES[@]}"; do
    [ -e "$ROOT/$f" ] || missing+=("$f")
done
if [ "${#missing[@]}" -gt 0 ]; then
    echo "❌ 清单里这些路径不存在，拒绝打包（宁可报错也不静默漏传）:" >&2
    printf '   %s\n' "${missing[@]}" >&2
    echo "   → 若该文件确实不再需要，从 service/MANIFEST 里删掉再重跑" >&2
    exit 1
fi

# ---------- 3. 凭据哨兵 ----------
# 带凭据的代理 URL 只该待在**服务器上**的 config/proxy.env（已 gitignore），
# 不该进发布包。
#   * 只扫可能承载配置的文本类文件，避开 sign.js 这类压缩 JS 的误报
#   * 账密字符类里**排掉 `<` `>`**：文档里的占位写法（`http://<用户>:<密码>@<主机>`）
#     是格式说明不是凭据，不该误报 —— 否则 pack.sh 会被自己的文档卡住
#   * 文件名含 `example` 的一律跳过（那正是给人照抄的模板，形状和真的一样）
rm -f "$TARBALL"
echo "==> 凭据哨兵：扫描配置类文件里是否混入带账密的 URL"
leaked="$(grep -rlIE '[a-zA-Z][a-zA-Z0-9+.-]*://[^/@[:space:]<>]+:[^/@[:space:]<>]+@[^/[:space:]]+' \
    --include='*.env' --include='*.example' --include='*.service' --include='*.sh' \
    --include='*.json' --include='*.py' --include='*.md' --include='*.txt' \
    "${FILES[@]/#/$ROOT/}" 2>/dev/null | grep -v 'example' || true)"
if [ -n "$leaked" ]; then
    echo "❌ 下面这些文件里含**带凭据的 URL**，拒绝打包：" >&2
    printf '   %s\n' "$leaked" >&2
    echo "   → 凭据请放到服务器上的 config/proxy.env（600 权限），仓库里只留无凭据示例" >&2
    exit 1
fi

# ---------- 4. 打包 ----------
mkdir -p "$OUT_DIR"
echo "==> 打包 ${#FILES[@]} 个文件 → $(basename "$TARBALL")"
( cd "$ROOT" && tar czf "$TARBALL" "${FILES[@]}" )

echo
echo "==> 包内清单："
tar tzf "$TARBALL" | sed 's/^/    /'
echo
echo "大小: $(du -h "$TARBALL" | cut -f1)"
cat <<'EOF'

⚠️ 这个包里**没有**代理凭据（故意的）。到服务器上后必须手建：
     mkdir -p /opt/douyin-live/config
     printf 'DOUYIN_PROXY=http://<用户>:<密码>@<主机>:<端口>\n' > /opt/douyin-live/config/proxy.env
     chmod 600 /opt/douyin-live/config/proxy.env
   不建也能跑，但那时是**数据中心 IP 直连** —— 按 DEPLOY.md 第七节的实测，
   腾讯云那种 IP 15 主播 @10s 就有 88 次 444，风控会拦掉大半。详见 service/README.md。
EOF
