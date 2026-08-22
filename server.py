"""Vinted 多账号监控服务端 —— 抓取在服务器,数据落 SQLite(带定期清理)。

架构:
  [本地 exe vinted_client.py] 纯展示
      └─ HTTP + X-API-Key ─► [本服务]
                             ├─ Scheduler 线程:每 interval_minutes 把全部账号抓一遍
                             │     → 全量商品 upsert 进 SQLite(items/snapshots/monitor_targets)
                             ├─ 定期清理:超过 retention_days 没再出现的商品 + 过期快照自动删除
                             ├─ 数据合并:客户端一次拉回所有账号的全部商品,单列表展示
                             └─ REST API: status / accounts CRUD / items / refresh

复用 monitor.py 当能力库(账号清单、IP 池轮换、make_session / ensure_token /
fetch_items / normalize_item),不调它的 run_check(那个每轮只换一次 IP,
1000 个账号全走同一出口必被封),这里自己编排"逐账号换 IP"。

启动:
  uvicorn server:app --host 0.0.0.0 --port 8000     (必须单 worker,防双调度)
  或 python server.py
"""
import json
import logging
import random
import re
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path

import requests
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

import monitor
import clash_pool

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("vinted-server")

HERE = Path(__file__).resolve().parent

# ============================ 配置 ============================

def _resolve_path(cfg, key):
    """配置里的相对路径都按脚本所在目录解析。"""
    v = (cfg.get(key) or "").strip()
    if not v:
        return ""
    p = Path(v)
    return str(p if p.is_absolute() else (HERE / p).resolve())


