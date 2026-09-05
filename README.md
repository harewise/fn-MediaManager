# trim-media TMDB 刮削源（tmdb_provider）

为飞牛 fnOS 的 **trim-media（飞牛影视）** 提供自建刮削数据源：TMDB 官方 API + **剧集组精确匹配**，
解决默认源（mediasvc.fnnas.com）对动漫"整季拆分 / 全局集号"匹配错乱、封面简介缺失的问题。

单文件 Python、零第三方依赖（仅相似图去重用到 `ffmpeg`）；
Docker 镜像由 GitHub Actions 自动构建并发布到 ghcr.io。

> 命名说明：项目早期用 bgm.tv 做数据源，后整体切换为 TMDB 官方接口（协议与飞牛原源一致，前端无感），
> bgm 相关代码已于 2026-09-05 全部移除。

## 架构

```
[飞牛 trim-media :8005] --item/subtitle--> [tmdb_provider :38080] --> api.tmdb.org/3（zh-CN）
                                       /t/p/* 图片 --> image.tmdb.org（按类型降尺寸代理）
                                       /v1/*  字幕  --> subtitle-service.fnnas.com（原样转发）
```

## 功能特性

- **剧集组匹配**：TMDB 常把动漫全部集放在 S1 里用全局号（如 Re:Zero S1=85 集），而文件按 S1-S4 分季命名。
  provider 取 TMDB Episode Groups（优先 `Seasons` 组），组内全局号直接命中；季内号自动换算（季起始号+ep-1）。
- **剧集组缓存自愈**：缓存带 12h TTL；命中"已播出但仍无剧照无简介"的占位集（TMDB 数据晚于播出完善）时，
  限频强制重取并重试——解决"刚播出的剧集刷新元数据没反应"。
- **图片搜索 `/meta/images`**：posters / backdrops / logos 三类；候选图按感知哈希（12x12 RGB + 24x24 梯度，ffmpeg 提取）
  聚类，同一张图的多分辨率/多语言版本只留最大一张（对齐 TMDB 网页的相似图分组）。
- **图片代理**：飞牛本地图片缓存回退 → TMDB（keep-alive 连接池 + 按类型降尺寸 + 磁盘缓存 +
  每日保底清理：超 500MB 按文件新旧删到 300MB）。
- **字幕转发**：`/v1/*` 原样转发 subtitle-service.fnnas.com。
- 单集无截图时返回空 `still_path`，由飞牛从视频自动截帧（与原源行为一致）。

## 快速开始（Docker Compose，推荐）

> 镜像地址：`ghcr.io/<你的GitHub用户名>/<仓库名>:latest`，push 到 main 分支后 Actions 自动构建。

**0. 前置**：[themoviedb.org](https://www.themoviedb.org/settings/api) 免费申请 API Key。

**1. 获取镜像**（两种任选）：

```bash
# GitHub Actions 自动构建：本仓库 push 到 main 即产出 latest；打 v* tag 产出版本号 tag。
# 注意：首次构建后，到仓库 Packages 页把镜像可见性改为 Public，NAS 才能免登录拉取；
# 保持 Private 则 NAS 上需先 docker login ghcr.io（PAT 即可）。
docker pull ghcr.io/<用户名>/<仓库名>:latest
```

**2. 部署**：

```bash
git clone https://github.com/<用户名>/<仓库名>.git && cd <仓库名>
echo 'TMDB_API_KEY=你的key' > .env          # 私有信息只放 .env（已被 .gitignore 排除）
# 编辑 docker-compose.yml，把 image 换成你的 ghcr 地址
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

## 接口

| 接口 | 说明 |
|---|---|
| `POST /search/item` | 按文件路径搜索剧集/集数，返回 `cleanData` + `episode` |
| `POST /search/multi` | 关键字搜索 |
| `POST /detail/tv` | 剧集详情（含按剧集组修正的季/集数） |
| `POST /detail/tv/season` | 季详情（集列表） |
| `POST /detail/tv/season/episode` | 单集详情 |
| `POST /meta/diff` | 元数据差量比对（dataVersion 不同才返回新数据） |
| `POST /meta/images` | 图片搜索（海报/背景/logo，相似图归组） |
| `POST /genres` | 类型列表 |
| `GET /t/p/*` | 图片代理（本地缓存回退 → TMDB 降尺寸） |
| `GET /match` | 返回 404（与飞牛原服务行为一致，**不是故障**） |
| `/v1/*` | 字幕服务转发 |

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
- 相似图聚类依赖 ffmpeg，缺失时仅去重功能退化，其余不受影响。

## 变更记录

- **2026-09-06** 剧集组缓存加 12h TTL + 占位数据限频强刷（修复"刷新元数据无效果"）；
  海报/剧照降为 w500（实测与飞牛原源交付体积一致）、未知类型图片兜底 w500 不再回落原图；
  图片磁盘缓存每日保底清理（>500MB 清到 300MB）；配置支持环境变量注入；GitHub Actions 自动构建镜像。
- **2026-09-05** 端口改 38080；图片管线重做（keep-alive 连接池、按类型降尺寸、并行预取、相似图聚类）；
  bgm 图源代码移除；service-setup 健康检查版安装。
- **2026-08-28~29** 项目创建（bgm.tv 方案），后切换 TMDB 官方 API + 剧集组匹配。
