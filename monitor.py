"""
Vinted 自营商品监控器
监控你自己 Vinted 店铺的在售商品:上架时间 / 是否售出 / 点赞量增长 / 降价。

原理(2026-08 实测,匿名即可,不需要登录 cookie):
  Vinted 公开接口  api/v2/wardrobe/{user_id}/items  返回你的在售商品,含:
    favourite_count    点赞数(真实)
    is_closed          是否已售出/下架
    is_reserved        是否被预留
    photos[0].high_resolution.timestamp  照片上传时间戳(≈上架时间)
    price              价格
  浏览量(view_count)匿名恒为 0(只有卖家登录可见),填了登录 cookie 才能看。

用法:
  python monitor.py --once              # 手动跑一次检查(测试用)
  python monitor.py                     # 按 CONFIG 里的间隔循环监控
  # CONFIG["profile_url"] 支持主页链接 / 数字ID / 用户名(如 "erikd336")

批量监听多个账号:
  python monitor.py --add-account erikd336   # 导入一个账号(追加进 accounts.txt,带首次导入时间)
  python monitor.py --accounts accounts.txt  # 按账号清单批量监控(每行一个账号,# 为注释)
  # 账号 = 主页链接 / 数字ID / 用户名。
  # 首次导入时间: 账号记在 monitor_targets.added_at,商品记在 items.first_seen_at(不覆盖)。

依赖: pip install -r requirements.txt
"""
import argparse
import datetime
import random
import re
import sqlite3
import sys
import time
from pathlib import Path

try:  # Windows 控制台可能 GBK,强制 UTF-8 避免中文输出报错/乱码
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

# 传输层:优先用 curl_cffi 伪装 Chrome TLS 指纹,抗 Cloudflare 识别
try:
    from curl_cffi import requests as http
    TRANSPORT = "curl_cffi (chrome impersonate)"
except ImportError:
    import requests as http
    TRANSPORT = "requests (可 pip install curl_cffi 提升抗封能力)"

# 机场节点轮换:池文件里写 `clash:<节点名>` 即通过本地 Clash 切节点换出口 IP。
# 见 clash_pool.txt 和 clash_pool.py 的说明。
import clash_pool  # noqa: E402

# ---- IP 池状态 ----
DEFAULT_PROXIES = None        # 无池时用的默认代理(来自 CONFIG["proxy"])
PROXY_POOL = []               # IP 池:一列代理,每轮检查换一个;空 = 不用池
_pool_index = -1              # 池游标(首次取第 0 个)

# ============================ 配置区 ============================
CONFIG = {
    # 你的 Vinted 主页链接,形如 https://www.vinted.co.uk/member/12345678-你的用户名;
    # 也可直接填用户名(自动解析成 ID),如 "erikd336"
    "profile_url": "https://www.vinted.co.uk/member/3176597493",

    # 批量监听(可选):一列账号,每个元素 = 主页链接 / 数字ID / 用户名。
    # 优先级: --accounts 文件 > 下面 accounts_file 指定的文件 > 这里的列表 > profile_url(单账号)。
    # 新账号第一次导入时间记在 monitor_targets.added_at;
    # 每个商品第一次出现时间记在 items.first_seen_at(首导时间,之后不覆盖)。
    "accounts": [],
    "accounts_file": "",  # 账号清单文件名(相对本脚本目录),如 "accounts.txt";每行一个账号,# 为注释

    # 登录态 cookie(可选,只为解锁"浏览量")。
    #   浏览器登录 vinted.co.uk → F12 → Application → Cookies →
    #   https://www.vinted.co.uk → 复制 access_token_web 的值。
    #   留空 = 匿名模式:点赞/售出/上架时间都能拿,浏览量显示 0。
    "access_token_web": "",

    # 国家站点域名(必须带 www,cookie 按 www 域存)
    "domain": "www.vinted.co.uk",

    # HTTP 代理(国内网络直连 Vinted 会被 SNI 阻断,必须走代理)。
    # 留空 = 不代理。Clash 默认 7890;V2RayN 默认 10809。
    "proxy": "http://127.0.0.1:7890",

    # IP 池(可选):一列代理节点,每次检查轮换一个出口,请求失败自动切下一个。
    #   元素可为 http/socks5 代理 URL 或 "direct"(=直连)。留空 = 只用上面 proxy。
    #   也可在运行时用 --proxy-pool proxy_pool.txt 从文件加载(每行一个,# 为注释)。
    "proxy_pool": [],

    # 轮询间隔(分钟)
    "interval_minutes": 10,

    # 数据库文件(相对本脚本所在目录)
    "db_path": "vinted_monitor.db",

    # 两次分页请求之间的随机延迟秒数(防风控)
    "page_delay": (1.5, 3.5),
}
# ================================================================