def load_config():
    path = HERE / "server_config.json"
    with open(path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    for key in ("accounts_file", "proxy_pool_file"):
        cfg[key] = _resolve_path(cfg, key)
    return cfg


CONFIG = load_config()


def apply_monitor_config(cfg):
    """把服务器配置注入 monitor 全局(domain / 默认代理 / cookie / 分页延迟)。"""
    monitor.CONFIG["domain"] = cfg.get("domain", "www.vinted.co.uk")
    monitor.CONFIG["proxy"] = cfg.get("default_proxy", "")
    monitor.CONFIG["access_token_web"] = cfg.get("access_token_web", "")
    monitor.CONFIG["page_delay"] = tuple(cfg.get("page_delay", [1.5, 3.5]))
    monitor.DEFAULT_PROXIES = None
    monitor._pool_index = -1  # 池游标清零,每轮从第 0 个开始轮换


apply_monitor_config(CONFIG)


def build_proxy_pool(cfg):
    """读池文件并过滤 clash: 条目(服务器无本地 Clash,留了也没法用)。

    空池 → 回退 default_proxy / 直连,记 WARN 提醒换住宅代理。
    """
    if not cfg.get("proxy_pool_file"):
        log.warning("未配置 proxy_pool_file,只会用 default_proxy 单出口")
        return []
    raw = monitor.read_pool_file(cfg["proxy_pool_file"])
    kept, skipped = [], []
    for line in raw:
        if clash_pool.spec_is_clash(line):
            skipped.append(line)
        else:
            kept.append(line)
    if skipped:
        log.warning("服务器无本地 Clash,已跳过 %d 条 clash: 条目", len(skipped))
    if not kept:
        log.warning("代理池为空,所有账号会共用 default_proxy 出口 —— 多账号必被封,请填住宅代理池")
    return kept


monitor.PROXY_POOL = build_proxy_pool(CONFIG)

# ============================ 青果短效代理池(每轮刷新) ============================

# 提取 API 返回的是无分隔符的 ip:port 串,不能用 \b 锚定(相邻数字间没有边界)
_QG_RE = re.compile(r"\d{1,3}(?:\.\d{1,3}){3}:\d{2,5}")


def _qingguo_config():
    q = CONFIG.get("qingguo") or {}
    if not q.get("enabled"):
        return None
    return q


def _probe_spec(spec, target, timeout=8.0):
    """探测一个代理出口能否正常访问 target(200 才算通)。

    用 curl_cffi chrome 指纹探测 —— 与真实抓取同一传输层,避免用 requests
    指纹导致 Vinted 403 误杀本来能用的代理。每个探测独立开一个短会话。
    用 HEAD 而非 GET:状态码语义一致(200/403/连不上),但首页 GET 约 1.9MB,
    HEAD 只有请求头,150 个探测能省掉约 280MB/轮。
    """
    try:
        with monitor.make_session() as s:
            r = s.request("HEAD", target,
                          proxies={"http": spec, "https": spec}, timeout=timeout)
            return r.status_code == 200
    except Exception:
        return False


def extract_qingguo_pool():
    """调青果提取 API,返回活着的代理列表(带鉴权前缀)。失败返回 None。"""
    q = _qingguo_config()
    if q is None:
        return None
    auth = (q.get("auth") or f"{q.get('key')}:{q.get('pwd')}").strip()
    key = (q.get("key") or "").strip()
    if not key or not auth:
        log.warning("青果池 enabled 但缺 key/auth,跳过刷新")
        return None
    api = q.get("api", "https://overseas.proxy.qg.net/get")
    params = {
        "key": key,
        "num": int(q.get("num", 200)),
        "area": q.get("area", ""),
        "isp": q.get("isp", ""),
        "format": "txt",
        "distinct": str(bool(q.get("distinct", False))).lower(),
        "keep_alive": int(q.get("keep_alive", 300)),
    }
    try:
        r = requests.get(api, params=params, timeout=40)
        r.raise_for_status()
    except Exception as e:
        log.warning("青果提取 API 请求失败: %s", e)
        return None
    addrs = sorted(set(_QG_RE.findall(r.text)))
    if not addrs:
        log.warning("青果提取 API 返回空池")
        return None
    return [f"http://{auth}@{a}" for a in addrs]


def refresh_qingguo_pool():
    """每轮抓取前刷新短效池:提取 → 存活过滤 → 写 proxy_pool.txt → 重建内存池。

    短效代理 IP 存活时间短(keep_alive 秒),静态 proxy_pool.txt 会过期,
    所以每轮开头现提现用。过滤出对 vinted.co.uk 能回 200 的出口,
    写到 proxy_pool.txt(仍然零商品数据,只有 IP 池)并换进 monitor.PROXY_POOL。
    """
    q = _qingguo_config()
    if q is None:
        return
    cand = extract_qingguo_pool()
    if not cand:
        log.warning("青果池刷新:提取为空,保留现有池(共 %d 个)", len(monitor.PROXY_POOL))
        return
    target = f"https://{CONFIG.get('domain', 'www.vinted.co.uk')}"
    timeout = float(q.get("probe_timeout", 8))
    n = min(len(cand), int(q.get("probe_max", 150)))   # 大池只探前 n 个控制耗时
    cand = cand[:n]
    log.info("青果池刷新:提取 %d 个,并行探测前 %d 个", len(cand), n)
    alive = []
    with ThreadPoolExecutor(max_workers=int(q.get("probe_workers", 20))) as ex:
        for spec, ok in ex.map(
                lambda s: (s, _probe_spec(s, target, timeout)), cand):
            if ok:
                alive.append(spec)
    if not alive:
        log.warning("青果池刷新:探测结果全灭,保留现有池")
        return
    # 写回 proxy_pool.txt(覆盖,零商品数据)并换进内存池
    if CONFIG.get("proxy_pool_file"):
        try:
            with open(CONFIG["proxy_pool_file"], "w", encoding="utf-8") as f:
                f.write("\n".join(alive) + "\n")
        except OSError as e:
            log.warning("写 proxy_pool.txt 失败: %s", e)
    monitor.PROXY_POOL = alive
    monitor._pool_index = -1
    global POOL_REFRESHED_AT
    POOL_REFRESHED_AT = time.time()
    log.info("青果池刷新完成:活代理 %d 个(keep_alive=%ss)",
             len(alive), q.get("keep_alive", 300))


# ============================ 静态池校验(自己服务器 IPv6 池,可选) ============================

def verify_static_pool():
    """启动时探测静态池,只保留对 Vinted 返回 200 的出口。

    数据中心 IPv6 在 Cloudflare 那里评分低,常被 JS 挑战(403)。把当前能过
    挑战的出口过滤出来,每轮抓取时少踩坑。pool_verify.enabled=false 跳过。
    注意:挑战是随机的,探测通过的出口后续也可能再被挑战,所以这只是优化,
    真正兜底靠 _get 的"403 自动换出口重试"。全灭时保留原池(避免空池)。
    """
    pv = CONFIG.get("pool_verify") or {}
    if not pv.get("enabled"):
        return
    pool = list(monitor.PROXY_POOL)
    if not pool:
        return
    target = f"https://{CONFIG.get('domain', 'www.vinted.co.uk')}"
    timeout = float(pv.get("timeout", 8))
    threads = int(pv.get("threads", 20))
    log.info("池校验:探测 %d 个出口(只保留对 Vinted 返回 200 的)...", len(pool))
    alive = []
    with ThreadPoolExecutor(max_workers=threads) as ex:
        for spec, ok in ex.map(lambda s: (s, _probe_spec(s, target, timeout)), pool):
            if ok:
                alive.append(spec)
    if not alive:
        log.warning("池校验:全部被挑战,保留原池(抓取时靠 _get 自动轮换)")
        return
    log.info("池校验:保留 %d/%d 个出口", len(alive), len(pool))
    monitor.PROXY_POOL = alive
    monitor._pool_index = -1
    try:
        with open(CONFIG["proxy_pool_file"], "w", encoding="utf-8") as f:
            f.write("\n".join(alive) + "\n")
    except OSError as e:
        log.warning("写池文件失败: %s", e)


# 静态池模式(青果关闭)下启动时校验一次;青果池每轮现提现用,不需要
if not (CONFIG.get("qingguo") or {}).get("enabled"):
    verify_static_pool()


# ============================ 代理轮换 ============================

_tls = threading.local()
_pool_lock = threading.Lock()


def _next_spec_locked():
    """线程安全地取下一个池条目(全局游标 + 锁)。"""
    with _pool_lock:
        monitor._pool_index += 1
        idx = monitor._pool_index % len(monitor.PROXY_POOL)
        return monitor._normalize_spec(monitor.PROXY_POOL[idx])


def _patched_current_proxies():
    """并行模式的 current_proxies:返回本线程已锁定的出口,锁定前用默认代理。"""
    forced = getattr(_tls, "forced", None)
    if forced is not None:
        return forced
    return monitor.DEFAULT_PROXIES


def _patched_next_proxies():
    """并行模式的 next_proxies:锁定一个新出口给本线程,同时全局游标前进。"""
    if monitor.PROXY_POOL:
        forced = _next_spec_locked()
        _tls.forced = forced
        return forced
    return monitor.DEFAULT_PROXIES


def _rotate_for_account():
    """逐账号换出口。workers>1 时给本线程锁定新出口;单线程时走全局轮换。"""
    if int(CONFIG.get("workers", 1)) > 1:
        if monitor.PROXY_POOL:
            _tls.forced = _next_spec_locked()
    else:
        monitor.next_proxies()


if int(CONFIG.get("workers", 1)) > 1:
    monitor.current_proxies = _patched_current_proxies
    monitor.next_proxies = _patched_next_proxies
    log.info("已启用并行抓取 workers=%d(thread-local 锁定出口)", CONFIG.get("workers"))

# ============================ 会话(每线程一个,Session 非线程安全) ============================

_TOKEN_FAILED = threading.Event()   # 本轮 token 确认失败 → 其余账号快速失败,不再逐账号重试
ROUND_DONE = 0   # 本轮已抓取成功的账号数(立即刷新判断:>0 才打断旧轮;0=还在提池,轮子自己够新)
POOL_REFRESHED_AT = 0.0   # 池子最近一次成功刷新时间戳(账号添加等零散操作判断池子是否过期)

def thread_session():
    s = getattr(_tls, "session", None)
    if s is None:
        if _TOKEN_FAILED.is_set():
            raise RuntimeError("IP 池全被 Cloudflare 挑战,本轮无法获取访问令牌")
        s = monitor.make_session()
        try:
            monitor.ensure_token(s)
        except Exception:
            _TOKEN_FAILED.set()   # 记录本轮 token 不可用,避免 30 个账号各空转一遍
            raise
        _tls.session = s
    return s


# ============================ 持久化(SQLite,数据保存 + 定期清理) ============================

class Cache:
    """轻量运行时状态:全量抓取进度 + 账号错误。商品数据本体在 SQLite。"""
    def __init__(self):
        self.lock = threading.Lock()
        self.updated_at = 0        # 最近一轮全量抓取完成时间(Unix 秒)
        self.refreshing = False    # 全量抓取是否正在跑
        self.last_duration = 0.0   # 最近一轮耗时(秒)
        self.errors = {}           # account -> 最近错误信息
        self.user_ids = {}         # account -> user_id(解析结果内存缓存)


CACHE = Cache()


_ITEM_COLS = ["item_id", "account", "title", "price", "currency", "url",
              "created_at_ts", "favourite_count", "view_count", "status",
              "first_seen_at", "last_seen_at"]


def _row_to_dict(row):
    d = dict(zip(_ITEM_COLS, row))
    d["status_zh"] = monitor.STATUS_ZH.get(d["status"], d["status"])
    return d


class Store:
    """SQLite 存取:全量 upsert + 售出标记 + 按保留期清理。所有访问加锁,支持多线程。"""

    def __init__(self, path):
        self.con = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.Lock()
        self._ensure_schema()

    def _ensure_schema(self):
        with self.lock:
            c = self.con
            c.execute("""CREATE TABLE IF NOT EXISTS items(
                item_id      INTEGER PRIMARY KEY,
                account      TEXT,
                title        TEXT,
                price        REAL,
                currency     TEXT,
                url          TEXT,
                created_at_ts INTEGER,
                favourite_count INTEGER,
                view_count    INTEGER,
                status       TEXT,
                first_seen_at INTEGER,
                last_seen_at  INTEGER
            )""")
            c.execute("""CREATE TABLE IF NOT EXISTS monitor_targets(
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                account        TEXT NOT NULL UNIQUE,
                user_id        INTEGER,
                added_at       INTEGER,
                last_checked_at INTEGER
            )""")
            c.execute("""CREATE TABLE IF NOT EXISTS snapshots(
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                item_id        INTEGER,
                ts             INTEGER,
                favourite_count INTEGER,
                view_count     INTEGER,
                price          REAL,
                status         TEXT
            )""")
            # 用户手动删除的商品:记在这里,下轮抓取跳过,避免"删了又被抓回来"
            c.execute("""CREATE TABLE IF NOT EXISTS deleted_items(
                item_id INTEGER PRIMARY KEY
            )""")
            c.commit()

    def upsert_account_rows(self, account, uid, rows, now):
        """写入单个账号的全量商品:新商品 INSERT,旧商品更新,已消失的标已售。

        用户手动删除的商品(记在 deleted_items)直接跳过,不会重新入库。
        """
        with self.lock:
            c = self.con
            deleted = {r[0] for r in c.execute("SELECT item_id FROM deleted_items")}
            rows = [r for r in rows if r["item_id"] not in deleted]
            c.execute("INSERT OR IGNORE INTO monitor_targets(account, user_id, added_at) VALUES(?,?,?)",
                      (account, uid, now))
            c.execute("UPDATE monitor_targets SET last_checked_at=? WHERE account=?", (now, account))
            cur_ids = set()
            for r in rows:
                cur_ids.add(r["item_id"])
                c.execute("""INSERT INTO items
                    (item_id, account, title, price, currency, url, created_at_ts,
                     favourite_count, view_count, status, first_seen_at, last_seen_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(item_id) DO UPDATE SET
                        title=excluded.title, price=excluded.price, currency=excluded.currency,
                        url=excluded.url,
                        created_at_ts=COALESCE(items.created_at_ts, excluded.created_at_ts),
                        favourite_count=excluded.favourite_count, view_count=excluded.view_count,
                        status=excluded.status, last_seen_at=excluded.last_seen_at""",
                    (r["item_id"], account, r["title"], r["price"], r["currency"], r["url"],
                     r["created_at_ts"], r["favourite_count"], r["view_count"], r["status"],
                     now, now))
            # 这个账号之前见过、这轮没再出现 → 标已售(不删,保留记录,靠清理期淘汰)
            prev_ids = {row[0] for row in
                        c.execute("SELECT item_id FROM items WHERE account=?", (account,))}
            for gone in prev_ids - cur_ids:
                c.execute("UPDATE items SET status='sold', last_seen_at=? WHERE item_id=?",
                          (now, gone))
            # 每件商品记一条快照,用来推算 fav_delta(上次的赞数)
            for r in rows:
                c.execute("INSERT INTO snapshots(item_id, ts, favourite_count, view_count, price, status)"
                          " VALUES(?,?,?,?,?,?)",
                          (r["item_id"], now, r["favourite_count"], r["view_count"],
                           r["price"], r["status"]))
            c.commit()

    def prev_favs(self, item_ids):
        """批量查每件商品上一次抓到的赞数(取最近第二条快照);没有历史返回 None。

        用窗口函数一次查出所有需要的行,避免逐条查询。
        """
        if not item_ids:
            return {}
        out = {}
        with self.lock:
            # 按 item_id 分区按 ts/id 倒序,第二行即上一次的值
            sql = """
                SELECT item_id, favourite_count FROM (
                    SELECT item_id, favourite_count,
                           ROW_NUMBER() OVER (PARTITION BY item_id ORDER BY ts DESC, id DESC) rn
                    FROM snapshots
                    WHERE item_id IN (%s)
                ) WHERE rn = 2
            """ % ",".join("?" * len(item_ids))
            for iid, fav in self.con.execute(sql, list(item_ids)).fetchall():
                out[iid] = fav
        return out

    def remove_account(self, account):
        """从库中彻底删掉一个账号的全部商品记录(配合账号清单删除)。"""
        with self.lock:
            c = self.con
            c.execute("DELETE FROM items WHERE account=?", (account,))
            c.execute("DELETE FROM monitor_targets WHERE account=?", (account,))
            c.commit()

    def delete_item(self, item_id):
        """永久删除一件商品:记入 deleted_items(防止下轮被抓回来)+ 清掉 items/snapshots。

        返回是否真的删除了 items 里的行(已不存在则 False)。
        """
        with self.lock:
            c = self.con
            c.execute("INSERT OR IGNORE INTO deleted_items(item_id) VALUES(?)", (item_id,))
            c.execute("DELETE FROM snapshots WHERE item_id=?", (item_id,))
            cur = c.execute("DELETE FROM items WHERE item_id=?", (item_id,))
            c.commit()
            return cur.rowcount > 0

    def delete_sold(self):
        """删除全部已售商品(记入 deleted_items 防止下轮回灌),返回删除条数。"""
        with self.lock:
            c = self.con
            ids = [r[0] for r in
                   c.execute("SELECT item_id FROM items WHERE status='sold'")]
            for iid in ids:
                c.execute("INSERT OR IGNORE INTO deleted_items(item_id) VALUES(?)", (iid,))
                c.execute("DELETE FROM snapshots WHERE item_id=?", (iid,))
                c.execute("DELETE FROM items WHERE item_id=?", (iid,))
            c.commit()
            return len(ids)

    def cleanup(self, now, retention_days):
        """清理超过保留期没再出现的商品(含已售)及其快照。retention_days<=0 不清理。"""
        if not retention_days:
            return
        cutoff = int(now) - int(retention_days) * 86400
        with self.lock:
            c = self.con
            c.execute("DELETE FROM snapshots WHERE item_id IN "
                      "(SELECT item_id FROM items WHERE last_seen_at < ?)", (cutoff,))
            c.execute("DELETE FROM items WHERE last_seen_at < ?", (cutoff,))
            c.commit()

    def all_rows(self, account=None):
        """全部账号的全部商品(可选按账号过滤),新的/刚更新的在前。"""
        with self.lock:
            c = self.con
            if account:
                rows = c.execute(
                    "SELECT " + ",".join(_ITEM_COLS) +
                    " FROM items WHERE account=? ORDER BY last_seen_at DESC", (account,)).fetchall()
            else:
                rows = c.execute(
                    "SELECT " + ",".join(_ITEM_COLS) +
                    " FROM items ORDER BY last_seen_at DESC").fetchall()
        return [_row_to_dict(r) for r in rows]

    def count(self):
        with self.lock:
            return self.con.execute("SELECT COUNT(*) FROM items").fetchone()[0]

    def count_by_account(self):
        with self.lock:
            return dict(self.con.execute(
                "SELECT account, COUNT(*) FROM items GROUP BY account").fetchall())

    def last_checked(self, account=None):
        """账号最近检查时间。account=None 返回全表映射。"""
        with self.lock:
            if account:
                row = self.con.execute("SELECT last_checked_at FROM monitor_targets WHERE account=?",
                                       (account,)).fetchone()
                return row[0] if row else None
            return dict(self.con.execute(
                "SELECT account, last_checked_at FROM monitor_targets").fetchall())


STORE = Store(monitor.DB_PATH)


def _purge_after_round(now):
    """每轮全量抓取结束后清理过期数据(保留期来自配置 retention_days)。"""
    days = int(CONFIG.get("retention_days", 7) or 0)
    try:
        STORE.cleanup(now, days)
    except Exception as e:
        log.warning("定期清理失败: %s", e)

# ============================ 抓取 ============================

def _needs_resolve(account):
    """数字ID / /member/ 链接 → 直接解析,零网络;用户名 → 需要查询接口。"""
    account = (account or "").strip().strip('"\'')
    if account.isdigit():
        return False
    if re.search(r"/member/\d+", account):
        return False
    return True


def _ensure_fresh_pool(force=False):
    """账号添加/手动等零散操作前调用:池子过期(>60s)或为空就重提一次。

    短效池 IP ~2.5 分钟就死,而池子只在每轮抓取开始时刷新。手动模式下
    隔一段时间后池子全是死代理,零散操作(如添加账号查用户名)会 407 失败。
    force=True:无条件重提(重试时换一批新出口 IP)。
    """
    if _qingguo_config() is None:
        return
    global POOL_REFRESHED_AT
    age = time.time() - POOL_REFRESHED_AT
    if not force and monitor.PROXY_POOL and age < 60:
        return   # 池子还新鲜
    log.info("账号操作前%s重提池(池龄 %.0fs)", "强制" if force else "", age)
    refresh_qingguo_pool()


def _resolve_with_retry(session, account, attempts=3):
    """解析用户名:代理/网络类失败自动换池重试,直到成功或撞上限。

    用户名不存在(404)等是确定性结论,直接抛错不浪费重试;
    407/403/连接失败多是池子/出口问题,每次重试前换一批新池提高成功率。
    """
    last = None
    for i in range(1, attempts + 1):
        try:
            monitor.ensure_token(session)   # 有缓存 token 时零网络
            return monitor.resolve_user_id(session, account)
        except Exception as e:
            msg = str(e)
            if "用户名不存在" in msg or "响应无用户ID" in msg:
                raise   # 确定性失败,重试无用
            last = e
        log.warning("账号 %s 添加第 %d/%d 次失败(%s),换池重试",
                    account, i, attempts, str(last)[:100])
        if i < attempts:
            _ensure_fresh_pool(force=True)   # 换一批新出口 IP 再试
    raise RuntimeError(f"添加账号重试 {attempts} 次仍失败: {last}")


def get_uid(account, deadline=None):
    """解析 user_id,结果内存缓存;用户名首次解析走一次网络查询。"""
    account = (account or "").strip()
    if not account:
        raise ValueError("账号为空")
    with CACHE.lock:
        uid = CACHE.user_ids.get(account)
    if uid:
        return uid
    session = thread_session() if _needs_resolve(account) else None
    uid = monitor.resolve_user_id(session, account, deadline=deadline)
    with CACHE.lock:
        CACHE.user_ids[account] = uid
    return uid


def fetch_account_rows(session, uid, deadline=None):
    """拉一个账号的在售商品并规整成展示行。任何字段缺失都安全降级。"""
    items = monitor.fetch_items(session, uid, deadline=deadline)
    rows = []
    for it in items:
        try:
            n = monitor.normalize_item(it)
        except Exception:
            continue  # 单条脏数据跳过,不拖垮整轮
        rows.append({
            "item_id": n["item_id"],
            "title": n["title"],
            "price": n["price"],
            "currency": n["currency"],
            "url": n["url"],
            "created_at_ts": n["created_at_ts"],
            "favourite_count": n["favourite_count"],
            "view_count": n["view_count"],
            "status": n["status"],
            "status_zh": monitor.STATUS_ZH.get(n["status"], n["status"]),
        })
    return rows


def _between_accounts_delay():
    lo, hi = CONFIG.get("between_accounts_delay", [0.2, 0.5])
    time.sleep(random.uniform(float(lo), float(hi)))


def fetch_one(account, deadline=None):
    """抓单个账号 → 写 SQLite。成功/失败都返回结果字典,不抛异常。"""
    account = (account or "").strip()
    if not account:
        return {"account": account, "ok": False, "error": "空账号"}
    try:
        uid = get_uid(account, deadline=deadline)
        if not uid:
            return {"account": account, "ok": False, "error": "无法解析账号 ID"}
        _rotate_for_account()          # 逐账号换出口
        session = thread_session()
        rows = fetch_account_rows(session, uid, deadline=deadline)
        now = int(time.time())
        STORE.upsert_account_rows(account, uid, rows, now)
        with CACHE.lock:
            CACHE.errors.pop(account, None)
        return {"account": account, "ok": True, "error": None, "count": len(rows)}
    except Exception as e:
        log.warning("账号 %s 抓取失败: %s", account, e)
        with CACHE.lock:
            CACHE.errors[account] = str(e)
        return {"account": account, "ok": False, "error": str(e)}


def _round_deadline(start):
    """单轮抓取截止时间:防止池子异常时一轮卡几十分钟。默认=抓取间隔,下限 240s。"""
    seconds = int(CONFIG.get("interval_minutes", 10)) * 60
    seconds = max(240, seconds)
    if CONFIG.get("round_timeout"):
        seconds = max(240, int(CONFIG.get("round_timeout")))
    return start + seconds


def _run_accounts(accounts):
    """逐账号抓(串行)或 workers 并行。单账号失败只记 error,不拖垮整轮。"""
    workers = int(CONFIG.get("workers", 1))
    started = time.time()
    deadline = _round_deadline(started)
    if workers <= 1 or len(accounts) <= 1:
        results = []
        consecutive_407 = 0
        last_refresh = started   # 本轮池子提取时刻(短效池 IP ~2.5 分钟就过期)
        for acc in accounts:
            if monitor.CANCEL_REQUESTED:   # "立即刷新"打断:中止本轮,交给调度线程重抓
                log.info("立即刷新打断本轮,中止抓取(已抓 %d/%d)", len(results), len(accounts))
                break
            if time.time() > deadline:
                log.warning("本轮超过 %ds 截止,剩余 %d 个账号跳过(池子可能异常)",
                            int(deadline - started), len(accounts) - len(results))
                break
            # 主动提前重提:池子用到 100s 就换新(提取 ~15s,新池 115s 就绪,
            # 旧池 150s 才死,永远赶在过期前,避免账号撞上过期窗口 407)。
            if time.time() - last_refresh > 100:
                log.warning("池子已用 %ds(短效池 IP ~2.5 分钟过期),提前重提",
                            int(time.time() - last_refresh))
                refresh_qingguo_pool()
                last_refresh = time.time()
            res = fetch_one(acc, deadline=deadline)
            results.append(res)
            global ROUND_DONE
            ROUND_DONE += 1
            # 兜底:连续 2 个账号 407 说明池子已经过期,立即重提。
            if res.get("error") and "407" in str(res.get("error")):
                consecutive_407 += 1
                if consecutive_407 >= 2:
                    log.warning("连续 %d 个账号 407(短效池 IP 过期),重新提取代理池",
                                consecutive_407)
                    refresh_qingguo_pool()
                    last_refresh = time.time()
                    consecutive_407 = 0
            else:
                consecutive_407 = 0
            _between_accounts_delay()
        return results
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(fetch_one, acc, deadline) for acc in accounts]
        return [f.result() for f in futs]


