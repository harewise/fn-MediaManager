#!/usr/bin/env bash
#
# attach-trim-source.sh —— 把飞牛影视(trim-media)的刮削源/字幕源固定指向自建 tmdb_provider
#
# 背景：飞牛影视 App 每次升级都会重置 /var/apps/trim.media/cmd/service-setup，
#       自定义源参数(--item/--subtitle)被清掉后，trim 会静默回落到默认源(mediasvc.fnnas.com)，
#       表现为：同一部剧新旧条目前缀/数据不一致(如 无职转生 S3E12 独立成卡)。
#       App 升级后重跑一次本脚本即可恢复。
#
# 用法（需要 root）：
#   sudo bash attach-trim-source.sh                # 写配置 + 重启 + 校验
#   sudo bash attach-trim-source.sh --item-only    # 只接刮削源，不动字幕源
#   sudo bash attach-trim-source.sh --no-restart   # 只写配置，不重启
#   sudo bash attach-trim-source.sh --force        # provider 健康检查失败也继续
#
# 环境变量：
#   TRIM_SRC_BASE   自定义源地址，默认 http://127.0.0.1:38080
#
set -euo pipefail

SRC_BASE="${TRIM_SRC_BASE:-http://127.0.0.1:38080}"
SETUP_FILE="${SETUP_FILE:-/var/apps/trim.media/cmd/service-setup}"
MAIN="${MAIN:-/var/apps/trim.media/cmd/main}"
TRIM_PKGVAR="${TRIM_PKGVAR:-/usr/local/apps/@appdata/trim.media}"
TRIM_PKGMETA="${TRIM_PKGMETA:-/vol1/@appmeta/trim.media}"

WITH_SUBTITLE=1
DO_RESTART=1
FORCE=0

log()  { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*"; }
warn() { printf '[warn] %s\n' "$*"; }
die()  { printf '[error] %s\n' "$*" >&2; exit 1; }

usage() { sed -n '2,18p' "$0" | sed 's/^# \{0,1\}//'; exit 0; }

for arg in "$@"; do
  case "$arg" in
    --item-only)  WITH_SUBTITLE=0 ;;
    --no-restart) DO_RESTART=0 ;;
    --force)      FORCE=1 ;;
    -h|--help)    usage ;;
    *) die "未知参数: $arg（-h 查看用法）" ;;
  esac
done

[ "$(id -u)" -eq 0 ] || die "需要 root 权限，请用: sudo bash $0"
command -v curl >/dev/null    || die "缺少 curl"
command -v python3 >/dev/null || die "缺少 python3（用于改写 service-setup）"
[ -f "$SETUP_FILE" ] || die "找不到 $SETUP_FILE（飞牛影视未安装，或路径不对）"

# ---- 0. provider 健康检查：避免把 trim 指到一个死源上 ----
if curl -fsS -m 3 "$SRC_BASE/healthz" | grep -q '"code": 0'; then
  log "provider 健康检查通过：$SRC_BASE/healthz"
elif [ "$FORCE" -eq 1 ]; then
  warn "provider 健康检查失败，--force 继续"
else
  die "provider 未就绪：$SRC_BASE/healthz 无响应。
       请先确认容器在跑（docker ps | grep tmdb-provider），或加 --force 强行继续。"
fi

# ---- 1. 备份 + 改写 service-setup ----
BACKUP="${SETUP_FILE}.bak.$(date +%Y%m%d-%H%M%S)"
cp -a "$SETUP_FILE" "$BACKUP"
log "已备份原文件：$BACKUP"

python3 - "$SETUP_FILE" "$SRC_BASE" "$WITH_SUBTITLE" <<'PY'
import re, sys
path, base, with_sub = sys.argv[1], sys.argv[2], sys.argv[3] == "1"
src = open(path, "rb").read().decode("utf-8", "surrogateescape")
orig = src

# 1) 写入 CUSTOM_SRC_BASE / ITEM_OPT / SUBTITLE_OPT（重复执行安全）
new_block = ["# ===== 自定义刮削数据源（tmdb_provider，attach-trim-source.sh 写入）=====",
             f'CUSTOM_SRC_BASE="{base}"',
             'ITEM_OPT="--item=${CUSTOM_SRC_BASE}"']
if with_sub:
    new_block.append('SUBTITLE_OPT="--subtitle=${CUSTOM_SRC_BASE}"')
new_block = "\n".join(new_block) + "\n"

