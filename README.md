# trim-media TMDB 刮削源（tmdb_provider）

为飞牛 fnOS 的 **trim-media（飞牛影视）** 提供自建刮削数据源：TMDB 官方 API + 剧集组精确匹配，
解决默认源（mediasvc.fnnas.com）对动漫"整季拆分 / 全局集号"匹配错乱、封面简介缺失的问题。
单文件 Python、零第三方依赖（仅相似图去重用到 `ffmpeg`）。

## 快速开始（Docker Compose，推荐）

**0. 前置**：[themoviedb.org](https://www.themoviedb.org/settings/api) 免费申请 API Key。

**1. 获取镜像**：

```bash
docker pull ghcr.io/harewise/fn-mediamanager:latest
```

> 镜像在发布 Release（tag 形如 v1.0.0）时自动构建，产出 tag：1.0.0 / 1.0 / latest。
> 首次构建后，到仓库 Packages 页把镜像可见性改为 Public，NAS 才能免登录拉取；
> 保持 Private 则 NAS 上需先 `docker login ghcr.io`（PAT 即可）。

**2. 部署**（NAS 上运行只需要 docker-compose.yml + .env 两个文件，镜像从 ghcr 拉取）：

```bash
git clone https://github.com/harewise/fn-MediaManager.git && cd fn-MediaManager
echo 'TMDB_API_KEY=你的key' > .env          # 私有信息只放 .env（已被 .gitignore 排除）
docker compose up -d

curl -s http://127.0.0.1:38080/healthz      # {"code":0,...} 即正常
```

`volumes` 里的 `./cache`、`./logs` 会自动创建；`/vol1/@appmeta/trim.media/img` 是飞牛图片缓存回退路径，
非 fnOS 环境删掉该行即可。

## 配置

优先级：**环境变量 > 配置文件 > 内置默认**。Docker 用环境变量即可，`tmdb_config.json` 不需要（也不要提交到仓库）。

| 环境变量 | 配置文件键 | 默认 | 说明 |
|---|---|---|---|
| `TMDB_API_KEY` | `api_key` | 无（必填） | TMDB API Key |
| `TMDB_BIND` | `bind` | `127.0.0.1` | 监听地址；容器内用 `0.0.0.0` |
| `TMDB_PORT` | `port` | `38080` | 监听端口 |
| `TMDB_LOG_FILE` | `log_file` | 无（只打 stdout） | 请求日志文件路径 |
| — | `tmdb_base` / `img_base` | TMDB 官方 | API/图片源地址，仅配置文件可改，一般不用动 |
| — | `img_original_size` | `false` | `true` 时图片代理不降尺寸，始终取原图 |

## trim-media 接线（service-setup 写死版）

编辑 `/var/apps/trim.media/cmd/service-setup`，把选源逻辑删掉，**无条件**写死：

```bash
# ===== 自定义刮削数据源（tmdb_provider，:38080）=====
# 写死接入自定义代理，不做健康检测；provider 自身常驻保活。
CUSTOM_SRC_BASE="http://127.0.0.1:38080"
ITEM_OPT="--item=${CUSTOM_SRC_BASE}"
SUBTITLE_OPT="--subtitle=${CUSTOM_SRC_BASE}"
```

- 生效时机：trim-media 下次启动/重启（不需要立刻重启，当前参数不变就不用动）。
- 代价：写死后不再自动回退飞牛默认源——**provider 不在，刮削就全部失败**，
  所以请确保 provider 常驻（Docker `restart: unless-stopped` 或 systemd）。
- 飞牛应用升级可能覆盖此文件：重新拷回即可（本机留有备份；仓库不携带，内容就上面 4 行核心）。

重启 trim-media（必须带 TRIM_* 环境变量，应用中心就是这样调用的）：

```bash
E="TRIM_APPNAME=trim.media TRIM_APPDEST=/usr/local/apps/@appcenter/trim.media TRIM_PKGVAR=/usr/local/apps/@appdata/trim.media TRIM_PKGMETA=/vol1/@appmeta/trim.media TRIM_USERNAME=trim-media"
sudo env $E /var/apps/trim.media/cmd/main stop && sleep 2
sudo env $E /var/apps/trim.media/cmd/main start

# 确认数据源（有输出=自定义源）
ps -ef | grep "[a]ppcenter/trim.media/trim-media" | grep -o "\-\-item[^ ]*"
```

## 手动运行（不用 Docker）

```bash
cd 项目目录
cp tmdb_config.example.json tmdb_config.json   # 填入 api_key（此文件已被 .gitignore 排除）

# kill 与启动分两条命令执行！（同一条命令里 pkill 会匹配到自身命令文本而自杀）
ss -tlnp | grep :38080                          # 找到旧 pid 先 kill
setsid nohup python3 tmdb_provider.py >> logs/tmdb.out 2>&1 < /dev/null &
sleep 2; curl -s http://127.0.0.1:38080/healthz
```

## 调试

```bash
tail -f logs/requests_tmdb.log                # 请求日志（占位强刷会打 [group] 行）
docker logs -f tmdb-provider                  # 容器模式看控制台日志

# 手动验证单集匹配（返回 hasDiff:true + 集数据）
curl -s -X POST http://127.0.0.1:38080/meta/diff \
  -d '{"dataVersion":"","category":"episode","language":"zh-CN","trimId":"tm65942","seasonNumber":4,"episodeNumber":81}'
```

**"刷新元数据没反应"排查顺序**：请求日志里有没有对应请求 → 没有则是 trim 侧没发出来；
有且返回 `hasDiff:false` → provider 判定 trim 已是最新（若集数据是占位的，日志会有 `[group]` 强刷行，
重启容器/进程可清缓存强刷一次）。

## 已知行为与限制

- TMDB 没播出的集本身就是占位数据（标题"第 N 集"、无截图），播出后缓存会在 TTL 内自动纠正。
- 人物详情（`/detail/person`）未实现，返回空数据。
- `GET /match` 返回 404 与飞牛原服务行为一致，**不是故障**。
- 相似图聚类依赖 ffmpeg，缺失时仅去重功能退化，其余不受影响。