def fetch_all():
    """全量抓取:读账号清单 → 逐账号换 IP 抓取 → 写 SQLite → 清理过期数据。"""
    if _qingguo_config() is not None:
        refresh_qingguo_pool()   # 短效池每轮现提现用,IP 存活时间短
    accounts = monitor.read_account_file(CONFIG["accounts_file"])
    accounts = list(dict.fromkeys(accounts))   # 去重:防重复添加让同一账号被抓多次拖慢整轮
    _TOKEN_FAILED.clear()          # 新一轮:重新允许尝试拿 token
    global ROUND_DONE
    ROUND_DONE = 0
    with CACHE.lock:
        CACHE.refreshing = True
    started = time.time()
    try:
        results = _run_accounts(accounts)
    finally:
        with CACHE.lock:
            CACHE.refreshing = False
            CACHE.updated_at = int(time.time())
            CACHE.last_duration = time.time() - started
    ok = sum(1 for r in results if r["ok"])
    _purge_after_round(int(time.time()))
    log.info("全量抓取完成:成功 %d/%d (%.1fs)", ok, len(results), time.time() - started)

# ============================ 调度线程 ============================

def _in_schedule_window(now=None):
    """是否在调度时段窗口内(服务器本地时间)。没配 schedule 则 24 小时都跑。"""
    sched = CONFIG.get("schedule") or {}
    start_s, end_s = sched.get("start"), sched.get("end")
    if not start_s or not end_s:
        return True
    try:
        h, m = map(int, start_s.split(":"))
        start_min = h * 60 + m
        h, m = map(int, end_s.split(":"))
        end_min = h * 60 + m
    except Exception:
        return True
    now = now or time.localtime()
    cur = now.tm_hour * 60 + now.tm_min
    if start_min == end_min:
        return True
    if start_min < end_min:
        return start_min <= cur < end_min
    return cur >= start_min or cur < end_min   # 跨天(如 22:00-06:00)


