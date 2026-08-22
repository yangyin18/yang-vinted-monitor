"""通过本地 Clash 把机场节点变成 IP 池,供 monitor.py / vinted_check.py 使用。

原理:
  机场(vmess)节点不能直连 curl_cffi,但本地 Clash 已经配好了这些节点,
  所有节点都从同一个本地端口(mixed-port 7890)出去 —— 所以"换 IP"的动作
  就是切换节点那一下。这里把"切节点"和"返回本地代理地址"打包成一个动作:
  在池文件里写 `clash:<节点名>`(每行一个),脚本的 _normalize_spec 遇到这种
  条目会先调 select_node() 把 Clash 出口切到对应节点,再返回 127.0.0.1:7890。

  每轮检查开头 next_proxies() 换一个节点、请求失败重试时也换下一个节点,
  即实现了"每轮换出口 IP + 失败自动切换"。死节点顶多每次轮到时多一次失败,
  重试循环会自动跳到下一个,不需要额外的健康检测。

用法:
  1) 确认本地 Clash 在跑,external-controller 和 secret 对得上(见下方默认值)。
  2) 池文件每行写 `clash:<节点名>`,例:
       python monitor.py --proxy-pool clash_pool.txt
     CONFIG["proxy_pool"] 里填同样的行也行。

环境变量(可覆盖默认值):
  CLASH_PROXY   本地 Clash 代理地址,默认 http://127.0.0.1:7890
  CLASH_CTRL    external-controller,默认 http://127.0.0.1:43478
  CLASH_SECRET  API 密钥(填你自己的,示例代码里是占位符)
  CLASH_GROUP   要切换的代理组名
"""
import json
import os
import sys
import time
import urllib.parse
import urllib.request

LOCAL_PROXY = os.environ.get("CLASH_PROXY", "http://127.0.0.1:7890")
CTRL = os.environ.get("CLASH_CTRL", "http://127.0.0.1:43478")
SECRET = os.environ.get("CLASH_SECRET", "YOUR_CLASH_SECRET")
GROUP = os.environ.get("CLASH_GROUP", "YOUR_PROXY_GROUP")

_hinted = False
_cfg_loaded = False


def _load_runtime_config():
    """从 CFW 的 config.yaml 读 controller 端口/密钥(重启后端口会随机变)。

    显式设置了 CLASH_CTRL/CLASH_SECRET 就跳过;文件读不到就沿用默认值。
    """
    global CTRL, SECRET, _cfg_loaded
    if _cfg_loaded:
        return
    _cfg_loaded = True
    if os.environ.get("CLASH_CTRL") and os.environ.get("CLASH_SECRET"):
        return
    path = os.path.join(os.path.expanduser("~"), ".config", "clash", "config.yaml")
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("external-controller:"):
                    v = line.split(":", 1)[1].strip().strip("'\"")
                    CTRL = v if "://" in v else "http://" + v
                elif line.startswith("secret:"):
                    SECRET = line.split(":", 1)[1].strip().strip("'\"")
    except OSError:
        pass  # 文件不存在 → 用环境变量或默认值


def _api(path, method="GET", data=None):
    """调 Clash REST API。GET 返回 JSON,PUT 成功时响应体为空。"""
    _load_runtime_config()
    req = urllib.request.Request(CTRL + path, method=method)
    req.add_header("Authorization", "Bearer " + SECRET)
    if data is not None:
        req.data = json.dumps(data).encode()
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=8) as r:
        body = r.read().decode()
    return json.loads(body) if body.strip() else None


def _switch(node):
    """把 GROUP 组的选中节点切到 node。路径=组名,body=节点名(都是要编码的)。"""
    global _hinted
    if not _hinted:
        print(f"[clash] 通过本地 Clash 轮换机场节点 → {LOCAL_PROXY}", file=sys.stderr)
        _hinted = True
    path = "/proxies/" + urllib.parse.quote(GROUP, safe="")
    _api(path, "PUT", {"name": node})
    time.sleep(1.2)  # 等 Clash 完成切换,再发业务请求


def select_node(node):
    """把 Clash 切到指定节点,返回 requests 的 proxies 字典。

    切换失败(Clash 没开 / 节点名变了)时打印警告并沿用当前节点返回本地代理,
    不抛错 —— 顶多本次请求还是走旧节点,重试逻辑会接着换。
    """
    try:
        _switch(node)
    except Exception as e:
        print(f"[clash] 切换节点「{node}」失败({e}),沿用当前选中节点", file=sys.stderr)
    return {"http": LOCAL_PROXY, "https": LOCAL_PROXY}


def spec_is_clash(spec):
    """判断池条目是不是 `clash:<节点名>` 格式。"""
    spec = (spec or "").strip()
    return spec.lower().startswith("clash:")


def node_of(spec):
    """从 `clash:<节点名>` 里取出节点名。"""
    return (spec or "").split(":", 1)[1].strip() if spec_is_clash(spec) else spec