DB_PATH = str(Path(__file__).resolve().parent / CONFIG["db_path"])
ACCOUNTS_FILE = (str(Path(__file__).resolve().parent / CONFIG["accounts_file"])
                 if CONFIG.get("accounts_file") else None)


# ==================== 鲁棒性工具(页面改版 / 字段缺失 / 格式异常时不崩溃) ====================

def _dict_of(v):
    """安全取字典:字段被改版成非 dict 时返回 {},避免 .get 直接崩。"""
    return v if isinstance(v, dict) else {}


def _list_of(v):
    """安全取列表:字段变成非 list 时返回 [],避免遍历报错。"""
    return v if isinstance(v, list) else []


def _to_int(v, default=0):
    """安全转 int:兼容 None / '1.5k' / '1,234' / 浮点字符串 等异常格式,失败给默认值。"""
    if v is None:
        return default
    if isinstance(v, bool):
        return int(v)
    try:
        return int(float(v))
    except (ValueError, TypeError):
        return default


def _to_float(v, default=0.0):
    """安全转 float:格式异常时给默认值而不是抛异常。"""
    if v is None:
        return default
    try:
        return float(v)
    except (ValueError, TypeError):
        return default


def _to_str(v, default=""):
    """安全转 str:None 给空串,避免后续切片 / 格式化报错。"""
    return default if v is None else str(v)


def _parse_json(resp, url):
    """解析 JSON 响应。页面改版或触发风控拦截返回 HTML 时,给明确中文报错而非抛难懂的异常。"""
    try:
        data = resp.json()
    except Exception as e:
        ct = resp.headers.get("content-type") or "未知"
        snippet = (resp.text or "")[:160].replace("\n", " ")
        raise RuntimeError(
            f"接口返回的不是 JSON(可能 Vinted 页面改版或触发风控拦截)。\n"
            f"  URL: {url}\n  Content-Type: {ct}\n  内容开头: {snippet!r}") from e
    if not isinstance(data, dict):
        raise RuntimeError(
            f"接口返回结构异常:期望 JSON 对象,实际是 {type(data).__name__}(可能页面改版)。\n"
            f"  URL: {url}")
    return data
# =======================================================================================

# ==================== IP 池(轮换出口 / 失败自动切换) ====================

def _normalize_spec(spec):
    """把池里的一条配置归一成 requests 的 proxies 字典;direct/none/空 → 直连。

    `clash:<节点名>` 条目 = 通过本地 Clash 把出口切到该机场节点,再走本地端口。
    """
    spec = (spec or "").strip()
    if not spec or spec.lower() in ("direct", "none", "off", "直连"):
        return None
    if clash_pool.spec_is_clash(spec):
        return clash_pool.select_node(clash_pool.node_of(spec))
    return {"http": spec, "https": spec}


def current_proxies():
    """当前使用的代理(requests proxies 格式)。池为空时用默认代理。"""
    global _pool_index
    if not PROXY_POOL:
        return DEFAULT_PROXIES
    if _pool_index < 0:
        _pool_index = 0
    return _normalize_spec(PROXY_POOL[_pool_index % len(PROXY_POOL)])


def next_proxies():
    """切到下一个代理并返回。每轮检查开头调用一次,即实现"每轮换一个出口 IP"。"""
    global _pool_index
    if PROXY_POOL:
        _pool_index += 1
    return current_proxies()


def read_pool_file(path):
    """从文本文件读 IP 池:每行一个代理,# 开头为注释,空行忽略。"""
    pool = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                pool.append(line)
    except OSError as e:
        print(f"[警告] 读取代理池文件失败: {e}", file=sys.stderr)
    if not pool:
        print("[警告] 代理池为空,将使用默认代理方式", file=sys.stderr)
    return pool


# ==================== 账号清单(批量监听多个账号) ====================