class Scheduler(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True, name="vinted-scheduler")
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._running = False
        self._pending = False   # 手动刷新排队标志:上一轮跑完立刻接着抓,点击不丢
        self._inside_logged = False   # 窗口外只打一次日志,别每分钟刷屏
        self._manual_override = False   # 手动 /api/refresh 不受时段窗口限制(用户显式触发)
        self.last_start = 0.0   # 0.0 = 尚未跑过 → 启动后先快速抓一轮填缓存

    def run(self):
        mode = CONFIG.get("mode", "auto")
        if mode == "manual":
            # 手动模式:只在 /api/refresh 触发时抓,无自动轮询、无启动预抓、无窗口
            log.info("调度线程:手动模式,只在 /api/refresh 触发时抓取")
            while True:
                self._event.wait()   # 阻塞等手动触发(0 流量空闲)
                self._event.clear()
                self._run_rounds()
            return

        interval = max(60, int(CONFIG.get("interval_minutes", 10)) * 60)
        sched_cfg = CONFIG.get("schedule") or {}
        log.info("调度线程:自动模式,每 %d 秒全量抓一轮(时段 %s-%s)",
                 interval, sched_cfg.get("start"), sched_cfg.get("end"))
        while True:
            # 时段窗口:窗口外完全停跑、零流量,每隔 60s 醒来查一次是否到点。
            # 手动 /api/refresh(用户显式触发)不受窗口限制。
            if not _in_schedule_window() and not self._manual_override:
                sched = CONFIG.get("schedule") or {}
                if not self._inside_logged:
                    log.info("当前不在调度时段(%s-%s)内,暂停抓取,等窗口开启",
                             sched.get("start"), sched.get("end"))
                    self._inside_logged = True
                self._event.wait(timeout=60)
                self._event.clear()
                continue
            self._inside_logged = False
            self._manual_override = False
            if self.last_start:
                self._event.wait(timeout=max(0.0, self.last_start + interval - time.time()))
                self._event.clear()
            else:
                self._event.wait(timeout=5)   # 启动约 5 秒后先抓一轮,尽快填缓存
                self._event.clear()
            self._run_rounds(force=True)   # 自动模式:到点就抓一轮

    def _run_rounds(self, force=False):
        """抓一轮;期间来了"立即刷新"则中止旧轮、从当前时刻重抓(点击永不丢弃)。
        force=True(自动模式):即使没排队也抓一轮;force=False(手动模式):只在有排队时抓。
        """
        while True:
            with self._lock:
                if self._running:
                    # 新"立即刷新"到达:旧轮已抓到账号才中止重抓;
                    # 还在提池/刚开局(0个)则它本身就是当前时刻的刷新,等它跑完不打断。
                    if ROUND_DONE > 0:
                        monitor.CANCEL_REQUESTED = True
                    time.sleep(0.2)
                    continue
                if not force and not self._pending:
                    return          # 手动模式空唤醒(排队轮已消化信号),不空跑
                force = False
                self._pending = False
                monitor.CANCEL_REQUESTED = False   # 新一轮前清掉打断标记
                self._running = True
            try:
                self.last_start = time.time()
                fetch_all()
            finally:
                with self._lock:
                    self._running = False

    def request_refresh(self):
        """立即刷新:从点击当前时刻重抓一轮。旧轮在跑且已抓到账号则中止它马上重抓;
        旧轮还在提池/刚开局(0个账号)则它本身就是当前时刻的刷新,本次点击由它覆盖。"""
        with self._lock:
            if self._running and ROUND_DONE == 0:
                return True      # 在跑的那轮够新,本次点击由它覆盖,不打断也不排队
        monitor.CANCEL_REQUESTED = True   # 正在跑的旧轮(有已抓账号)尽快中止
        self._pending = True
        self._manual_override = True   # 手动触发不受时段窗口限制
        self._event.set()
        return True