if "CUSTOM_SRC_BASE=" in src:
    src = re.sub(r'CUSTOM_SRC_BASE="[^"]*"', f'CUSTOM_SRC_BASE="{base}"', src, count=1)
    if with_sub and 'SUBTITLE_OPT="--subtitle=${CUSTOM_SRC_BASE}"' not in src:
        src = src.replace('ITEM_OPT="--item=${CUSTOM_SRC_BASE}"',
                          'ITEM_OPT="--item=${CUSTOM_SRC_BASE}"\n'
                          'SUBTITLE_OPT="--subtitle=${CUSTOM_SRC_BASE}"', 1)
else:
    m = re.search(r'^[# \t]*ITEM_OPT=.*\n(?:[# \t]*SUBTITLE_OPT=.*\n)?', src, re.M)
    if not m:
        sys.exit("service-setup 里找不到 ITEM_OPT 定义行，结构可能与预期不同，请手动编辑")
    src = src[:m.start()] + new_block + src[m.end():]

# 2) 确保 SERVICE_COMMAND[0] 真的带上 ${ITEM_OPT} ${SUBTITLE_OPT}
#    注意：只吃行尾的 " 和空白，不能用 \s（会把后面的空行一起吞掉）
m = re.search(r'^(SERVICE_COMMAND\[0\]=.*)"[ \t]*$', src, re.M)
if not m:
    sys.exit("service-setup 里找不到 SERVICE_COMMAND[0] 行")
line = m.group(0)
body = line.rstrip()
if not body.endswith('"'):
    sys.exit("SERVICE_COMMAND[0] 行格式异常（行尾不是引号），请手动编辑")
body = body[:-1]  # 去掉行尾引号，参数要加在引号里面
want = ["${ITEM_OPT}"] + (["${SUBTITLE_OPT}"] if with_sub else [])
add = [w for w in want if w not in line]
if add:
    src = src.replace(line, body + " " + " ".join(add) + '"', 1)

if src == orig:
    print("service-setup 已是目标配置，未改动")
else:
    open(path, "w", encoding="utf-8", errors="surrogateescape").write(src)
    print("service-setup 已更新")
PY

log "当前关键行："
grep -nE 'CUSTOM_SRC_BASE=|^ITEM_OPT=|^SUBTITLE_OPT=|^SERVICE_COMMAND' "$SETUP_FILE" | sed 's/^/    /'

if [ "$DO_RESTART" -eq 0 ]; then
  log "--no-restart：配置未生效，需要时手动重启 trim-media"
  exit 0
fi

# ---- 2. 重启 trim-media（必须带 TRIM_* 环境变量，应用中心就是这样调用的） ----
export TRIM_APPNAME=trim.media
export TRIM_APPDEST=/usr/local/apps/@appcenter/trim.media
export TRIM_PKGVAR TRIM_PKGMETA
export TRIM_USERNAME=trim-media

log "停止 trim-media ..."
bash "$MAIN" stop || true
sleep 2
log "启动 trim-media ..."
bash "$MAIN" start || true

# ---- 3. 校验 ----
code=""
for _ in $(seq 1 15); do
  sleep 1
  code=$(curl -s -o /dev/null -w '%{http_code}' -m 3 http://127.0.0.1:8005/ || true)
  { [ "$code" = "301" ] || [ "$code" = "200" ]; } && break
done

item_arg=$(ps -ef | grep "[a]ppcenter/trim.media/trim-media" | grep -o -- '--item=[^ ]*' | head -1 || true)
sub_arg=$(ps -ef  | grep "[a]ppcenter/trim.media/trim-media" | grep -o -- '--subtitle=[^ ]*' | head -1 || true)

echo
if { [ "$code" = "301" ] || [ "$code" = "200" ]; } && [ -n "$item_arg" ]; then
  log "✔ 完成：trim-media 已带自定义源启动"
  echo "    backend : HTTP $code  (127.0.0.1:8005)"
  echo "    item    : ${item_arg}${sub_arg:+  $sub_arg}"
  echo "    提示    : 下次刮削/刷新会走 provider，可在 刮削文件/logs/requests_tmdb.log 看到请求"
  echo "    提示    : 飞牛影视 App 再次升级会重置 service-setup，届时重跑本脚本即可"
else
  warn "重启后校验未通过（backend HTTP ${code:-无响应}, item=${item_arg:-未找到}）"
  warn "自动回滚到备份配置并重新拉起 ..."
  cp -a "$BACKUP" "$SETUP_FILE"
  bash "$MAIN" stop || true
  sleep 2
  bash "$MAIN" start || true
  die "已回滚。请手动检查：bash $MAIN status / docker logs -f tmdb-provider"
fi