def read_account_file(path):
    """从文本文件读账号清单:每行一个账号,# 开头为注释,空行忽略。"""
    accounts = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                accounts.append(line)
    except OSError as e:
        print(f"[警告] 读取账号清单失败: {e}", file=sys.stderr)
    return accounts


def resolve_accounts_path(args=None):
    """账号清单文件的实际路径。--accounts > CONFIG['accounts_file'] > 默认 accounts.txt。"""
    if args and args.accounts:
        return args.accounts
    if ACCOUNTS_FILE:
        return ACCOUNTS_FILE
    return str(Path(__file__).resolve().parent / "accounts.txt")


def add_account_to_file(account, path):
    """把账号追加进账号清单文件(不存在则新建)。首次导入由 monitor_targets.added_at 记录。"""
    account = (account or "").strip().strip('"\'')
    if not account:
        raise ValueError("账号不能为空")
    with open(path, "a", encoding="utf-8") as f:
        f.write(account + "\n")
    return path


def load_accounts(args=None):
    """决定要监听哪些账号。优先级: --profile > --accounts 文件 > accounts_file > CONFIG['accounts'] > profile_url。"""
    if args and args.profile:
        return [args.profile]                      # 命令行单账号优先
    if args and args.accounts:
        return read_account_file(args.accounts)
    if ACCOUNTS_FILE and Path(ACCOUNTS_FILE).exists():
        return read_account_file(ACCOUNTS_FILE)
    if CONFIG.get("accounts"):
        return [str(a) for a in CONFIG["accounts"]]
    if CONFIG.get("profile_url"):
        return [CONFIG["profile_url"]]             # 退化为旧版单账号行为
    return []
# ==========================================================================


def make_session():
    global DEFAULT_PROXIES
    DEFAULT_PROXIES = _normalize_spec(CONFIG.get("proxy"))
    if TRANSPORT.startswith("curl_cffi"):
        s = http.Session(impersonate="chrome")
    else:
        s = http.Session()
    s.headers.update({
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/126.0.0.0 Safari/537.36"),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-GB,en;q=0.9",
        "Referer": f"https://{CONFIG['domain']}/",
    })
    return s


_cached_token = None          # 跨轮/跨线程复用的游客 access_token_web
CANCEL_REQUESTED = False       # 服务器"立即刷新"打断标志:设 True 让当前轮尽快中止


def ensure_token(session) -> None:
    """拿访问令牌:有登录 cookie 就用它的;否则用缓存的游客 token,没有才访问首页。

    游客 token(access_token_web)是 Vinted 的 JWT,实测**跨 IP、跨轮次可复用**:
    同一 token 换 12 个不同出口 9/12 成功(失败的出口是单点 Cloudflare 挑战),
    裸连接只带这一个 cookie 也能 200。所以每轮只带缓存 token 请求 wardrobe,
    不再下载整个首页(解压 ~1.9MB / 线上压缩 ~312KB,一轮流量的大头)。
    token 失效(wardrobe 401)时由 _get 触发一次 _refresh_token 重新拉首页。
    """
    if CONFIG.get("access_token_web"):
        session.cookies.set("access_token_web", CONFIG["access_token_web"])
        return
    global _cached_token
    if _cached_token:
        session.cookies.set("access_token_web", _cached_token)
        return
    _fetch_homepage_token(session, attempts=30)


def _fetch_homepage_token(session, attempts):
    """访问首页拿游客 token:逐出口轮换直到 200,成功把 token 存进缓存。

    200 才算 Cloudflare 挑战通过、游客 token 已种下;403 是 Vinted 的 JS 挑战页,
    换下一个出口重试。attempts 封顶(正常 30,401 触发刷新时 5),防池子全死空转。
    """
    global _cached_token
    last = "未尝试"
    for i in range(attempts):
        if CANCEL_REQUESTED:   # 服务器"立即刷新"打断
            raise RuntimeError("取 token 被打断(新的立即刷新)")
        try:
            r = session.get(f"https://{CONFIG['domain']}/", proxies=current_proxies(), timeout=30)
            if r.status_code == 200:
                tok = session.cookies.get("access_token_web")
                if tok:
                    _cached_token = tok
                return
            last = f"HTTP {r.status_code} ({(r.text or '')[:80]})"
        except http.exceptions.RequestException as e:
            last = str(e)
        if PROXY_POOL:
            next_proxies()          # 403 / 连不上都换下一个出口再试
        time.sleep(0.5 if i % 4 else 2)
    raise RuntimeError(f"无法访问 Vinted 首页获取令牌(最后: {last}),请检查代理/IP 池")