SCHEDULER = Scheduler()

# ============================ REST API ============================

@asynccontextmanager
async def lifespan(app):
    SCHEDULER.start()
    yield  # daemon 线程随进程退出,无需清理


app = FastAPI(title="Vinted Monitor Server", version="1.0.0", lifespan=lifespan)


def _api_error(status, code, message):
    return HTTPException(status_code=status,
                         detail={"error": {"code": code, "message": message}})


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/api/status")
def api_status():
    with CACHE.lock:
        refreshing = CACHE.refreshing
        updated_at = CACHE.updated_at
        last_duration = CACHE.last_duration
        error_count = len(CACHE.errors)
    accounts = monitor.read_account_file(CONFIG["accounts_file"])
    pool_size = len(monitor.PROXY_POOL)
    mode = ("residential" if pool_size else
            ("default" if CONFIG.get("default_proxy") else "none"))
    return {
        "ok": True,
        "version": app.version,
        "server_time": int(time.time()),
        "cache": {"updated_at": updated_at, "refreshing": refreshing,
                  "last_duration": round(last_duration, 1)},
        "accounts": {"total": len(accounts), "error": error_count},
        "pool": {"size": pool_size, "mode": mode},
        "items": {"total": STORE.count()},
    }


@app.get("/api/accounts")
def api_accounts():
    with CACHE.lock:
        errors_map = dict(CACHE.errors)
        uids_map = dict(CACHE.user_ids)
    last_checked = STORE.last_checked() or {}
    counts = STORE.count_by_account()
    out = []
    for acc in monitor.read_account_file(CONFIG["accounts_file"]):
        out.append({
            "account": acc,
            "user_id": uids_map.get(acc),
            "last_checked_at": last_checked.get(acc),
            "item_count": counts.get(acc, 0),
            "error": errors_map.get(acc),
        })
    return {"ok": True, "accounts": out}


