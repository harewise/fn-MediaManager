#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TMDB 数据源服务（剧集组匹配版）
================================================================
监听 127.0.0.1:38080，对外提供与 mediasvc.fnnas.com 相同的接口格式，
数据来源：api.tmdb.org（TMDB v3，中文 zh-CN，API key 在 tmdb_config.json）。

匹配规则：
  1. 【主表优先】文件解析出的 (season, episode) 先按 TMDB 主表
     /tv/{id}/season/{s}/episode/{e} 查询——最权威、新季最全
     （如无职转生 Season 3 只在主表，剧集组长期未收录）。
  2. 【剧集组兜底】主表无此集（整季拆分/全局集号的旧结构，如
     Re:Zero S1=85 集、文件 S04E78 用全局号）时，在主剧集组内按
     全局 episode_number / 季内号换算匹配，season_number 按组名；
     组内没有该季时不做跨季全局号兜底（避免 S3E11→S1E11 错配）。
  季/集结构（季数、季集列表）与单集匹配同源：剧集组比主表认识更多编号季时
  （主表把多季塞在一个编号季里用全局号，如 Re:Zero S1=85 集、我独自升级
  两季合一）按剧集组出结构，保证季行与文件命名一致；其余一律主表优先
  （最权威最新，包括真单季番如 Silent Witch，不再让剧集组的脏数据干扰）。
  任一侧缺的季由另一侧补齐。

用法：
  python3 tmdb_provider.py --config tmdb_config.json
  配置 { "api_key": "...", "port": 38080, "bind": "127.0.0.1",
         "log_file": "logs/requests_tmdb.log", "tmdb_base": "https://api.tmdb.org/3",
         "img_base": "https://image.tmdb.org/t/p" }
  缓存统一存放在 cache/tmdb_cache.json（剧集组/季封面/图片类型/聚类特征）。