def _refresh_token(session) -> bool:
    """wardrobe 返回 401(游客 token 失效/无效)时强制刷新一次,成功返回 True。"""
    try:
        _fetch_homepage_token(session, attempts=5)
        return True
    except Exception:
        return False


def resolve_user_id(session, account: str, deadline=None) -> int:
    """从主页链接 / 数字ID / 用户名解析出数字 user_id。查询接口改版/响应非 JSON 时给明确报错。

    用户名 → 数字ID: 查询公开用户接口(需先 ensure_token 种访客 cookie)。
    池子被风控(403)时**换出口重试**(上限 5 次,含 deadline 中止):
    单次请求只碰一个出口,池子一波动就随机失败,加账号/新账号首轮全都报错。
    404 = 用户名不存在,是确定性结论,直接报错不再重试。
    """
    account = (account or "").strip().strip('"\'')
    m = re.search(r"/member/(\d+)", account)
    if m:
        return int(m.group(1))
    if account.isdigit():
        return int(account)
    # 用户名 → 数字ID: 查询公开用户接口(需先 ensure_token 种访客 cookie)
    url = f"https://{CONFIG['domain']}/api/v2/users/{account.lower()}"
    max_tries = min(30, max(5, len(PROXY_POOL))) if PROXY_POOL else 5
    last = None
    proxies = current_proxies()
    for attempt in range(max_tries):
        if CANCEL_REQUESTED:   # 服务器"立即刷新"打断
            raise ValueError(f"查询用户名被打断(新的立即刷新): {account}")
        if deadline is not None and time.time() > deadline:
            raise ValueError(f"本轮已超时,中止查询用户名: {account}")
        try:
            r = session.get(url, proxies=proxies, timeout=30)
            if r.status_code == 200:
                data = _parse_json(r, url)
                user = _dict_of(data.get("user"))
                uid = _to_int(user.get("id"))
                if uid:
                    return uid
                raise ValueError(f"用户名查询失败(响应无用户ID): {account}")
            if r.status_code == 404:
                raise ValueError(f"用户名不存在: {account}")
            last = f"HTTP {r.status_code}"
        except http.exceptions.RequestException as e:
            last = str(e)
        if attempt < max_tries - 1:
            time.sleep(1)
            proxies = next_proxies()   # 换下一个出口再试(403 常是单个出口被挑战)
    raise ValueError(f"用户名查询失败({last}): {account}")


def _get(session, url, params=None, retries=None, deadline=None):
    """带重试的 GET。成功返回 Response,失败抛错(对网络抖动和临时风控都容错)。

    IP 池在池: 网络错误 / 非 200(如 403 被风控)会自动切到下一个代理重试。
    重试次数 = 池大小(至少 5 次),但**上限 30 次**——池子再大也不能让单次请求把
    1000 个出口全试一遍,否则池子全被风控时一轮会卡几十分钟(和 ensure_token 同款保护)。
    deadline 传入服务器单轮截止时间,超时直接中止,防止单账号拖垮整轮。
    """
    if retries is None:
        retries = max(5, len(PROXY_POOL)) if PROXY_POOL else 5
    retries = min(retries, 30)
    last = None
    proxies = current_proxies()
    refreshed = False   # 每次 _get 最多刷新一次 token(401 时)
    for attempt in range(retries):
        if CANCEL_REQUESTED:   # 服务器"立即刷新"打断
            raise RuntimeError("抓取被打断(新的立即刷新)")
        if deadline is not None and time.time() > deadline:
            raise RuntimeError("本轮抓取已超过截止时间,中止")
        try:
            r = session.get(url, params=params, proxies=proxies, timeout=30)
            if r.status_code == 200:
                return r
            if r.status_code == 401 and not refreshed and _refresh_token(session):
                refreshed = True
                continue        # 游客 token 失效 → 换新后同出口立刻重试
            last = f"HTTP {r.status_code}: {(r.text or '')[:120]}"
        except http.exceptions.RequestException as e:
            last = str(e)
            if "407" in last:
                # 407 = 青果短效池 IP 已过期(确定性死代理),立刻换出口,不等 2s
                time.sleep(0.3)
                proxies = next_proxies()
                continue
        if attempt < retries - 1:
            time.sleep(2)              # 连接异常和非 200 都等一会再重试
            proxies = next_proxies()   # 换下一个出口 IP 再试
    raise RuntimeError(f"请求失败 {url} → {last}")