class AddAccountsBody(BaseModel):
    accounts: list[str] | None = None
    account: str | None = None


@app.post("/api/accounts")
def api_add_accounts(body: AddAccountsBody):
    raw = body.accounts if body.accounts else ([body.account] if body.account else [])
    raw = [a.strip().strip('"\'') for a in raw if a and a.strip()]
    if not raw:
        raise _api_error(400, "EMPTY", "没有要添加的账号(body 需要 account 或 accounts 字段)")
    existing = set(monitor.read_account_file(CONFIG["accounts_file"]))
    # 有用户名需要网络查询时,先确保池子新鲜(手动模式下隔久了池子过期→查用户名全 407)
    if any(_needs_resolve(a) for a in raw):
        _ensure_fresh_pool()
    session = None
    results = []
    for a in raw:
        if a in existing:
            with CACHE.lock:
                uid = CACHE.user_ids.get(a)
            results.append({"account": a, "ok": True, "user_id": uid,
                            "added": False, "error": None})
            continue
        try:
            if _needs_resolve(a):
                # 用户名:串行 + 随机限速,降低被查询接口风控的概率
                if session is None:
                    session = monitor.make_session()
                uid = _resolve_with_retry(session, a)   # 失败自动换池重试,直到成功
                time.sleep(random.uniform(0.3, 0.6))
            else:
                uid = monitor.resolve_user_id(None, a)   # 数字ID/链接,零网络
        except Exception as e:
            results.append({"account": a, "ok": False, "user_id": None,
                            "added": False, "error": str(e)})
            continue
        with open(CONFIG["accounts_file"], "a", encoding="utf-8") as f:
            f.write(a + "\n")
        with CACHE.lock:
            CACHE.user_ids[a] = uid
        results.append({"account": a, "ok": True, "user_id": uid,
                        "added": True, "error": None})
    added = sum(1 for r in results if r.get("added"))
    failed = sum(1 for r in results if not r.get("ok"))
    skipped = len(results) - added - failed
    return {"ok": True, "added": added, "failed": failed, "skipped": skipped,
            "results": results}


