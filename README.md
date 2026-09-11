# trim-media TMDB 刮削源（tmdb_provider）

为飞牛 fnOS 的 **trim-media（飞牛影视）** 提供自建刮削数据源：TMDB 官方 API + 剧集组精确匹配，
解决默认源（mediasvc.fnnas.com）对动漫"整季拆分 / 全局集号"匹配错乱、封面简介缺失的问题。
单文件 Python、零第三方依赖。支持剧集与电影。

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

## 接入 trim-media（service-setup 写死版）

编辑 `/var/apps/trim.media/cmd/service-setup`，把选源逻辑删掉，**无条件**写死：

```bash
# ===== 自定义刮削数据源（tmdb_provider，:38080）=====
# 写死接入自定义代理，不做健康检测；provider 自身常驻保活。
CUSTOM_SRC_BASE="http://127.0.0.1:38080"
ITEM_OPT="--item=${CUSTOM_SRC_BASE}"
SUBTITLE_OPT="--subtitle=${CUSTOM_SRC_BASE}"
```

- 生效时机：trim-media 下次启动/重启（当前参数不变就不用立刻动）。
- 代价：写死后不再自动回退飞牛默认源——**provider 不在，刮削就全部失败**，
  所以请确保 provider 常驻（Docker `restart: unless-stopped`）。
- 飞牛应用升级可能覆盖此文件：重新拷回即可（内容就上面 4 行核心）。

重启 trim-media（必须带 TRIM_* 环境变量，应用中心就是这样调用的）：

```bash
E="TRIM_APPNAME=trim.media TRIM_APPDEST=/usr/local/apps/@appcenter/trim.media TRIM_PKGVAR=/usr/local/apps/@appdata/trim.media TRIM_PKGMETA=/vol1/@appmeta/trim.media TRIM_USERNAME=trim-media"
sudo env $E /var/apps/trim.media/cmd/main stop && sleep 2
sudo env $E /var/apps/trim.media/cmd/main start

# 确认数据源（有输出=自定义源）
ps -ef | grep "[a]ppcenter/trim.media/trim-media" | grep -o "\-\-item[^ ]*"
```

## 日常运维

```bash
tail -f logs/requests_tmdb.log                # 请求日志（占位强刷会打 [group] 行）
docker logs -f tmdb-provider                  # 容器控制台日志

# 验证单集匹配（返回 hasDiff:true + 集数据）
curl -s -X POST http://127.0.0.1:38080/meta/diff \
  -d '{"dataVersion":"","category":"episode","language":"zh-CN","trimId":"tt65942","seasonNumber":4,"episodeNumber":81}'
```

**"刷新元数据没反应"排查顺序**：请求日志里有没有对应请求 → 没有则是 trim 侧没发出来；
有且返回 `hasDiff:false` → provider 判定 trim 已是最新（若集数据是占位的，日志会有 `[group]` 强刷行，
重启容器可清缓存强刷一次）。

## 开发与调试

改代码后的三层验证（从快到慢），均不影响生产实例（38080）：

| 命令 | 用途 |
|---|---|
| `bash dev.sh test` | 离线单测（秒级）：fixture 回放 TMDB 响应，不耗 API 配额 |
| `bash dev.sh host` | 宿主机直跑 38081，前台可断点（VS Code F5 已配好） |
| `bash dev.sh up` / `restart` / `down` | 调试容器 38081：源码 bind mount，改代码 restart 即生效免 rebuild |

- 测试数据来自 `tests/fixtures/`（真实 TMDB 响应录制）；数据过期或新增场景时 `bash dev.sh fixtures` 联网重录。
- 覆盖端口须用 `TMDB_PORT` 环境变量：优先级高于配置文件里的 `port`，`--port` 参数最低。
- 不用 Docker 部署时直接 `python3 tmdb_provider.py`（按配置文件键准备 `tmdb_config.json`，
  模板见 `tmdb_config.example.json`）。**杀旧进程与启动必须分两条命令**——同一条里 pkill 会
  匹配到自身命令文本而自杀。

## 项目结构

| 路径 | 内容 |
|---|---|
| `tmdb_provider.py` | 全部源码（单文件） |
| `Dockerfile` / `docker-compose.yml` | 生产部署 |
| `docker-compose.debug.yml` / `dev.sh` | 调试环境（38081，与生产互不影响） |
| `tests/` | 离线单测 + TMDB fixture（录制/回放，见 `tests/record_fixtures.py`） |
| `docs/` | 飞牛影视接口协议 |
| `tmdb_config.example.json` | 配置模板（手动部署用；Docker 只需 `.env`） |

## 已知行为与限制

- TMDB 没播出的集本身就是占位数据（标题"第 N 集"、无截图），播出后缓存会在 TTL 内自动纠正。
- 电影：`/search/byThirdPartyHash` 按文件名匹配 TMDB 电影（hash 不参与匹配，TMDB 无此体系）、
  `/detail/movie` 详情、`/meta/diff` 电影差量均已支持；手动搜索候选含电影。
- 人物详情（`/detail/person`）未实现，返回空数据。
- `GET /match` 返回 404 与飞牛原服务行为一致，**不是故障**。
- 镜像不含 ffmpeg（省约 450MB）：`/meta/images` 候选海报的相似去重随之禁用（近重复图不再
  合并，其余不受影响），启动日志会提示；宿主机装有 ffmpeg 时直跑自动启用。