def fetch_items(session, user_id, max_pages=200, deadline=None):
    """拉取在售商品。匿名即可用 wardrobe 接口(2026-08 实测通过)。

    鲁棒性要点:
      - 响应不是 JSON / 结构里没有 items 列表 → 直接报错,绝不把"抓取失败"误当成
        "商品全下架了",否则会把所有在售商品误判成已售。
      - 去重 + 最大页数限制,防止接口改版后分页参数失效导致无限循环。
      - 畸形条目(不是 dict / 缺 id)自动跳过,不让单条脏数据拖垮整轮。
    """
    items, page, per_page = [], 1, 96
    seen = set()
    while page <= max_pages:
        url = f"https://{CONFIG['domain']}/api/v2/wardrobe/{user_id}/items"
        r = _get(session, url, params={"page": page, "per_page": per_page}, deadline=deadline)
        data = _parse_json(r, url)
        raw_items = data.get("items")
        if not isinstance(raw_items, list):
            # items 字段缺失或变成别的类型 = 页面改版,必须显式报错
            raise RuntimeError(
                f"接口 items 字段不是列表(可能 Vinted 页面改版),"
                f"实际类型 {type(raw_items).__name__}: {url}")
        batch = raw_items
        # 过滤畸形条目并去重(防分页异常时无限循环 / 重复记录)
        new = []
        for it in batch:
            if not isinstance(it, dict) or it.get("id") is None:
                continue
            if it.get("id") in seen:
                continue
            seen.add(it.get("id"))
            new.append(it)
        items.extend(new)
        if not batch or len(batch) < per_page or not new:
            break
        page += 1
        time.sleep(random.uniform(*CONFIG["page_delay"]))
    return items


def _listing_status(it):
    """从状态标志推导中文售出状态。"""
    if it.get("is_closed"):
        return "sold"
    if it.get("is_reserved"):
        return "reserved"
    if it.get("is_draft"):
        return "draft"
    if it.get("is_hidden"):
        return "hidden"
    return "visible"


STATUS_ZH = {"visible": "在售", "sold": "已售", "reserved": "被预留",
             "draft": "草稿", "hidden": "已隐藏"}


def normalize_item(it: dict) -> dict:
    """把接口条目规整成统一结构。任何字段缺失 / 类型异常都给安全默认值,不让单条脏数据拖垮整轮。"""
    if not isinstance(it, dict):
        raise ValueError(f"商品数据格式异常: {type(it).__name__}(可能页面改版)")
    item_id = it.get("id")
    if item_id is None:
        raise ValueError("商品数据缺少 id 字段(可能页面改版)")

    url = it.get("url") or f"https://{CONFIG['domain']}{it.get('path') or ''}"
    raw_price = it.get("price")
    if isinstance(raw_price, dict):
        amount = _to_float(raw_price.get("amount"))
        currency = _to_str(raw_price.get("currency_code")) or _to_str(it.get("currency"))
    else:
        amount = _to_float(raw_price)
        currency = _to_str(it.get("currency"))

    # 上架时间:接口没有 created_at_ts,用照片上传时间戳近似(误差通常几分钟)
    created_at_ts = it.get("created_at_ts") or it.get("created_at")
    if not created_at_ts:
        photos = _list_of(it.get("photos"))
        if photos:
            hr = _dict_of(photos[0].get("high_resolution"))
            created_at_ts = hr.get("timestamp")

    return {
        "item_id": item_id,
        "title": _to_str(it.get("title")),
        "price": amount,
        "currency": currency,
        "url": url,
        "created_at_ts": created_at_ts,
        "favourite_count": _to_int(it.get("favourite_count")),
        "view_count": _to_int(it.get("view_count")),
        "status": _listing_status(it),
    }