def _remove_account(account):
    """从 accounts.txt 移除该行(整文件重写)。返回移除条数(0=不存在)。"""
    try:
        with open(CONFIG["accounts_file"], "r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return 0
    kept = [ln for ln in lines if ln.strip() != account]
    if len(kept) == len(lines):
        return 0
    with open(CONFIG["accounts_file"], "w", encoding="utf-8") as f:
        f.writelines(kept)
    return 1


@app.delete("/api/accounts/{account}")
def api_delete_account(account: str):
    removed = _remove_account(account)
    if removed == 0:
        raise _api_error(404, "NOT_FOUND", f"账号不在监控清单里: {account}")
    STORE.remove_account(account)
    with CACHE.lock:
        CACHE.errors.pop(account, None)
        CACHE.user_ids.pop(account, None)
    return {"ok": True, "removed": 1}


@app.get("/api/items")
def api_items(account: str | None = None):
    """全部账号的全部商品合并成一个列表(account 可选过滤)。新上/刚更新的在前。"""
    account = (account or "").strip()
    items = STORE.all_rows(account or None)
    now = int(time.time())
    # 批量取每件商品上次的赞数,推算 fav_delta 供客户端展示增量
    prev_favs = STORE.prev_favs([it["item_id"] for it in items])
    for it in items:
        prev = prev_favs.get(it["item_id"])
        it["fav_delta"] = (it["favourite_count"] - prev) if prev is not None else None
    return {"ok": True, "account": account or None, "fetched_at": now,
            "items": items, "total": len(items)}


@app.delete("/api/items/{item_id}")
def api_delete_item(item_id: int):
    """永久删除一件商品(如已售的),删除后不再自动出现。"""
    if not STORE.delete_item(item_id):
        raise _api_error(404, "NOT_FOUND", f"商品不存在: {item_id}")
    return {"ok": True, "deleted": item_id}


@app.post("/api/items/delete-sold")
def api_delete_sold():
    """一键删除全部已售商品,返回删除条数。"""
    n = STORE.delete_sold()
    return {"ok": True, "deleted": n}


@app.post("/api/refresh", status_code=202)
def api_refresh():
    # 永远接受:上一轮在跑就排队,结束立即接上一轮,点击不丢弃
    SCHEDULER.request_refresh()
    return {"ok": True, "started": True}


def main():
    import uvicorn
    uvicorn.run(app,
                host=CONFIG.get("host", "0.0.0.0"),
                port=int(CONFIG.get("port", 8000)),
                workers=1)   # 固定单 worker,防 Scheduler 双跑


if __name__ == "__main__":
    main()