"""
import argparse
import hashlib
import http.client
import json
import math
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UA = "trim-media-tmdb-provider/1.0"


# --------------------------------------------------------------------------
# HTTPS keep-alive 连接池（image.tmdb.org 每张图省 ~1s TLS 建连）
# --------------------------------------------------------------------------
class ConnPool:
    """keep-alive 连接池。NAS 上 Python TLS 握手 2~4s，复用后单图 0.3~1s。
    空闲超过 IDLE_TTL 的连接直接丢弃（TMDB 服务端会主动断开，拿到死连接
    要白付一次失败重试）。"""
    IDLE_TTL = 30.0

    def __init__(self, host: str, maxsize: int = 8):
        self.host = host
        self.maxsize = maxsize
        self._conns = []   # [(conn, last_used_ts)]
        self._lock = threading.Lock()

    def get(self) -> http.client.HTTPSConnection:
        with self._lock:
            while self._conns:
                conn, ts = self._conns.pop()
                if time.time() - ts <= self.IDLE_TTL:
                    return conn
                try:
                    conn.close()
                except OSError:
                    pass
        return http.client.HTTPSConnection(self.host, timeout=30)

    def put(self, conn: http.client.HTTPSConnection):
        with self._lock:
            if len(self._conns) < self.maxsize:
                self._conns.append((conn, time.time()))
                return
        try:
            conn.close()
        except OSError:
            pass

    def discard(self, conn: http.client.HTTPSConnection):
        try:
            conn.close()
        except OSError:
            pass


_pools = {}
_pools_lock = threading.Lock()


def _pool_for(host: str) -> ConnPool:
    with _pools_lock:
        pool = _pools.get(host)
        if pool is None:
            pool = _pools[host] = ConnPool(host)
        return pool


def http_fetch(url: str, timeout: int = 30, headers: dict = None):
    """keep-alive GET，返回 (status, content_type, body)；传输失败重试一次。"""
    u = urllib.parse.urlsplit(url)
    path = u.path + (("?" + u.query) if u.query else "")
    hdrs = {"User-Agent": UA, "Accept-Encoding": "identity"}
    if headers:
        hdrs.update(headers)
    last_exc = None
    for attempt in range(2):
        pool = _pool_for(u.hostname)
        conn = pool.get()
        try:
            conn.request("GET", path, headers=hdrs)
            resp = conn.getresponse()
            body = resp.read()
            status, ct = resp.status, resp.getheader("Content-Type") or ""
            if resp.will_close:
                pool.discard(conn)
            else:
                pool.put(conn)
            return status, ct, body
        except Exception as e:
            last_exc = e
            pool.discard(conn)
    raise last_exc

TMDB_BASE = "https://api.tmdb.org/3"
IMG_BASE = "https://image.tmdb.org/t/p"
SUBTITLE_UPSTREAM = "https://subtitle-service.fnnas.com/v1"

STATE = {
    "config": {},
    "log_file": None,
    "counter": 0,
    "tv_cache": {},      # tv_id -> tv detail dict
    "movie_cache": {},   # movie_id -> movie detail dict（与 tv_cache 分开：TMDB 电影/剧集 id 空间重叠）
    "group_cache": {},   # tv_id -> 扁平化剧集组 episodes
    "group_ts": {},      # tv_id -> 剧集组拉取时间戳（TTL 判定，持久化）
    "group_refetch_at": {},  # tv_id -> 上次占位强刷时间（限频用，不持久化）
    "ep_cache": {},      # (tv_id, season, ep) -> episode dict
    "poster_cache": {},  # (tv_id, season) -> 季封面 relative path
    "local_img_cache": {},  # 本地 img 缓存文件名 -> 路径(或空串表示不存在)
    "img_types": {},     # 图片文件名 -> 类型(post/backdrop/logo/still)，用于代理时选尺寸
    "img_types_dirty": False,
    "img_feat": {},      # 图片文件名 -> 12x12 RGB 原始字节（相似图分组特征，持久化）
}

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_FILE = os.path.join(SCRIPT_DIR, "cache", "tmdb_cache.json")  # 剧集组/季封面/图片类型/聚类特征 合一缓存

GROUP_TTL = 12 * 3600        # 剧集组缓存有效期（秒）；过期后首个请求重取，追上 TMDB 新播出/修订的集数据
GROUP_REFETCH_MIN = 600      # 命中占位数据时限频强刷的最小间隔（秒），TMDB 确实无数据时避免反复请求

IMG_CACHE_DIR = os.path.join(SCRIPT_DIR, "cache", "img")
IMG_CACHE_MAX = 500 * 1024 * 1024   # 图片磁盘缓存保底清理阈值；超过则按文件从旧到新删
IMG_CACHE_KEEP = 300 * 1024 * 1024  # 清理到该大小为止
IMG_CACHE_CHECK_EVERY = 24 * 3600    # 清理检查间隔（秒）

_cache_lock = threading.Lock()

# 刮削会话上下文：飞牛请求体里 season 是 Go int + omitempty，第 0 季（特别篇）
# 会被整个省略（实测 /detail/tv/season 只剩 {"sourceId","language","source",
# "isRescrap"}）。缺 season 字段 ≠ 第 1 季，按该剧最近一次 /search/item 命中的
# 季兜底；TTL 防止很久以前的搜索残留污染后续无季请求。
_scrape_season_ctx = {}    # tv_id -> (season, ts)
_SEASON_CTX_TTL = 600      # 秒


def _season_ctx_record(tv_id: int, season: int):
    with _cache_lock:
        _scrape_season_ctx[tv_id] = (int(season), time.time())


def _season_ctx_lookup(tv_id: int):
    with _cache_lock:
        hit = _scrape_season_ctx.get(tv_id)
    if hit and time.time() - hit[1] <= _SEASON_CTX_TTL:
        return int(hit[0])
    return None


def _season_from_body_or_ctx(tv_id: int, body: dict) -> int:
    """季号解析：显式字段（含 0）为准；缺席时回退刮削上下文，仍无则 1。
    字段名两套：/detail/tv/season* 用 season，meta diff 用 seasonNumber。"""
    s_raw = body.get("season")
    if s_raw is None:
        s_raw = body.get("seasonNumber")
    if s_raw is not None:
        return int(s_raw)
    ctx = _season_ctx_lookup(tv_id)
    return ctx if ctx is not None else 1


def log_line(s: str):
    print(s, flush=True)
    if STATE["log_file"]:
        try:
            with open(STATE["log_file"], "a", encoding="utf-8") as f:
                f.write(s + "\n")
        except OSError:
            pass


def ok(data) -> dict:
    return {"code": 0, "msg": "", "data": data}


def fail(code, msg) -> dict:
    return {"code": code, "msg": msg, "data": None}


def data_version(obj: dict) -> str:
    o = {k: v for k, v in obj.items() if k != "data_version"}
    blob = json.dumps(o, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _save_caches():
    """剧集组 / 季封面 / 图片类型 / 聚类特征 统一持久化到 cache/tmdb_cache.json（原子写）。"""
    try:
        os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
        data = {
            "groups": {str(k): v for k, v in STATE["group_cache"].items()},
            "group_ts": {str(k): v for k, v in STATE["group_ts"].items()},
            "posters": {f"{k[0]}:{k[1]}": v for k, v in STATE["poster_cache"].items()},
            "img_types": STATE["img_types"],
            "img_feat": dict(STATE["img_feat"]),
        }
        tmp = CACHE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, CACHE_FILE)
        STATE["img_types_dirty"] = False
    except OSError:
        pass


def _load_caches():
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return
    for k, v in (raw.get("groups") or {}).items():
        try:
            STATE["group_cache"][int(k)] = v
        except ValueError:
            pass
    for k, v in (raw.get("group_ts") or {}).items():
        try:
            STATE["group_ts"][int(k)] = float(v)
        except (ValueError, TypeError):
            pass
    for k, v in (raw.get("posters") or {}).items():
        parts = k.split(":", 1)
        if len(parts) == 2:
            try:
                STATE["poster_cache"][(int(parts[0]), int(parts[1]))] = v
            except ValueError:
                pass
    STATE["img_types"].update(raw.get("img_types") or {})
    for k, v in (raw.get("img_feat") or {}).items():
        if "|" not in v:      # 旧格式（纯 rgb hex）弃用
            continue
        STATE["img_feat"][k] = v


# --------------------------------------------------------------------------
# TMDB API client
# --------------------------------------------------------------------------
def tmdb_get(path: str, **params) -> dict:
    base = STATE["config"].get("tmdb_base") or TMDB_BASE
    params.setdefault("api_key", STATE["config"].get("api_key") or "")
    params.setdefault("language", "zh-CN")
    url = base.rstrip("/") + path + "?" + urllib.parse.urlencode(params)
    try:
        status, _ct, raw = http_fetch(url, timeout=30)
        if status != 200:
            return {"_status": status, "_error": raw[:200].decode("utf-8", "replace")}
        return json.loads(raw)
    except Exception as e:
        return {"_status": -1, "_error": str(e)}


def tmdb_ok(d: dict) -> bool:
    return bool(d) and isinstance(d, dict) and "_status" not in d


def search_tv(query: str):
    d = tmdb_get("/search/tv", query=query, page=1)
    if not tmdb_ok(d):
        return []
    return d.get("results") or []


def search_movie(query: str):
    d = tmdb_get("/search/movie", query=query, page=1)
    if not tmdb_ok(d):
        return []
    return d.get("results") or []


def tv_detail(tv_id: int) -> dict:
    key = tv_id
    with _cache_lock:
        if key in STATE["tv_cache"]:
            return STATE["tv_cache"][key]
    d = tmdb_get(f"/tv/{tv_id}")
    if tmdb_ok(d):
        with _cache_lock:
            STATE["tv_cache"][key] = d
        return d
    return {}


def movie_detail(mid: int) -> dict:
    key = mid
    with _cache_lock:
        if key in STATE["movie_cache"]:
            return STATE["movie_cache"][key]
    d = tmdb_get(f"/movie/{mid}")
    if tmdb_ok(d):
        with _cache_lock:
            STATE["movie_cache"][key] = d
        return d
    return {}


def season_episodes_default(tv_id: int, season: int):
    """默认结构：/tv/{id}/season/{n}，返回集列表或 []。"""
    d = tmdb_get(f"/tv/{tv_id}/season/{season}")
    if not tmdb_ok(d):
        return []
    return d.get("episodes") or []


def episode_default(tv_id: int, season: int, episode: int):
    d = tmdb_get(f"/tv/{tv_id}/season/{season}/episode/{episode}")
    if tmdb_ok(d) and d.get("episode_number") == episode:
        return d
    return None


def _fetch_flat_groups(tv_id: int) -> list:
    """拉取并扁平化所有剧集组：[(group_name, sub_name, season_num, order, ep_dict)]（无缓存逻辑）。"""
    d = tmdb_get(f"/tv/{tv_id}/episode_groups")
    out = []
    if tmdb_ok(d):
        for grp in d.get("results") or []:
            gid = grp.get("id")
            gname = grp.get("name") or ""
            detail = tmdb_get(f"/tv/episode_group/{gid}")
            if not tmdb_ok(detail):
                continue
            for sub in detail.get("groups") or []:
                subname = sub.get("name") or ""
                # 子组名 -> 飞牛季号
                snum = 0
                m = re.search(r"([0-9]+)", subname)
                if m and "season" in subname.lower():
                    snum = int(m.group(1))
                elif m and subname.lower().startswith("s") and not subname.lower().startswith("sp"):
                    snum = int(m.group(1))
                for ep in sub.get("episodes") or []:
                    ep = dict(ep)
                    ep["_group"] = gname
                    ep["_sub"] = subname
                    ep["_sub_season"] = snum
                    out.append(ep)
    return out


def _flat_groups(tv_id: int, force: bool = False) -> list:
    """扁平化所有剧集组（带 TTL 缓存；force=True 跳过 TTL，供占位数据强刷）。
    只缓存非空结果：TMDB 瞬时故障时沿用旧缓存，避免把空结果永久写进缓存。"""
    now = time.time()
    cached = STATE["group_cache"].get(tv_id)
    if not force and cached and now - STATE["group_ts"].get(tv_id, 0) < GROUP_TTL:
        return cached
    out = _fetch_flat_groups(tv_id)
    if out:
        STATE["group_cache"][tv_id] = out
        STATE["group_ts"][tv_id] = now
        _save_caches()
    elif cached:
        # 重取失败：沿用旧缓存并退避一个 TTL
        STATE["group_ts"][tv_id] = now
    return out or cached or []


def group_priority(tv_id: int) -> list:
    """剧集组优先级列表（返回按偏好排序的组名，精确匹配优先）。"""
    d = tmdb_get(f"/tv/{tv_id}/episode_groups")
    if not tmdb_ok(d):
        return []
    names = [x.get("name") or "" for x in d.get("results") or []]
    order = []
    for want in ("Seasons", "All Episodes by Season", "Episodes by Season", "Absolute Order", "Absolute", "剧集", "Story Arc",
                 "Chapter Arc", "Cours"):
        for n in names:
            if n == want:
                if n not in order:
                    order.append(n)
    for want in ("Seasons", "Absolute", "剧集", "Story Arc", "Chapter Arc", "Cours"):
        for n in names:
            if want.lower() in n.lower() and n not in order:
                order.append(n)
    for n in names:
        if n not in order:
            order.append(n)
    return order


def main_group_flat(tv_id: int) -> list:
    """按优先级返回第一个有数据且包含季节子组的主组扁平列表。"""
    flat_all = _flat_groups(tv_id)
    if not flat_all:
        return []
    for gname in group_priority(tv_id):
        items = [e for e in flat_all if e.get("_group") == gname]
        if not items:
            continue
        # 必须含 >0 的季节子组（跳过 Absolute Order 这类无季结构组）
        if any(int(e.get("_sub_season") or 0) > 0 for e in items):
            return items
    for gname in group_priority(tv_id):
        items = [e for e in flat_all if e.get("_group") == gname]
        if items:
            return items
    return flat_all


def season_range_of(flat: list, season: int):
    """主组内某季的全局号范围：(min_ep, max_ep, [episodes])。"""
    items = [e for e in flat if int(e.get("_sub_season") or -1) == season]
    if not items:
        return None
    nums = sorted(int(e.get("episode_number") or 0) for e in items)
    return nums[0], nums[-1], items


def _ep_is_placeholder(e: dict) -> bool:
    """已播出但仍无剧照且无简介 → TMDB 占位数据（缓存构建早于该集数据完善）。"""
    if not e:
        return False
    air = str(e.get("air_date") or "")
    if not air or air > time.strftime("%Y-%m-%d"):
        return False  # 未播出或日期未知，不触发
    return not e.get("still_path") and not e.get("overview")


def _group_refetch(tv_id: int) -> bool:
    """限频强制重取剧集组（命中占位数据或找不到集时调用）。"""
    now = time.time()
    if now - STATE["group_refetch_at"].get(tv_id, 0) < GROUP_REFETCH_MIN:
        return False
    STATE["group_refetch_at"][tv_id] = now
    log_line(f"[group] tv{tv_id} 缓存疑似过期（占位/缺集），强制重取剧集组")
    _flat_groups(tv_id, force=True)
    return True


def _match_episode_in_group(tv_id: int, season: int, ep: int):
    """在主剧集组中匹配：先按全局号直接找，找不到按季内号换算（季起始号+ep-1）。
    主组里没有这一季时返回 None（由调用方退回默认主表结构）：
    跨季全局号兜底只对"组内已有的季"生效，否则组未收录新播季时会把
    S3E11 按全局 11 错配成 S1E11（无职转生 Season 3 实测）。
    返回 (ep_dict, group_name, season_num) 或 (None, "", season)。"""
    flat = main_group_flat(tv_id)
    rng = season_range_of(flat, season)
    if not rng:
        return None, "", season
    start, end, items = rng
    # 1) 全局号直接命中（文件 ep 落在本季范围内）
    if start <= ep <= end:
        for e in items:
            if int(e.get("episode_number") or 0) == ep:
                return e, e.get("_group", ""), season
    # 2) 季内号换算：ep 小于季起始号 → 视为季内号
    if ep < start:
        target = start + ep - 1
        for e in items:
            if int(e.get("episode_number") or 0) == target:
                return e, e.get("_group", ""), season
    # 3) 本季范围未命中（文件季号与组季布局不一致，如绝对顺序组）：全局号跨季兜底
    for e in flat:
        if int(e.get("episode_number") or 0) == ep:
            return e, e.get("_group", ""), int(e.get("_sub_season") or 0) or season
    return None, "", season


def match_episode(tv_id: int, season: int, ep: int):
    """在主剧集组中匹配；命中占位数据或整组找不到时，限频强刷剧集组后重试一次。"""
    gep, gname, s_num = _match_episode_in_group(tv_id, season, ep)
    if (gep is None or _ep_is_placeholder(gep)) and _group_refetch(tv_id):
        return _match_episode_in_group(tv_id, season, ep)
    return gep, gname, s_num


# --------------------------------------------------------------------------
# 文件名/标题工具
# --------------------------------------------------------------------------
CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def parse_chinese_num(s: str):
    s = s.strip()
    if s.isdigit():
        return int(s)
    if s in CN_NUM:
        return CN_NUM[s]
    if s.startswith("十"):
        return 10 + (CN_NUM.get(s[1:], 0) if len(s) > 1 else 0)
    if s.endswith("十"):
        return CN_NUM.get(s[0], 1) * 10
    return 0


def parse_filename(path: str):
    parent = os.path.basename(os.path.dirname(path) or "")
    grand = os.path.basename(os.path.dirname(os.path.dirname(path)) or "")
    stem = os.path.splitext(os.path.basename(path))[0]
    year_m = re.search(r"\((\d{4})\)", path)
    year = int(year_m.group(1)) if year_m else 0
    se_m = re.search(r"[Ss](\d+)[Ee](\d+)", path)
    season, episode = (int(se_m.group(1)), int(se_m.group(2))) if se_m else (0, 0)
    zh_m = re.search(r"第([一二三四五六七八九十\d]+)季", parent + " " + stem)
    if zh_m:
        s = parse_chinese_num(zh_m.group(1))
        if s and not season:
            season = s
    return {"parent": parent, "grand": grand, "stem": stem,
            "year": year, "season": season, "episode": episode}


def clean_title(s: str, year: int = 0, season: int = 0) -> str:
    t = re.sub(r"\((\d{4})\)", "", s)
    t = re.sub(r"[Ss]\d+[Ee]?\d*", "", t)
    t = re.sub(r"第[一二三四五六七八九十\d]+季.*", "", t)
    t = re.sub(r"Season\s*\d+", "", t, flags=re.I)
    # 季文件夹名不是片名（"Specials" 曾被当成剧名搜出无关剧集）
    t = re.sub(r"(?i)^(specials?|ova|oad|extras|trailers?|others?|ncop|nced|menu)s?$", "", t.strip())
    # 清晰度/片源标签不是片名一部分（"让子弹飞 1080p" 搜不到）
    t = re.sub(r"(?i)(^|\s)(1080p|720p|2160p|4k|uhd|bluray|blu-ray|web-?dl|webrip|hdtv|remux|h\.?26[45]|x26[45]|hdr10?\+?)(\s|$)", " ", t)
    t = re.sub(r"\s+", " ", t).strip(" -_.")
    return t


def pick_tv(results, query, year):
    best, best_score = None, -1
    q = re.sub(r"\s+", "", query).lower()
    for r in results:
        name = (r.get("name") or "") + " " + (r.get("original_name") or "")
        n = re.sub(r"\s+", "", name).lower()
        score = 0
        if q and q in n:
            score += 10
        elif n and n in q:
            score += 8
        else:
            score += int(6 * len(set(q) & set(n)) / max(len(set(q)), 1))
        if year and (r.get("first_air_date") or "").startswith(str(year)):
            score += 3
        if score > best_score:
            best, best_score = r, score
    return best


def pick_movie(results, query, year):
    """电影候选择优：评分逻辑与 pick_tv 一致，字段对齐 title/original_title/release_date。
    返回 (原始候选, 得分)，调用方按得分设阈值，避免垃圾文件名匹配到不相干电影。"""
    best, best_score = None, -1
    q = re.sub(r"\s+", "", query).lower()
    for r in results:
        name = (r.get("title") or "") + " " + (r.get("original_title") or "")
        n = re.sub(r"\s+", "", name).lower()
        score = 0
        if q and q in n:
            score += 10
        elif n and n in q:
            # 短标题几乎总能被垃圾/带后缀的文件名"包含"（如《无职转生》总集篇
            # 抢走"无职转生 特别篇"文件），反向包含要求标题足够长才足为凭
            score += 8 if len(n) >= 6 else 0
        else:
            score += int(6 * len(set(q) & set(n)) / max(len(set(q)), 1))
        if year and (r.get("release_date") or "").startswith(str(year)):
            score += 3
        if score > best_score:
            best, best_score = r, score
    return best, best_score


# --------------------------------------------------------------------------
# 响应构建
# --------------------------------------------------------------------------
def img_path(path: str, kind: str = "post"):
    """TMDB 图片相对路径（如 /xxx.jpg），飞牛会拼 /t/p/original 前缀请求。
    同时记录图片类型（post/backdrop/logo/still），代理下载时按类型选尺寸。"""
    if not path:
        return ""
    _remember_img_type(path, kind)
    return "/" + path.lstrip("/")


def build_episode(trim, tmdb_id, ep, season_number: int, episode_number: int) -> dict:
    data = {
        "trim_id": trim,
        "tmdb_id": tmdb_id,
        "imdb_id": "",
        "pinYin": {},
        "air_date": (ep.get("air_date") or "") if ep else "",
        "episode_number": episode_number,
        "name": (ep.get("name") or "") if ep else "",
        "overview": (ep.get("overview") or "") if ep else "",
        "runtime": int(ep.get("runtime") or 0) if ep else 0,
        "season_number": season_number,
        "still_path": img_path(ep.get("still_path"), "still") if ep and ep.get("still_path") else "",
        "vote_average": float(ep.get("vote_average") or 0) if ep else 0,
        "vote_count": int(ep.get("vote_count") or 0) if ep else 0,
        "episode_imdb_id": "",
    }
    data["data_version"] = data_version(data)
    return data


def build_clean_data(tmdb_id) -> dict:
    return {"trimId": f"tm{tmdb_id}", "tmdbId": tmdb_id, "imdbId": "", "doubanId": 0, "pinYin": {}}


def build_tv(tv: dict) -> dict:
    name = tv.get("name") or tv.get("original_name") or ""
    poster = tv.get("poster_path") or ""
    back = tv.get("backdrop_path") or ""
    score = tv.get("vote_average") or 0
    genres = [{"id": g.get("id"), "name": g.get("name")} for g in tv.get("genres") or []]
    seasons = tv.get("seasons") or []
    main = [s for s in seasons if s.get("season_number", 0) > 0]
    data = {
        "trim_id": f"tm{tv.get('id')}",
        "id": tv.get("id"),
        "imdb_id": "",
        "pinYin": {},
        "first_air_date": tv.get("first_air_date") or "",
        "genres": genres[:12],
        "last_air_date": "",
        "name": name,
        "number_of_episodes": sum(int(s.get("episode_count") or 0) for s in main) or tv.get("number_of_episodes"),
        "number_of_seasons": len(main),
        "origin_country": tv.get("origin_country") or [],
        "original_name": tv.get("original_name") or name,
        "overview": tv.get("overview") or "",
        "poster_path": img_path(poster),
        "production_countries": [],
        "status": "",
        "vote_average": score,
        "vote_count": tv.get("vote_count") or 0,
        "alternative_titles": {"titles": []},
        "content_ratings": {"results": None},
        "images": {
            "backdrops": [{"file_path": img_path(back, "backdrop")}] if back else [],
            "logos": None,
            "posters": [{"file_path": img_path(poster)}] if poster else [],
        },
        "keywords": {"keywords": None},
        "adult": bool(tv.get("adult")),
    }
    data["data_version"] = data_version(data)
    return data


def build_movie(m: dict) -> dict:
    """构建 movie 详情对象：信封字段与 build_tv 同构，条目字段按 TMDB 电影命名
    （title/original_title/release_date/runtime），同时给 name/original_name 别名，
    兼容飞牛可能按剧集结构读字段。"""
    title = m.get("title") or m.get("original_title") or ""
    poster = m.get("poster_path") or ""
    back = m.get("backdrop_path") or ""
    genres = [{"id": g.get("id"), "name": g.get("name")} for g in m.get("genres") or []]
    data = {
        "trim_id": f"tm{m.get('id')}",
        "id": m.get("id"),
        "imdb_id": m.get("imdb_id") or "",
        "pinYin": {},
        "release_date": m.get("release_date") or "",
        "genres": genres[:12],
        "name": title,
        "title": title,
        "original_name": m.get("original_title") or title,
        "original_title": m.get("original_title") or title,
        "overview": m.get("overview") or "",
        "poster_path": img_path(poster),
        "production_countries": [
            {"iso_3166_1": c.get("iso_3166_1"), "name": c.get("name")}
            for c in m.get("production_countries") or []],
        "runtime": int(m.get("runtime") or 0),
        "status": m.get("status") or "",
        "tagline": m.get("tagline") or "",
        "vote_average": m.get("vote_average") or 0,
        "vote_count": m.get("vote_count") or 0,
        "alternative_titles": {"titles": []},
        "content_ratings": {"results": None},
        "images": {
            "backdrops": [{"file_path": img_path(back, "backdrop")}] if back else [],
            "logos": None,
            "posters": [{"file_path": img_path(poster)}] if poster else [],
        },
        "keywords": {"keywords": None},
        "adult": bool(m.get("adult")),
    }
    return data


def build_movie_out(mid: int, m: dict) -> dict:
    out = build_movie(m)
    out["data_version"] = data_version(out)
    return out


# --------------------------------------------------------------------------
# 各接口处理
# --------------------------------------------------------------------------
def handle_search_item(body: dict):
    path = body.get("fileName") or ""
    info = parse_filename(path)
    nfo = body.get("nfo") or {}
    # nfo 里 season=-1/缺失 表示未指定；显式的 0 是合法的第 0 季（特别篇），
    # 不能当缺失值覆盖回文件名解析结果（否则手动指定第 0 季会失效）
    ns, ne = nfo.get("season"), nfo.get("episode")
    req_season = int(ns) if ns is not None else -1
    req_episode = int(ne) if ne is not None else -1
    if req_season < 0:
        req_season = info["season"]
    if req_episode < 0:
        req_episode = info["episode"]
    query = clean_title(info["parent"], info["year"], info["season"]) or \
        clean_title(info["grand"], info["year"], info["season"]) or \
        clean_title(info["stem"], info["year"], info["season"])
    if not query:
        return fail(404, "not found")
    results = search_tv(query)
    subj = pick_tv(results, query, info["year"])
    if not subj:
        results2 = search_tv(clean_title(info["grand"], info["year"], 0) or query)
        subj = pick_tv(results2, query, info["year"])

    # 没有集号（电影/SP）：电影文件所在分类目录（电影/Movies）的 parent/grand
    # 是分类名不是片名，靠它搜出的剧集是噪声（"上海正午"曾被 parent"电影"搜出的
    # 无关剧集顶掉）——先按 stem 搜电影，命中达标就归电影；否则用剧集结果
    # （SP 文件 parent 即剧名），最后才用文件夹名搜电影兜底。飞牛对电影文件会
    # 先打 /search/byThirdPartyHash，404 后退回本接口（日志实测）。
    if req_episode <= 0:
        q_stem = clean_title(info["stem"], info["year"], 0)
        m_stem, s_stem = pick_movie(search_movie(q_stem), q_stem, info["year"]) if q_stem else (None, -1)
        if s_stem < _MOVIE_MIN_SCORE:
            m_stem = None
        is_movie = nfo.get("type") == "Movie" or m_stem is not None
        if is_movie:
            m = m_stem or _match_movie(info)[0]
            if m:
                log_line(f"[search] {query}: 电影命中 tm{int(m['id'])}")
                return ok({"cleanData": build_clean_data(int(m["id"])), "episode": None})
            if nfo.get("type") == "Movie":
                return fail(404, "not found")   # 明确是电影，别塞给剧集
        if subj:
            return ok({"cleanData": build_clean_data(int(subj["id"])), "episode": None})
        m = _match_movie(info)[0]
        if m:
            log_line(f"[search] {query}: 电影命中 tm{int(m['id'])}")
            return ok({"cleanData": build_clean_data(int(m["id"])), "episode": None})
        return fail(404, "not found")

    if not subj:
        return fail(404, "not found")
    tv_id = int(subj["id"])
    trim = f"tm{tv_id}"

    # 规则1：TMDB 主表（剧集主页的季结构，最权威、新季最全，如无职转生 Season 3）
    ep = episode_default(tv_id, req_season, req_episode)
    if ep:
        episode = build_episode(trim, tv_id, ep, req_season, req_episode)
        log_line(f"[search] {query}: 默认结构命中 S{req_season}E{req_episode}")
        _season_ctx_record(tv_id, req_season)
        return ok({"cleanData": build_clean_data(tv_id), "episode": episode})

    # 规则2：主表无此集（整季拆分/全局集号的旧结构，如 Re:Zero S1=85 集、
    # 文件 S04E78 用全局号）→ 主剧集组匹配（组内全局号/季内号换算）
    gep, gname, s_num = match_episode(tv_id, req_season, req_episode)
    if gep:
        episode = build_episode(trim, tv_id, gep, s_num, req_episode)
        log_line(f"[search] {query}: 剧集组 [{gname}] 命中 S{req_season}E{req_episode} -> S{s_num}")
        _season_ctx_record(tv_id, s_num)
        return ok({"cleanData": build_clean_data(tv_id), "episode": episode})

    return fail(404, "not found")


_MOVIE_MIN_SCORE = 8   # 只接受"标题包含/被包含"级别的匹配，拦截垃圾文件名的胡乱命中


def _match_movie(info: dict):
    """按文件名搜电影：stem（文件名）优先，parent/grand（所在文件夹名）兜底。
    文件夹名常是存储层噪音（"电影"、"vol02"、"1000-1-hash"），兜底查询
    须 ≥4 字且含非 ASCII 字符才当片名搜，否则会命中 VOL.02 之类同名噪声。
    返回 (TMDB电影候选, 得分) 或 (None, -1)。"""
    for i, src in enumerate((info["stem"], info["parent"], info["grand"])):
        query = clean_title(src, info["year"], 0)
        if not query:
            continue
        if i and (len(re.sub(r"\s+", "", query)) < 4 or not re.search(r"[^\x00-\x7f]", query)):
            continue
        m, score = pick_movie(search_movie(query), query, info["year"])
        if m and score >= _MOVIE_MIN_SCORE:
            return m, score
    return None, -1


def handle_search_by_hash(body: dict):
    """电影文件识别入口（/search/by 同处理）：飞牛对电影先打本接口再退回
    /search/item。TMDB 没有 hash 体系，thirdPartyHash 仅透传不参与匹配，
    退化为文件名搜索电影；响应信封与 /search/item 一致（电影无集 → episode null）。"""
    info = parse_filename(body.get("fileName") or "")
    m, score = _match_movie(info)
    if not m:
        return fail(404, "not found")
    log_line(f"[search] {clean_title(info['stem'], info['year']) or info['stem']}: 电影(hash)命中 tm{int(m['id'])} score={score}")
    return ok({"cleanData": build_clean_data(int(m["id"])), "episode": None})


def handle_detail_movie(body: dict):
    mid = _parse_tm_id(body.get("sourceId") or "")
    if not mid:
        return fail(404, "not found")
    m = movie_detail(mid)
    if not m:
        return fail(404, "not found")
    return ok({"cleanData": build_clean_data(mid), "movie": build_movie_out(mid, m)})


def season_poster_rel(tv_id: int, tv: dict, season: int) -> str:
    """季封面相对路径：TMDB 季节海报 -> 主海报（带缓存）。"""
    key = (tv_id, season)
    with _cache_lock:
        if key in STATE["poster_cache"]:
            return STATE["poster_cache"][key]
    d_season = tmdb_get(f"/tv/{tv_id}/season/{season}")
    rel = ""
    if tmdb_ok(d_season) and d_season.get("poster_path"):
        rel = d_season["poster_path"]
    if not rel:
        rel = tv.get("poster_path") or ""
    with _cache_lock:
        STATE["poster_cache"][key] = rel
    _save_caches()
    return rel


def _group_numbered_seasons(flat: list) -> int:
    """主剧集组里的编号季个数（sub_season > 0 的去重季号数）。"""
    return len({int(e.get("_sub_season") or 0) for e in flat
                if int(e.get("_sub_season") or 0) > 0})


def structure_source_is_main(tv: dict, group_seasons: int) -> bool:
    """季/集结构层的数据源选择：只有主剧集组比主表"认识"更多的编号季时
    （主表把多季塞在一个编号季里用全局号的旧结构，如 Re:Zero S1=85 集、
    我独自升级两季合一只报 S1=25，而组里明确拆了 Season 1/2）才按剧集组出结构；
    其余情况一律主表——包括真单季番（如 Silent Witch：主表 S1=13 是全部，
    剧集组反而含重复集号的脏数据），主表结构即最终结构，不引入剧集组。"""
    n_main = sum(1 for s in (tv.get("seasons") or [])
                 if int(s.get("season_number") or 0) > 0 and int(s.get("episode_count") or 0) > 0)
    return group_seasons <= n_main


def build_tv_out(tv_id: int, tv: dict) -> dict:
    """构建 tv 详情对象（含 seasons[] 与 data_version）。"""
    out = build_tv(tv)
    # 结构层数据源与单集匹配同源：主表优先（structure_source_is_main），
    # 另一侧补齐缺的季——主表缺新季（剧集组未收录的反向：组有主表无）或
    # 剧集组未收录新播季（主表有组无）时互相补，保证季行完整可归档
    by_season = {}
    flat = main_group_flat(tv_id)
    if flat:
        for e in flat:
            s = int(e.get("_sub_season") or 0)
            if s > 0:
                by_season.setdefault(s, []).append(int(e.get("episode_number") or 0))
    main_ep_count = {}
    for s in tv.get("seasons") or []:
        sn = int(s.get("season_number") or 0)
        if sn >= 0:  # 第 0 季（特别篇）也要，否则 S0 不出现在季结构里
            main_ep_count[sn] = int(s.get("episode_count") or 0)
    main_first = structure_source_is_main(tv, len(by_season))

    def _cnt(s: int) -> int:
        if main_first:
            return main_ep_count.get(s) or len(by_season.get(s) or [])
        return len(by_season.get(s) or []) or main_ep_count.get(s, 0)

    # 正季数量只统计 S1+（S0 是特别篇，不计入 number_of_seasons）
    n = max(len([s for s in by_season if s >= 1]),
            len([s for s in main_ep_count if s >= 1]))
    if n:
        out["number_of_seasons"] = n
        out["number_of_episodes"] = sum(_cnt(s) for s in range(1, n + 1))
        # 标准 TMDB seasons[]（飞牛可能据此创建/更新季行、季封面）
        # 第 0 季（特别篇）也要给出，否则飞牛拿到的季结构里没有 S0，
        # 会把 S00Exx 的集归到第 1 季（无职转生 S00E01→S1E1 实测）
        season_nums = ([0] if _cnt(0) else []) + list(range(1, n + 1))
        seasons_out = []
        for s in season_nums:
            rel = season_poster_rel(tv_id, tv, s)
            seasons_out.append({
                # 飞牛 Go 结构体 TvSeasons[].id 是 string，返回数字会导致
                # 整个 detail/tv 响应 unmarshal 失败、刮削结果被整单丢弃
                "id": str(s),
                "name": f"第 {s} 季" if s else "特别篇",
                "overview": tv.get("overview") or "",
                "poster_path": img_path(rel),
                "season_number": s,
                "episode_count": _cnt(s),
                "air_date": tv.get("first_air_date") or "",
            })
        out["seasons"] = seasons_out
    out["data_version"] = data_version(out)
    return out


def handle_detail_tv(body: dict):
    tv_id = _parse_tm_id(body.get("sourceId") or "")
    if not tv_id:
        return fail(404, "not found")
    tv = tv_detail(tv_id)
    if not tv:
        return fail(404, "not found")
    out = build_tv_out(tv_id, tv)
    return ok({"cleanData": build_clean_data(tv_id), "tv": out})


def build_season_out(tv_id: int, tv: dict, season: int) -> dict:
    """构建 season 详情对象（含 episodes 与 data_version）。
    季集列表数据源与单集匹配同源：主表结构可靠时主表优先（缺该季用剧集组补），
    主表是全局号塞 S1 的旧结构时剧集组优先（缺该季用主表补）。"""
    trim = f"tm{tv_id}"
    # 季封面：TMDB 季节海报 -> 主海报
    poster_rel = season_poster_rel(tv_id, tv, season)

    def _from_default(eps: list) -> list:
        return [build_episode(trim, tv_id, e, season, int(e.get("episode_number") or 0))
                for e in eps]

    def _from_group(items: list) -> list:
        items = sorted(items, key=lambda e: int(e.get("episode_number") or 0))
        return _from_default(items)

    flat = main_group_flat(tv_id)
    rng = season_range_of(flat, season)
    eps_default = season_episodes_default(tv_id, season)
    main_first = structure_source_is_main(tv, _group_numbered_seasons(flat))
    if main_first:
        ep_out = _from_default(eps_default) if eps_default else (_from_group(rng[2]) if rng else [])
    else:
        ep_out = _from_group(rng[2]) if rng else (_from_default(eps_default) if eps_default else [])
    if not ep_out:
        return {}
    season_obj = {
        "trim_id": trim,
        "tmdb_id": tv_id,
        "imdb_id": "",
        "pinYin": {},
        "air_date": tv.get("first_air_date") or "",
        "name": "特别篇" if season == 0 else f"第 {season} 季",
        "overview": tv.get("overview") or "",
        "poster_path": img_path(poster_rel),
        "season_number": season,
        "episode_count": len(ep_out),
        "episodes": ep_out,
        "credits": {"cast": [], "crew": []},
        "images": {
            "backdrops": [{"file_path": img_path(tv.get("backdrop_path"), "backdrop")}] if tv.get("backdrop_path") else [],
            "logos": None,
            "posters": [{"file_path": img_path(poster_rel)}] if poster_rel else [],
        },
    }
    season_obj["data_version"] = data_version(season_obj)
    return season_obj


def handle_detail_season(body: dict):
    tv_id = _parse_tm_id(body.get("sourceId") or "")
    if not tv_id:
        return fail(404, "not found")
    season = _season_from_body_or_ctx(tv_id, body)  # 缺 season ≠ S1，飞牛对 S0 不发该字段
    tv = tv_detail(tv_id)
    if not tv:
        return fail(404, "not found")
    season_obj = build_season_out(tv_id, tv, season)
    if not season_obj:
        return fail(404, "not found")
    return ok({"cleanData": build_clean_data(tv_id), "season": season_obj})


def handle_detail_season_episode(body: dict):
    tv_id = _parse_tm_id(body.get("sourceId") or "")
    if not tv_id:
        return fail(404, "not found")
    season = _season_from_body_or_ctx(tv_id, body)  # 缺 season ≠ S1，飞牛对 S0 不发该字段
    ep = int(body.get("episode") or 0)
    if ep <= 0:
        return fail(404, "not found")
    trim = f"tm{tv_id}"
    # 主表优先，未命中再走剧集组（与 /search/item 一致）
    e = episode_default(tv_id, season, ep)
    if e:
        return ok({"cleanData": build_clean_data(tv_id),
                   "episode": build_episode(trim, tv_id, e, season, ep)})
    gep, _g, s_num = match_episode(tv_id, season, ep)
    if gep:
        return ok({"cleanData": build_clean_data(tv_id),
                   "episode": build_episode(trim, tv_id, gep, s_num, ep)})
    return fail(404, "not found")


def _structure_season_count(tv_id: int) -> int:
    """与 /detail/tv 同源的结构季数（主表与剧集组取大），供搜索列表展示/校验。"""
    tv = tv_detail(tv_id)
    if not tv:
        return 0
    group_seasons = set()
    for e in main_group_flat(tv_id):
        s = int(e.get("_sub_season") or 0)
        if s > 0:
            group_seasons.add(s)
    n_main = sum(1 for s in tv.get("seasons") or [] if int(s.get("season_number") or 0) > 0)
    return max(len(group_seasons), n_main)


def handle_search_multi(body: dict):
    keyword = (body.get("keyword") or "").strip()
    if not keyword:
        return ok({"list": []})
    results = search_tv(keyword)[:20]
    # 补真实结构季数：TMDB 搜索接口不含季数字段，透传 0 会让飞牛匹配弹窗
    # 拿到"共 0 季"的候选，选中超后无后续动作（无职转生 S3 手动匹配实测）
    with ThreadPoolExecutor(max_workers=5) as ex:
        counts = list(ex.map(lambda r: _structure_season_count(int(r.get("id") or 0)), results[:10]))
    out = []
    for i, r in enumerate(results):
        out.append({
            "source": "trim_id",
            "sourceId": f"tm{r.get('id')}",
            "type": "tv",
            "name": r.get("name") or "",
            "posterPath": img_path(r.get("poster_path")),
            "genres": [],
            "productionCountries": [],
            "firstAirDate": r.get("first_air_date") or "",
            "lastAirDate": "",
            "numberOfSeasons": counts[i] if i < len(counts) else 0,
        })
    # 电影候选追加在剧集之后：手动搜索电影名时也能选中（type=movie，
    # 上映日复用 firstAirDate 字段装；电影无季，numberOfSeasons=0）
    for r in search_movie(keyword)[:10]:
        out.append({
            "source": "trim_id",
            "sourceId": f"tm{r.get('id')}",
            "type": "movie",
            "name": r.get("title") or "",
            "posterPath": img_path(r.get("poster_path")),
            "genres": [],
            "productionCountries": [],
            "firstAirDate": r.get("release_date") or "",
            "lastAirDate": "",
            "numberOfSeasons": 0,
        })
    return ok({"list": out})


_img_lock = threading.Lock()
_img_inflight = {}   # 子路径 -> Event（同一张图并发下载去重）


def _img_cache_path(sub: str) -> str:
    ext = os.path.splitext(sub)[1] or ".jpg"
    return os.path.join(SCRIPT_DIR, "cache", "img",
                        hashlib.sha1(sub.encode("utf-8")).hexdigest() + ext)


def _remember_img_type(filename: str, kind: str):
    """记录图片文件名 -> 类型(post/backdrop/logo/still)，代理按类型选尺寸。"""
    if not filename:
        return
    k = filename.lstrip("/")
    if STATE["img_types"].get(k) != kind:
        STATE["img_types"][k] = kind
        STATE["img_types_dirty"] = True


def _img_types_autosave():
    """图片类型映射节流落盘（60s 一次），避免扫描期间频繁写文件。"""
    now = time.time()
    if STATE["img_types_dirty"] and now - _img_types_last_save[0] > 60:
        _img_types_last_save[0] = now
        _save_caches()


_img_types_last_save = [0.0]

# 飞牛原源交付的海报实测约 w500 级（30~300KB webp），backdrop 常为 1920 宽。
# 这里按类型降尺寸取图，兼顾清晰度与下载耗时；未知类型兜底降档，避免回落原图。
_SIZE_BY_TYPE = {
    "post": "w500",
    "backdrop": "w1280",
    "logo": "w500",
    "still": "w500",
}
_FALLBACK_SIZE = "w500"   # 类型未记录的图片也降档，不取原图


def _downgrade_sub(sub: str) -> str:
    """original/xxx.jpg -> w500/xxx.jpg（按记录的图片类型，未知类型兜底 _FALLBACK_SIZE）。
    配置 img_original_size=true 时不降尺寸，始终取原图。"""
    if STATE["config"].get("img_original_size"):
        return sub
    m = re.match(r"^original/(.+)$", sub)
    if not m:
        return sub
    fname = m.group(1)
    size = _SIZE_BY_TYPE.get(STATE["img_types"].get(fname), _FALLBACK_SIZE)
    return f"{size}/{fname}"


def fetch_tmdb_bytes(sub: str) -> bytes:
    """按 /t/p/ 子路径取 TMDB 图片（keep-alive + 磁盘缓存 + 并发去重 + 按类型降尺寸）。"""
    want = _downgrade_sub(sub)
    cf = _img_cache_path(want)
    try:
        with open(cf, "rb") as f:
            return f.read()
    except OSError:
        pass
    with _img_lock:
        ev = _img_inflight.get(want)
        owner = ev is None
        if owner:
            ev = threading.Event()
            _img_inflight[want] = ev
    if not owner:
        # 已有别的线程在同一下载：等它落盘
        ev.wait(60)
        try:
            with open(cf, "rb") as f:
                return f.read()
        except OSError:
            pass
    try:
        base = (STATE["config"].get("img_base") or IMG_BASE).rstrip("/")
        _status, ct, raw = http_fetch(f"{base}/t/p/{want}", timeout=20)
        if _status != 200 or not raw:
            raise RuntimeError(f"img http {_status}")
        try:
            os.makedirs(os.path.dirname(cf), exist_ok=True)
            with open(cf, "wb") as f:
                f.write(raw)
        except OSError:
            pass
        return raw
    finally:
        with _img_lock:
            if _img_inflight.get(want) is ev:
                _img_inflight.pop(want, None)
            ev.set()


def _img_cache_cleanup():
    """图片磁盘缓存保底清理：超过 IMG_CACHE_MAX 时按文件从旧到新删到 IMG_CACHE_KEEP。
    缓存纯可再生，删掉的图下次请求会按当前尺寸自动重新下载；
    文件写入即定稿（mtime=落盘时间），按 mtime 排序即按新鲜度淘汰。"""
    try:
        entries = []
        total = 0
        for root, _dirs, files in os.walk(IMG_CACHE_DIR):
            for f in files:
                fp = os.path.join(root, f)
                try:
                    st = os.stat(fp)
                except OSError:
                    continue
                entries.append((st.st_mtime, st.st_size, fp))
                total += st.st_size
        if total <= IMG_CACHE_MAX:
            return
        entries.sort()
        removed = freed = 0
        for _mt, size, fp in entries:
            if total - freed <= IMG_CACHE_KEEP:
                break
            try:
                os.unlink(fp)
                removed += 1
                freed += size
            except OSError:
                continue
        log_line(f"[imgcache] 图片缓存 {total // 1048576}MB 超过 {IMG_CACHE_MAX // 1048576}MB，"
                 f"已删 {removed} 个最旧文件（约 {freed // 1048576}MB），当前约 {(total - freed) // 1048576}MB")
    except Exception as e:
        log_line(f"[imgcache] 清理异常: {e}")


def _img_cache_cleanup_loop():
    time.sleep(300)  # 启动后 5 分钟做首次检查，随后每日一次
    while True:
        _img_cache_cleanup()
        time.sleep(IMG_CACHE_CHECK_EVERY)


def _prefetch_images(items):
    """并行预取候选图片：items=[(type, path), ...]，边下边显，二次打开全走缓存。"""
    def _one(it):
        kind, p = it
        try:
            _remember_img_type(p, kind)
            fetch_tmdb_bytes("original/" + p.lstrip("/"))
        except Exception:
            pass
    with ThreadPoolExecutor(max_workers=4) as ex:
        list(ex.map(_one, items))
    _img_types_autosave()


# --------------------------------------------------------------------------
# 相似图分组：同一张图的不同分辨率/语言版本归为一组，只留最大的一张
# （TMDB 网页的 "Ungroup Similar Images" 反向功能）
# 相似度：12x12 RGB 的 NCC（rgb）+ 24x24 灰度梯度幅值的 NCC（edge）。
# 实测标定（Dr.STONE/Re:Zero 人工核对 20 对）：同图 rgb ≥0.73；异图 rgb ≤0.61
# 且 edge ≤0.17（同系列异图 rgb 落在 0.4~0.61 的灰色带，靠 edge 拦截）。
# 判同规则：rgb ≥ 0.71，或 rgb ≥ 0.50 且 edge ≥ 0.55。
# （异图 rgb 最高实测 0.687，同图 rgb 最低实测 0.733，0.71 居中）
# 曾用 9x8 dHash：对文字/裁切变体漏判、对不同图误判，已弃用。
# --------------------------------------------------------------------------
FFMPEG_BIN = shutil.which("ffmpeg") or "/usr/bin/ffmpeg"
_SIM_RGB = 0.71
_SIM_RGB2 = 0.50
_SIM_EDGE = 0.55


def _thumb_sub(path: str, kind: str) -> str:
    size = "w300" if kind == "backdrop" else "w92"   # 各类型最小档
    return f"{size}/{path.lstrip('/')}"


def _sim_feat(path: str, kind: str):
    """候选图特征 (rgb12, edge24)，均为去均值 float 列表，持久缓存。"""
    fp = path.lstrip("/")
    with _cache_lock:
        hit = STATE["img_feat"].get(fp)
    if hit is not None:
        return _feat_unpack(hit)
    sub = _thumb_sub(path, kind)
    try:
        fetch_tmdb_bytes(sub)          # 缩略图落盘（cache/img）
        cf = _img_cache_path(sub)
        rgb = subprocess.run(
            [FFMPEG_BIN, "-y", "-i", cf,
             "-vf", f"scale=12:12:force_original_aspect_ratio=increase,crop=12:12",
             "-f", "rawvideo", "-pix_fmt", "rgb24", "-v", "error", "pipe:1"],
            capture_output=True, timeout=15).stdout
        gray = subprocess.run(
            [FFMPEG_BIN, "-y", "-i", cf,
             "-vf", "scale=24:24:force_original_aspect_ratio=increase,crop=24:24,format=gray",
             "-f", "rawvideo", "-pix_fmt", "gray", "-v", "error", "pipe:1"],
            capture_output=True, timeout=15).stdout
        if len(rgb) < 432 or len(gray) < 576:
            return None
        packed = _feat_pack(rgb, gray)
        with _cache_lock:
            STATE["img_feat"][fp] = packed
        return _feat_unpack(packed)
    except Exception:
        return None


def _center(v):
    m = sum(v) / len(v)
    return [x - m for x in v]


def _feat_pack(rgb: bytes, gray: bytes):
    return rgb.hex() + "|" + gray.hex()


def _feat_unpack(packed: str):
    rgb_hex, gray_hex = packed.split("|", 1)
    return _center(list(bytes.fromhex(rgb_hex))), _center(grad_mag(list(bytes.fromhex(gray_hex))))


def grad_mag(g, w=24):
    """24x24 灰度的中心差分梯度幅值。"""
    out = []
    for y in range(w):
        row = y * w
        for x in range(w):
            gx = g[row + min(x + 1, w - 1)] - g[row + max(x - 1, 0)]
            gy = g[min(y + 1, w - 1) * w + x] - g[max(y - 1, 0) * w + x]
            out.append(math.sqrt(gx * gx + gy * gy))
    return out


def _ncc(a, b) -> float:
    """去均值向量的归一化互相关（cosine）。"""
    da = math.sqrt(sum(x * x for x in a))
    db = math.sqrt(sum(x * x for x in b))
    if da < 1e-6 or db < 1e-6:
        return 0.0
    return sum(a[i] * b[i] for i in range(len(a))) / (da * db)


def _similar(fa, fb) -> bool:
    """判同：rgb 相似度高，或 rgb 中等且边缘结构也相似。"""
    rgb = _ncc(fa[0], fb[0])
    if rgb >= _SIM_RGB:
        return True
    return rgb >= _SIM_RGB2 and _ncc(fa[1], fb[1]) >= _SIM_EDGE


def _dedupe_similar(items: list, kind: str) -> list:
    """items=[images 接口的图 dict]（已按偏好排序）。
    单链聚类：候选与组内任一成员判同即归组；若同时匹配多个组（桥接），
    将这些组合并。每组保留分辨率最高的成员，组间保持原顺序。"""
    if not items:
        return []
    with ThreadPoolExecutor(max_workers=8) as ex:
        feats = list(ex.map(lambda it: _sim_feat(it.get("file_path") or "", kind), items))

    def _area(it):
        return int(it.get("width") or 0) * int(it.get("height") or 0)

    groups = []   # [[feat, ...], rep_dict]
    for it, feat in zip(items, feats):
        if feat is None:
            groups.append([[None], it])
            continue
        matched = [g for g in groups
                   if g[0][0] is not None and any(_similar(f, feat) for f in g[0])]
        if not matched:
            groups.append([[feat], it])
            continue
        base = matched[0]
        for g in matched[1:]:          # 桥接组并集
            base[0].extend(g[0])
            if _area(g[1]) > _area(base[1]):
                base[1] = g[1]
            groups.remove(g)
        if _area(it) > _area(base[1]):
            base[1] = it
        base[0].append(feat)
    try:
        _save_caches()
    except OSError:
        pass
    return [g[1] for g in groups]


def handle_meta_images(body: dict):
    """POST /meta/images —— 飞牛"编辑元数据 → 搜索图片"弹窗数据源。

    请求: {"category":"tv","language":"zh-CN","trimId":"tm65942"}
    响应: data = {"poster": "<裸路径>", "posters": ["<裸路径>", ...],
                  "backdrops": [...], "logos": [...]}
    posters=竖版海报（网格第一组）；backdrops=横向剧照/背景图；logos=标题 logo。
    飞牛原源协议即这三个数组（对应 TMDB images 接口的 posters/backdrops/logos）。
    同一张图的多分辨率/多语言版本会先按感知哈希归组，每组只保留分辨率最高的
    一张（等价 TMDB 网页的相似图分组），哈希结果持久化，二次打开零开销。
    """
    cat = body.get("category") or "tv"
    tid = _parse_tm_id(body.get("trimId") or "")
    if not tid:
        return ok({"poster": "", "posters": [], "backdrops": [], "logos": []})

    kind = "movie" if cat == "movie" else "tv"
    try:
        imgs = tmdb_get(f"/{kind}/{tid}/images", include_image_language="zh,ja,en,null")
    except Exception:
        imgs = {}

    def build(lst, dedup_kind, limit, pre_cap):
        cand = [p for p in (lst or []) if p.get("file_path")]
        if dedup_kind == "post":
            cand.sort(key=lambda p: {"zh": 0, "ja": 1, "en": 2}.get(p.get("iso_639_1") or "", 3))
        cand = cand[:pre_cap]
        kept = _dedupe_similar(cand, dedup_kind)
        return [p["file_path"] for p in kept][:limit]

    posters = build(imgs.get("posters"), "post", 30, 40)
    backdrops = build(imgs.get("backdrops"), "backdrop", 20, 40)
    logos = build(imgs.get("logos"), "logo", 10, 20)

    cur = ""
    if kind == "tv":
        tv = tv_detail(tid)
        if tv:
            cur = tv.get("poster_path") or ""
    if not cur and posters:
        cur = posters[0]

    prefetch = [("post", p) for p in posters] + \
               [("backdrop", p) for p in backdrops] + \
               [("logo", p) for p in logos]
    threading.Thread(target=_prefetch_images, args=(prefetch,), daemon=True).start()
    return ok({"poster": cur, "posters": posters,
               "backdrops": backdrops, "logos": logos})


def handle_meta_diff(body: dict):
    cat = body.get("category") or ""
    tv_id = _parse_tm_id(body.get("trimId") or "")
    if not tv_id:
        return ok({"hasDiff": False})
    cur_ver = body.get("dataVersion") or ""

    if cat == "tv":
        tv = tv_detail(tv_id)
        if not tv:
            return ok({"hasDiff": False})
        out = build_tv_out(tv_id, tv)
        if out.get("data_version") == cur_ver:
            return ok({"hasDiff": False})
        tv_data = {k: v for k, v in out.items() if k != "data_version"}
        return ok({"hasDiff": True, "tv": tv_data})

    if cat == "movie":
        # 电影差量：此前落到单集分支恒 hasDiff:false，存量电影元数据被冻结
        m = movie_detail(tv_id)
        if not m:
            return ok({"hasDiff": False})
        out = build_movie_out(tv_id, m)
        if out.get("data_version") == cur_ver:
            return ok({"hasDiff": False})
        return ok({"hasDiff": True, "movie": {k: v for k, v in out.items() if k != "data_version"}})

    if cat == "season":
        season = _season_from_body_or_ctx(tv_id, body)  # 缺 seasonNumber ≠ S1，同上按上下文兜底
        tv = tv_detail(tv_id)
        if not tv:
            return ok({"hasDiff": False})
        season_obj = build_season_out(tv_id, tv, season)
        if not season_obj:
            return ok({"hasDiff": False})
        if season_obj.get("data_version") == cur_ver:
            return ok({"hasDiff": False})
        s_data = {k: v for k, v in season_obj.items() if k != "data_version"}
        return ok({"hasDiff": True, "season": s_data})

    # 单集：主表优先，未命中再走剧集组（seasonNumber=0 是特别篇，不能换成 S1）
    ep_number = int(body.get("episodeNumber") or -1)
    season = int(body.get("seasonNumber") or 0)
    if ep_number <= 0:
        return ok({"hasDiff": False})
    e = episode_default(tv_id, season, ep_number)
    s_num = season
    if not e:
        gep, _g, s_num = match_episode(tv_id, season, ep_number)
        e = gep
    if not e:
        return ok({"hasDiff": False})
    episode = build_episode(f"tm{tv_id}", tv_id, e, s_num, ep_number)
    if cur_ver == episode.get("data_version"):
        return ok({"hasDiff": False})
    return ok({"hasDiff": True, "episode": episode})


def handle_genres():
    d = tmdb_get("/genre/tv/list")
    if tmdb_ok(d):
        return ok({"genres": d.get("genres") or []})
    return ok({"genres": []})


def handle_match():
    return 404, b"404 page not found", "text/plain"


def _parse_tm_id(src: str) -> int:
    s = re.sub(r"^[a-zA-Z]+", "", src or "")
    try:
        return int(s)
    except ValueError:
        return 0


def _read_local_img(key: str):
    """在飞牛图片缓存目录(两层 hash 目录)中查找单文件 key,返回 bytes 或 None。"""
    root = STATE["config"].get("img_root") or "/vol1/@appmeta/trim.media/img"
    with _cache_lock:
        hit = STATE["local_img_cache"].get(key)
    if hit is not None:
        if not hit:
            return None
        try:
            with open(hit, "rb") as f:
                return f.read()
        except OSError:
            return None
    try:
        for d1 in sorted(os.listdir(root)):
            p1 = os.path.join(root, d1)
            if not os.path.isdir(p1):
                continue
            for d2 in sorted(os.listdir(p1)):
                fp = os.path.join(p1, d2, key)
                if os.path.isfile(fp):
                    with open(fp, "rb") as f:
                        raw = f.read()
                    with _cache_lock:
                        STATE["local_img_cache"][key] = fp
                    return raw
    except OSError:
        pass
    with _cache_lock:
        STATE["local_img_cache"][key] = ""
    return None


# --------------------------------------------------------------------------
# HTTP Handler
# --------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _body(self):
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
        except ValueError:
            length = 0
        return self.rfile.read(length) if length > 0 else b""

    def _send(self, status: int, body: bytes, ct: str = "application/json"):
        self.send_response(status)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _send_json(self, obj: dict, status: int = 200):
        self._send(status, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def _proxy_image(self, path: str):
        """图片代理（兼容新旧路径）：
        - /t/p/original/x.jpg -> image.tmdb.org/t/p/original/x.jpg
        - /t/p/original/t/p/w300/x.jpg（旧格式） -> image.tmdb.org/t/p/w300/x.jpg
        查找顺序：本地 img 缓存回退 -> TMDB（keep-alive + 按类型降尺寸 + 磁盘缓存）。
        """
        if not path.startswith("/t/p/"):
            self._send(404, b"404 page not found", "text/plain")
            return
        # 取最后一个 /t/p/ 之后的子路径（兼容双重前缀）
        idx = path.rfind("/t/p/")
        sub = path[idx + 5:] if idx >= 0 else path[len("/t/p"):]
        # 本地缓存文件名（支持带两层 hash 目录前缀，如 1e/17/poster-xxx.webp）
        fname = sub.rsplit("/", 1)[-1] if "/" in sub else sub
        m = re.match(r"^([\w.-]+)$", fname)
        if m:
            # 本地 img 缓存回退（飞牛缓存文件名，如 RXFg...webp / poster-xxx.webp）
            raw = _read_local_img(m.group(1))
            if raw is not None:
                key = m.group(1)
                ct = "image/webp" if key.lower().endswith(".webp") else "image/jpeg"
                self._send(200, raw, ct)
                return
        # TMDB 图片（磁盘缓存 + 并发去重，按类型降尺寸保证速度）
        try:
            raw = fetch_tmdb_bytes(sub)
            ct = "image/webp" if sub.lower().endswith(".webp") else "image/jpeg"
            self._send(200, raw, ct)
        except Exception:
            self._send(404, b"404 page not found", "text/plain")

    def _proxy_subtitle(self, split, body):
        url = SUBTITLE_UPSTREAM + split.path[len("/v1"):]
        if split.query:
            url += "?" + split.query
        headers = {k: v for k, v in self.headers.items()
                   if k.lower() not in ("host", "content-length", "connection", "accept-encoding")}
        req = urllib.request.Request(url, data=body or None, headers=headers, method=self.command)
        try:
            with urllib.request.urlopen(req, timeout=25) as resp:
                raw = resp.read()
            ct = resp.headers.get("Content-Type", "application/json")
            self._send(200, raw, ct)
        except urllib.error.HTTPError as e:
            raw = e.read()
            self._send(e.code, raw, e.headers.get("Content-Type", "text/plain"))
        except Exception as e:
            self._send_json(fail(5000, f"subtitle proxy error: {e}"))

    def _handle(self):
        STATE["counter"] += 1
        n = STATE["counter"]
        split = urllib.parse.urlsplit(self.path)
        body = self._body()
        if body:
            try:
                req_body = json.loads(body)
            except Exception:
                req_body = {}
        else:
            req_body = {}
        log_line("=" * 70)
        log_line(f"[{n}] {time.strftime('%Y-%m-%d %H:%M:%S')}  {self.command} {split.path}")
        if body:
            log_line(f"--- Body ({len(body)} bytes) ---")
            log_line(body.decode("utf-8", "replace")[:500])
        if self.command == "POST" and split.path != "/healthz":
            # 临时调试：记录请求头（抓飞牛的签名头，用于与原版 fnnas 响应对比）
            hdrs = {k: v for k, v in self.headers.items()
                    if k.lower() not in ("cookie", "authorization")}
            log_line(f"--- Headers: {json.dumps(hdrs, ensure_ascii=False)}")

        path = split.path
        try:
            if self.command == "GET" and path.startswith("/t/p/"):
                self._proxy_image(path)
                return
            if self.command == "GET" and path == "/match":
                self._send(404, b"404 page not found", "text/plain")
                return
            if path.startswith("/v1"):
                self._proxy_subtitle(split, body)
                return
            if path == "/healthz" and self.command == "GET":
                self._send_json(ok({"status": "ok"}))
                return
            if self.command != "POST":
                self._send(404, b"404 page not found", "text/plain")
                return
            if path == "/search/item":
                out = handle_search_item(req_body)
            elif path == "/search/multi":
                out = handle_search_multi(req_body)
            elif path in ("/search/byThirdPartyHash", "/search/by"):
                out = handle_search_by_hash(req_body)
            elif path == "/detail/tv":
                out = handle_detail_tv(req_body)
            elif path == "/detail/movie":
                out = handle_detail_movie(req_body)
            elif path == "/detail/tv/season":
                out = handle_detail_season(req_body)
            elif path == "/detail/tv/season/episode":
                out = handle_detail_season_episode(req_body)
            elif path == "/detail/person":
                out = fail(404, "not found")
            elif path == "/meta/diff":
                out = handle_meta_diff(req_body)
            elif path == "/meta/images":
                out = handle_meta_images(req_body)
            elif path == "/genres":
                out = handle_genres()
            else:
                out = fail(404, "not found")
            log_line(f"<<< Response: {json.dumps(out, ensure_ascii=False)[:500]}")
            _img_types_autosave()
            self._send_json(out)
        except Exception as e:
            import traceback
            log_line(f"[!] handler error: {e}")
            traceback.print_exc()
            self._send_json(fail(5000, str(e)))

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _handle

    def log_message(self, fmt, *args):
        pass


# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="TMDB 剧集组版 provider for trim.media")
    ap.add_argument("--config", default=os.path.join(SCRIPT_DIR, "tmdb_config.json"))
    ap.add_argument("--port", type=int, default=38080)
    args = ap.parse_args()

    cfg = {}
    if os.path.exists(args.config):
        try:
            cfg = json.load(open(args.config, encoding="utf-8"))
        except Exception:
            cfg = {}
    # 环境变量优先于配置文件（容器部署用：配置文件不入仓库，运行时注入）
    if os.environ.get("TMDB_API_KEY"):
        cfg["api_key"] = os.environ["TMDB_API_KEY"]
    if not cfg.get("api_key"):
        print("[!] 未配置 api_key（tmdb_config.json 或环境变量 TMDB_API_KEY）")
    STATE["config"] = cfg
    STATE["log_file"] = os.environ.get("TMDB_LOG_FILE") or cfg.get("log_file")
    _load_caches()

    port = int(os.environ.get("TMDB_PORT") or cfg.get("port") or args.port)
    bind = os.environ.get("TMDB_BIND") or cfg.get("bind") or "127.0.0.1"
    log_line(f"[start] TMDB provider on http://{bind}:{port}")
    log_line(f"[start] tmdb_base: {cfg.get('tmdb_base') or TMDB_BASE}")
    log_line(f"[start] api_key: {'configured' if cfg.get('api_key') else 'missing'}")
    threading.Thread(target=_img_cache_cleanup_loop, daemon=True).start()
    log_line(f"[start] 图片缓存保底清理：>{IMG_CACHE_MAX // 1048576}MB 时清到 {IMG_CACHE_KEEP // 1048576}MB，"
             f"每 {IMG_CACHE_CHECK_EVERY // 3600}h 检查一次")

    server = ThreadingHTTPServer((bind, port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log_line("[stop] TMDB provider stopped")
        server.server_close()


if __name__ == "__main__":
    main()