def init_db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.execute("""CREATE TABLE IF NOT EXISTS items(
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
    con.execute("""CREATE TABLE IF NOT EXISTS snapshots(
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        item_id        INTEGER,
        ts             INTEGER,
        favourite_count INTEGER,
        view_count     INTEGER,
        price          REAL,
        status         TEXT
    )""")
    # 老库迁移:旧版是单账号,items 没有 account 列,这里补上(已有数据 account 为空,首次运行时回填)
    cols = {r[1] for r in con.execute("PRAGMA table_info(items)")}
    if "account" not in cols:
        con.execute("ALTER TABLE items ADD COLUMN account TEXT")
    # 被监听账号清单:added_at = 第一次导入(开始监听)该账号的时间
    con.execute("""CREATE TABLE IF NOT EXISTS monitor_targets(
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        account        TEXT NOT NULL UNIQUE,
        user_id        INTEGER,
        added_at       INTEGER,
        last_checked_at INTEGER
    )""")
    con.commit()
    return con


def fmt_time(ts):
    """格式化时间戳。格式异常 / 越界 / 未知类型都给可读兜底,绝不抛异常。"""
    if not ts:
        return "未知"
    if isinstance(ts, (int, float)):
        try:
            return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
        except (OverflowError, OSError, ValueError):
            return str(ts)
    try:  # 兼容 ISO 字符串
        return datetime.datetime.fromisoformat(str(ts).replace("Z", "+00:00")) \
            .strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(ts)


def ensure_targets(con, session, accounts, now):
    """把账号清单同步进 monitor_targets,返回 [(account, user_id)]。

    新账号第一次导入时记录 added_at(=首次导入时间),之后不再覆盖;
    解析出的数字 user_id 缓存下来,避免每轮都去查用户名接口。
    """
    targets = []
    for acc in accounts:
        acc = (acc or "").strip().strip('"\'')
        if not acc:
            continue
        row = con.execute("SELECT user_id FROM monitor_targets WHERE account=?", (acc,)).fetchone()
        if row and row[0]:
            uid = row[0]  # 已缓存,不再解析
        else:
            try:
                uid = resolve_user_id(session, acc)
            except Exception as e:
                print(f"[警告] 解析账号「{acc}」失败,本轮跳过: {e}", file=sys.stderr)
                continue
            con.execute("INSERT OR IGNORE INTO monitor_targets(account, user_id, added_at) VALUES(?,?,?)",
                        (acc, uid, now))
        targets.append((acc, uid))
    con.commit()
    return targets


def process_account(con, now, account, items) -> None:
    """记录单个账号的在售商品:新商品打 first_seen_at,旧商品更新快照,消失的商品标已售。

    售出检测按账号隔离:只看这个账号自己上次记录过的商品,不会把
    其他账号(本轮可能没轮到 / 抓取失败)的商品误判成已售。
    """
    prev = {r[0]: r[1:] for r in con.execute(
        "SELECT item_id, favourite_count, view_count, price, created_at_ts, first_seen_at, status"
        " FROM items WHERE account=?", (account,))}
    current_ids = set()

    new_items, price_changes = [], []
    for it in items:
        try:
            n = normalize_item(it)
        except (KeyError, TypeError, ValueError) as e:
            print(f"  跳过一条格式异常的商品数据(可能页面改版): {e}", file=sys.stderr)
            continue
        current_ids.add(n["item_id"])
        old = prev.get(n["item_id"])
        if old is None:
            # 首次出现:first_seen_at = 首次导入(发现)时间,之后所有 UPDATE 都不会覆盖它
            con.execute(
                "INSERT INTO items(item_id,account,title,price,currency,url,created_at_ts,favourite_count,view_count,status,first_seen_at,last_seen_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (n["item_id"], account, n["title"], n["price"], n["currency"], n["url"],
                 n["created_at_ts"], n["favourite_count"], n["view_count"], n["status"], now, now))
            new_items.append(n)
        else:
            if old[2] is not None and old[2] != n["price"]:
                price_changes.append((n, old[2], n["price"]))
            if old[3] is None and n["created_at_ts"]:
                con.execute("UPDATE items SET created_at_ts=? WHERE item_id=?", (n["created_at_ts"], n["item_id"]))
            con.execute("UPDATE items SET title=?, price=?, favourite_count=?, view_count=?, status=?, last_seen_at=? WHERE item_id=?",
                        (n["title"], n["price"], n["favourite_count"], n["view_count"], n["status"], now, n["item_id"]))
        con.execute("INSERT INTO snapshots(item_id,ts,favourite_count,view_count,price,status)"
                    " VALUES(?,?,?,?,?,?)",
                    (n["item_id"], now, n["favourite_count"], n["view_count"], n["price"], n["status"]))

    # 售出检测: 该账号之前记录过、这次不在在售列表里 -> 标记 sold
    # (注意: 若本轮抓取因页面改版/风控报错,会在 fetch_items 里直接抛错,不会走到这里,
    #   因此不会被误判成"全部下架";其他账号的商品也绝不会被本账号误判)
    sold = []
    for row in con.execute("SELECT item_id, title, status FROM items WHERE account=?", (account,)).fetchall():
        iid, title, status = row
        if iid not in current_ids and status != "sold":
            con.execute("UPDATE items SET status='sold' WHERE item_id=?", (iid,))
            sold.append((iid, title or ""))  # 老数据 title 可能是 NULL,兜底防打印崩溃
    if sold:
        print(f"[提示] {account} 有 {len(sold)} 件商品从在售列表消失,已标记为已售/下架。", file=sys.stderr)
    con.commit()

    _print_report(now, account, items, prev, new_items, sold, price_changes)


def run_check(session, con, now, accounts) -> None:
    """跑一轮批量检查:逐个账号抓取在售商品。一个账号失败不拖垮其他账号。"""
    targets = ensure_targets(con, session, accounts, now)
    if not targets:
        print("[警告] 账号清单为空,没有可监控的账号。用 --accounts 指定文件,或在 CONFIG['accounts'] 里填。",
              file=sys.stderr)
        return
    # 老库升级兼容:旧单账号数据 account 列为空,若当前只有一个监听账号,回填它
    if len(targets) == 1:
        con.execute("UPDATE items SET account=? WHERE account IS NULL", (targets[0][0],))
        con.commit()
    ok = 0
    for idx, (acc, uid) in enumerate(targets, 1):
        print(f"\n[账号 {idx}/{len(targets)}] {acc}")
        try:
            items = fetch_items(session, uid)
        except http.exceptions.RequestException as e:
            print(f"  ↳ 网络错误: {e}\n    提示: 确认代理/节点可用;被 Cloudflare 封就换节点。", file=sys.stderr)
            continue
        except Exception as e:
            print(f"  ↳ 抓取失败: {e}", file=sys.stderr)
            continue
        process_account(con, now, acc, items)
        con.execute("UPDATE monitor_targets SET last_checked_at=? WHERE account=?", (now, acc))
        ok += 1
    con.commit()
    print(f"\n[{fmt_time(now)}] 本轮批量检查完成: 成功 {ok}/{len(targets)} 个账号")


def _print_report(now, account, items, prev, new_items, sold, price_changes):
    sep = "=" * 62
    print(f"\n{sep}")
    print(f"Vinted 监控报告  {fmt_time(now)}  账号: {account}  在售 {len(items)} 件")
    print(sep)

    if new_items:
        print(f"\n[新增上架] {len(new_items)} 件:")
        for n in new_items:
            print(f"  + {n['title'][:40]}  {n['currency']}{n['price']:.2f}"
                  f"  上架≈{fmt_time(n['created_at_ts'])}")
    if sold:
        print(f"\n[已售出/下架] {len(sold)} 件:")
        for iid, title in sold:
            print(f"  - {title[:50]}  https://{CONFIG['domain']}/items/{iid}")
    if price_changes:
        print(f"\n[价格变动] {len(price_changes)} 件:")
        for n, old_p, new_p in price_changes:
            mark = "降价" if new_p < old_p else "涨价"
            print(f"  ~ {n['title'][:40]}  {n['currency']}{old_p:.2f} → {n['currency']}{new_p:.2f}  ({mark})")

    print("\n[在售商品 点赞·状态变化]")
    rows = []
    for it in items:
        try:
            n = normalize_item(it)
        except (KeyError, TypeError, ValueError):
            continue
        old = prev.get(n["item_id"])
        dl = (n["favourite_count"] - old[0]) if old else None
        rows.append((n, dl, old))
    rows.sort(key=lambda r: (r[1] if r[1] is not None else -1), reverse=True)
    for n, dl, old in rows:
        d = f" +{dl}" if dl else ""
        listed = n["created_at_ts"] or (old[4] if old else None)
        first_seen = old[4] if old else now   # 首次导入(第一次发现该商品)的时间
        print(f"  {n['title'][:30]:<32} {n['currency']}{n['price']:.2f}"
              f"  赞{n['favourite_count']}{d}  {STATUS_ZH.get(n['status'], n['status'])}"
              f"  上架≈{fmt_time(listed)}  首导{fmt_time(first_seen)}")
    print(sep)


def main():
    ap = argparse.ArgumentParser(description="Vinted 自营商品监控器")
    ap.add_argument("--once", action="store_true", help="只跑一次检查后退出(测试用)")
    ap.add_argument("--profile", help="临时指定卖家主页链接、数字ID或用户名,覆盖 CONFIG['profile_url']")
    ap.add_argument("--domain", help="国家站点域名,如 www.vinted.co.uk / www.vinted.fr(默认取 CONFIG)")
    ap.add_argument("--proxy", help="单个代理,如 http://127.0.0.1:7890 或 socks5h://...;覆盖 CONFIG['proxy']")
    ap.add_argument("--proxy-pool", metavar="FILE", help="IP 池文件,每行一个代理(# 为注释);每轮检查轮换,失败自动切换")
    ap.add_argument("--accounts", metavar="FILE", help="账号清单文件,每行一个账号(主页链接/数字ID/用户名),# 为注释")
    ap.add_argument("--add-account", metavar="ACCOUNT", help="把账号追加进账号清单文件后退出(导入新账号用)")
    args = ap.parse_args()
    if args.add_account:
        try:
            path = add_account_to_file(args.add_account, resolve_accounts_path(args))
            print(f"已把账号「{args.add_account}」追加到 {path}(首次导入时间将在首次监控时记录)")
        except (ValueError, OSError) as e:
            print(f"[错误] 写入账号清单失败: {e}", file=sys.stderr)
            sys.exit(1)
        return
    if args.profile:
        CONFIG["profile_url"] = args.profile
    if args.domain:
        CONFIG["domain"] = args.domain.strip().lstrip("http://").lstrip("https://").rstrip("/")
    if args.proxy:
        CONFIG["proxy"] = args.proxy
    # 组装 IP 池: 命令行文件 > CONFIG 里的列表 > 命令行单代理
    global PROXY_POOL
    if args.proxy_pool:
        PROXY_POOL = read_pool_file(args.proxy_pool)
    elif CONFIG.get("proxy_pool"):
        PROXY_POOL = [str(p) for p in CONFIG["proxy_pool"]]
    elif args.proxy:
        PROXY_POOL = [args.proxy]

    session = make_session()
    con = init_db()
    if PROXY_POOL:
        shown = ", ".join(p or "直连" for p in PROXY_POOL[:5])
        if len(PROXY_POOL) > 5:
            shown += f"…(共{len(PROXY_POOL)}个)"
        pool_desc = f"IP池[{len(PROXY_POOL)}个: {shown}]"
    else:
        pool_desc = f"单代理 {CONFIG.get('proxy') or '无'}"
    accounts = load_accounts(args)
    if not accounts:
        print("[警告] 没有配置任何账号。请在 CONFIG['profile_url']/['accounts'] 填写,"
              "或用 --accounts 指定账号清单文件。", file=sys.stderr)
    account_desc = ", ".join(a for a in accounts[:5]) + ("…" if len(accounts) > 5 else "")
    print(f"[传输层] {TRANSPORT}   [代理] {pool_desc}"
          f"   [登录cookie] {'已填' if CONFIG.get('access_token_web') else '未填(匿名)'}"
          f"   [账号] {len(accounts)} 个:{account_desc}")

    def do_check():
        now = int(time.time())
        try:
            next_proxies()  # 每个检查周期轮换一个出口 IP(有池时;_get 失败还会继续切换)
            ensure_token(session)
            run_check(session, con, now, accounts)
        except http.exceptions.RequestException as e:
            print(f"[{fmt_time(now)}] 网络错误: {e}", file=sys.stderr)
            print("   提示: 请确认代理已开启、节点可用;若被 Cloudflare 封 IP,换一个代理节点再试。", file=sys.stderr)
        except Exception as e:
            print(f"[{fmt_time(now)}] 出错: {e}", file=sys.stderr)

    do_check()
    if args.once:
        return

    interval = CONFIG["interval_minutes"] * 60
    print(f"进入监控循环,每 {CONFIG['interval_minutes']} 分钟检查一次 (Ctrl+C 退出)")
    while True:
        time.sleep(interval)
        do_check()


if __name__ == "__main__":
    main()
