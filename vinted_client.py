"""Vinted 多账号监控 —— 本地 Windows 可视化客户端(全部商品一个列表)。

设计原则:用户不看单个账号,只关心所有账号的**全部商品**合在一起。
  - 不区分账号:列表里没有账号列,不需要选账号,一次拉回全部商品
  - 新上架自动补进来、已售置灰沉底、赞数增量高亮
  - 操作只有:连服务器 / 批量添加账号 / 删账号(输入名字,不靠选中) / 立即刷新

本机**不存任何数据**。唯一可能落盘的是一个最小配置文件
  %APPDATA%\\vinted-client\\config.ini
只存服务器地址;删掉它没有任何影响。

打包:  pyinstaller vinted_client.spec
运行:  python vinted_client.py
"""
import configparser
import os
import queue
import threading
import time
import webbrowser
from datetime import datetime
from pathlib import Path

import customtkinter as ctk
import requests
from tkinter import messagebox, ttk

APP_NAME = "Vinted 商品监控"
DEFAULT_SERVER_URL = "http://your-server-ip:8000"
CONFIG_PATH = Path(os.environ.get("APPDATA", str(Path.home()))) / "vinted-client" / "config.ini"

# 旧版服务器不回传 status_zh,客户端自己兜底翻译,避免显示英文状态
STATUS_ZH = {"visible": "在售", "sold": "已售", "reserved": "被预留",
             "draft": "草稿", "hidden": "已隐藏"}


class VintedClient(ctk.CTk):
    def __init__(self):
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")
        super().__init__()
        self.title(APP_NAME)
        self.geometry("1180x680")
        self.minsize(980, 560)

        # 状态
        self.cfg = self._load_config()
        self.q = queue.Queue()
        self._running = False        # 轮询线程是否存活
        self._connected = False      # 最近一次 status 是否成功
        self.server_url = ""
        self.updated_at = 0          # 最近一次渲染的 cache.updated_at(轮询线程维护)
        self._dirty = True           # 首次连接即拉一次全部商品(服务端 DB 可能有上次遗留)
        self._item_urls = {}         # tree iid -> url
        self._item_ids = {}          # tree iid -> item_id(删除选中商品用)
        self._all_items = []         # 最近拉回的全部商品(筛选/排序在本地做,不重拉)
        self._sort_earliest = False  # False=上架时间最新在前,True=最早上架在前
        self._prev_fav = {}          # item_id -> 上次赞数(客户端算增量,重启归零)

        self._build_ui()
        self._pump_queue()

    # ============================ UI ============================
    def _build_ui(self):
        self.grid_rowconfigure(3, weight=1)
        self.grid_columnconfigure(0, weight=1)

        # --- 顶栏:连接 + 手动刷新 ---
        top = ctk.CTkFrame(self)
        top.grid(row=0, column=0, sticky="ew", padx=8, pady=(8, 4))
        top.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(top, text="服务器地址").grid(row=0, column=0, padx=(12, 4), pady=8)
        self.url_var = ctk.StringVar(value=DEFAULT_SERVER_URL)
        ctk.CTkEntry(top, textvariable=self.url_var, width=340).grid(row=0, column=1, padx=4, sticky="w")

        self.btn_connect = ctk.CTkButton(top, text="连接", width=64, command=self._toggle_connect)
        self.btn_connect.grid(row=0, column=2, padx=8, pady=8)

        self.light = ctk.CTkLabel(top, text="●", text_color="#555555",
                                  font=ctk.CTkFont(size=18))
        self.light.grid(row=0, column=3, padx=(0, 8))

        self.btn_refresh = ctk.CTkButton(top, text="立即刷新", width=92, state="disabled",
                                         command=self._manual_refresh)
        self.btn_refresh.grid(row=0, column=4, padx=(4, 12))

        # --- 账号管理行:批量添加 + 删账号(输名字,不靠选中) ---
        mgmt = ctk.CTkFrame(self)
        mgmt.grid(row=1, column=0, sticky="ew", padx=8, pady=(0, 4))
        mgmt.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(mgmt, text="批量添加(每行一个账号):",
                     anchor="w", font=ctk.CTkFont(size=11), text_color="#aaaaaa").grid(
            row=0, column=0, sticky="w", padx=12, pady=(6, 0))
        self.txt_add = ctk.CTkTextbox(mgmt, height=48)
        self.txt_add.grid(row=0, column=1, sticky="ew", padx=4, pady=(6, 0))
        self.btn_add = ctk.CTkButton(mgmt, text="添加账号", width=90, state="disabled",
                                     command=self._add_accounts)
        self.btn_add.grid(row=0, column=2, padx=6, pady=(6, 0))

        ctk.CTkLabel(mgmt, text="删除账号:",
                     anchor="w", font=ctk.CTkFont(size=11), text_color="#aaaaaa").grid(
            row=1, column=0, sticky="w", padx=12, pady=(6, 0))
        self.del_var = ctk.StringVar()
        ctk.CTkEntry(mgmt, textvariable=self.del_var, width=260).grid(
            row=1, column=1, sticky="w", padx=4, pady=(6, 0))
        self.btn_del = ctk.CTkButton(mgmt, text="删除该账号", width=90, state="disabled",
                                     fg_color="#9c3030", hover_color="#b84242",
                                     command=self._delete_by_input)
        self.btn_del.grid(row=1, column=2, padx=6, pady=(6, 0))
        ctk.CTkLabel(mgmt, text="数字ID / 主页链接 / 用户名 均可;双击商品行浏览器打开",
                     anchor="w", font=ctk.CTkFont(size=10), text_color="#777777").grid(
            row=2, column=0, columnspan=3, sticky="w", padx=12, pady=(0, 4))

        # --- 筛选/排序/删除行 ---
        filt = ctk.CTkFrame(self)
        filt.grid(row=2, column=0, sticky="ew", padx=8, pady=(0, 4))
        filt.grid_columnconfigure(3, weight=1)

        ctk.CTkLabel(filt, text="状态:", font=ctk.CTkFont(size=11), text_color="#aaaaaa").grid(
            row=0, column=0, padx=(12, 2), pady=6)
        self.status_var = ctk.StringVar(value="全部")
        ctk.CTkOptionMenu(filt, values=["全部", "在售", "已售", "被预留", "草稿", "已隐藏"],
                          variable=self.status_var, width=120,
                          command=lambda _v: self._apply_view()).grid(
            row=0, column=1, padx=(0, 12), pady=6)
        self.btn_sort = ctk.CTkButton(filt, text="上架时间: 最新在前", width=160,
                                      command=self._toggle_sort)
        self.btn_sort.grid(row=0, column=2, padx=(0, 12), pady=6)
        self.btn_del_item = ctk.CTkButton(filt, text="删除选中商品", width=110,
                                          fg_color="#9c3030", hover_color="#b84242",
                                          state="disabled", command=self._delete_selected_item)
        self.btn_del_item.grid(row=0, column=4, padx=(0, 6), pady=6)
        self.btn_del_sold = ctk.CTkButton(filt, text="删除全部已售", width=110,
                                          fg_color="#9c3030", hover_color="#b84242",
                                          command=self._delete_all_sold)
        self.btn_del_sold.grid(row=0, column=5, padx=(0, 12), pady=6)

        # --- 商品表格:全部账号商品合并成一个列表,无账号列 ---
        wrap = ctk.CTkFrame(self)
        wrap.grid(row=3, column=0, sticky="nsew", padx=8, pady=(0, 4))
        wrap.grid_rowconfigure(0, weight=1)
        wrap.grid_columnconfigure(0, weight=1)

        self._style_tree()
        self.tree = ttk.Treeview(wrap, columns=("title", "price", "fav", "status", "time"),
                                 show="headings", selectmode="browse")
        self.tree.heading("title", text="标题")
        self.tree.column("title", width=520, anchor="w")
        self.tree.heading("price", text="价格")
        self.tree.column("price", width=90, anchor="e")
        self.tree.heading("fav", text="赞")
        self.tree.column("fav", width=100, anchor="e")
        self.tree.heading("status", text="状态")
        self.tree.column("status", width=72, anchor="center")
        self.tree.heading("time", text="上架时间")
        self.tree.column("time", width=130, anchor="w")

        vsb = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")

        self.tree.tag_configure("new", foreground="#4da6ff", font=("Microsoft YaHei UI", 13, "bold"))
        self.tree.tag_configure("fav_up", foreground="#00cc66")
        self.tree.tag_configure("fav_down", foreground="#ff5555")
        self.tree.tag_configure("sold", foreground="#888888")
        self.tree.tag_configure("reserved", foreground="#d0a000")
        self.tree.bind("<Double-1>", self._on_double_click)

        # --- 底栏 ---
        bar = ctk.CTkFrame(self)
        bar.grid(row=4, column=0, sticky="ew", padx=8, pady=(0, 8))
        bar.grid_columnconfigure(0, weight=1)
        self.lbl_status = ctk.CTkLabel(bar, text="未连接", anchor="w")
        self.lbl_status.grid(row=0, column=0, sticky="w", padx=12, pady=6)

    def _style_tree(self):
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        bg = "#2b2b2b"
        font = ("Microsoft YaHei UI", 13)
        style.configure("Treeview", background=bg, fieldbackground=bg,
                        foreground="#ffffff", rowheight=32, borderwidth=0,
                        relief="flat", font=font)
        style.map("Treeview", background=[("selected", "#1f6aa5")],
                  foreground=[("selected", "#ffffff")])
        style.configure("Treeview.Heading", background="#232323", foreground="#e6e6e6",
                        relief="flat", borderwidth=0, padding=7, font=("Microsoft YaHei UI", 13, "bold"))
        style.map("Treeview.Heading", background=[("active", "#2b2b2b")])

    # ============================ 连接管理 ============================
    def _toggle_connect(self):
        if self._running:
            self._disconnect()
        else:
            self._connect()

    def _connect(self):
        url = self.url_var.get().strip().rstrip("/")
        if not url:
            self._flash("请填写服务器地址")
            return
        self.server_url = url
        self._save_config(url)
        self._running = True
        self._connected = False
        self.btn_connect.configure(text="断开")
        self.btn_refresh.configure(state="normal")
        self.btn_add.configure(state="normal")
        self.btn_del.configure(state="normal")
        self.btn_del_item.configure(state="normal")
        self.light.configure(text="○", text_color="#ffcc00")
        self.lbl_status.configure(text="连接中…")
        threading.Thread(target=self._poll_loop, daemon=True).start()

    def _disconnect(self):
        self._running = False
        self._connected = False
        self.btn_connect.configure(text="连接")
        self.btn_refresh.configure(state="disabled")
        self.btn_add.configure(state="disabled")
        self.btn_del.configure(state="disabled")
        self.btn_del_item.configure(state="disabled")
        self.light.configure(text="●", text_color="#555555")
        self.lbl_status.configure(text="已断开")

    # ============================ 轮询 ============================
    def _poll_loop(self):
        base = self.server_url.rstrip("/")
        while self._running:
            try:
                st = requests.get(base + "/api/status", timeout=10)
                if st.status_code == 401:
                    self.q.put(("conn_error", "服务器拒绝访问(401)"))
                elif st.status_code != 200:
                    self.q.put(("conn_error", f"服务器返回 HTTP {st.status_code}"))
                else:
                    data = st.json()
                    new_upd = data.get("cache", {}).get("updated_at", 0)
                    if new_upd != self.updated_at:      # 服务器刷新过 → 需要重新拉全部商品
                        self._dirty = True
                        self.updated_at = new_upd
                    self.q.put(("status", data))
                    # 服务器刷新过(或首次连接)→ 拉全部商品(后台线程,兼容新旧服务器)
                    if self._dirty:
                        self._dirty = False
                        threading.Thread(target=self._fetch_items_all,
                                         args=(base,), daemon=True).start()
            except requests.exceptions.RequestException as e:
                self.q.put(("conn_error", f"无法连接服务器: {type(e).__name__}"))
            except Exception as e:
                self.q.put(("conn_error", f"轮询异常: {e}"))
            time.sleep(5 if not self._connected else 15)

    def _fetch_items_all(self, base):
        """拉全部账号的商品合并成一个列表。

        新版服务器:/api/items 直接返回全部(一次请求)。
        旧版服务器:/api/items 强制要 account 参数 → 逐个账号拉再合并(自动降级)。
        """
        try:
            it = requests.get(base + "/api/items", timeout=30)
            if it.status_code == 200:
                self.q.put(("items", it.json()))
                return
            if it.status_code not in (400, 422):
                # 真错误(500/502 等)不是旧版兼容问题
                self.q.put(("items_error", it.json() if it.status_code == 502 else None))
                return
            # 旧版服务器:需要 account → 逐个账号拉,合并
            acc = requests.get(base + "/api/accounts", timeout=15)
            if acc.status_code != 200:
                self.q.put(("items_error", None))
                return
            merged = []
            for a in acc.json().get("accounts", []):
                acct = a.get("account")
                if not acct:
                    continue
                try:
                    r = requests.get(base + "/api/items",
                                     params={"account": acct}, timeout=20)
                    if r.status_code == 200:
                        merged.extend(r.json().get("items", []))
                except requests.exceptions.RequestException:
                    continue   # 单账号失败跳过,不拖垮整体
            self.q.put(("items", {"ok": True, "items": merged, "total": len(merged)}))
        except requests.exceptions.RequestException as e:
            self.q.put(("conn_error", f"无法连接服务器: {type(e).__name__}"))

    def _pump_queue(self):
        try:
            while True:
                msg = self.q.get_nowait()
                kind = msg[0]
                if kind == "status":
                    self._on_status(msg[1])
                elif kind == "items":
                    self._on_items(msg[1])
                elif kind == "items_error":
                    self._on_items_error(msg[1])
                elif kind == "conn_error":
                    self._on_conn_error(msg[1])
                elif kind == "flash":
                    self._flash(msg[1])
        except queue.Empty:
            pass
        self.after(250, self._pump_queue)

    # ============================ 消息处理 ============================
    def _on_status(self, data):
        self._connected = True
        self.light.configure(text="●", text_color="#00cc66")
        c = data.get("cache", {})
        accounts_n = data.get("accounts", {}).get("total", 0)
        items_n = data.get("items", {}).get("total", 0)
        pool = data.get("pool", {})
        pool_n = pool.get("size", 0)
        refreshing = c.get("refreshing", False)
        last = ""
        if self.updated_at:
            last = datetime.fromtimestamp(self.updated_at).strftime("%m-%d %H:%M:%S")
        seg = [f"更新于 {last}" if last else "缓存未就绪",
               f"{accounts_n} 账号", f"{items_n} 商品", f"IP池 {pool_n}"]
        if refreshing:
            seg.append("正在刷新…")
        self.lbl_status.configure(text=" · ".join(seg))

    def _on_items(self, data):
        items = data.get("items", [])
        # 赞数增量(对比客户端上次拉到的值,重启后归零);存全量,筛选/排序本地做
        for it in items:
            iid = it.get("item_id")
            if iid is None:
                continue
            prev = self._prev_fav.get(iid)
            it["fav_delta"] = (it.get("favourite_count", 0) - prev) if prev is not None else None
            self._prev_fav[iid] = it.get("favourite_count", 0)
        self._all_items = items
        self._apply_view()

    def _apply_view(self):
        """按当前状态筛选 + 上架时间排序,重画表格。"""
        self.tree.delete(*self.tree.get_children())
        self._item_urls.clear()
        self._item_ids.clear()
        want = {"全部": "all", "在售": "visible", "已售": "sold", "被预留": "reserved",
                "草稿": "draft", "已隐藏": "hidden"}.get(self.status_var.get(), "all")
        items = [it for it in self._all_items
                 if want == "all" or it.get("status") == want]
        # 上架时间排序:最新在前(reverse)或最早上架在前
        items.sort(key=lambda r: (r.get("created_at_ts") or 0), reverse=not self._sort_earliest)
        for it in items:
            status = it.get("status")
            fav = it.get("favourite_count", 0)
            d = it.get("fav_delta")
            fav_txt = str(fav)
            if d is not None and d > 0:
                fav_txt = f"{fav} +{d}"
            elif d is not None and d < 0:
                fav_txt = f"{fav} {d}"
            # 一次只上一个前景色 tag(优先已售 > 状态 > 赞变化),避免颜色冲突
            is_new = it.get("fav_delta") is None
            if status == "sold":
                tags = ("sold",)
            elif status == "reserved":
                tags = ("reserved",)
            elif is_new:
                tags = ("new",)
            elif d is not None and d > 0:
                tags = ("fav_up",)
            elif d is not None and d < 0:
                tags = ("fav_down",)
            else:
                tags = ()
            price = it.get("price")
            price_txt = f"{price:.2f} {it.get('currency', '')}".strip() if isinstance(price, (int, float)) else ""
            ts = it.get("created_at_ts")
            time_txt = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else ""
            status_zh = it.get("status_zh") or STATUS_ZH.get(status, status)
            iid = self.tree.insert("", "end",
                                   values=(it.get("title", ""), price_txt,
                                           fav_txt,
                                           status_zh,
                                           time_txt),
                                   tags=tuple(tags))
            self._item_urls[iid] = it.get("url", "")
            self._item_ids[iid] = it.get("item_id")

    def _on_items_error(self, resp):
        msg = "拉取商品失败"
        if resp and isinstance(resp, dict):
            err = resp.get("detail", {}).get("error", {})
            msg = err.get("message", msg) if isinstance(err, dict) else str(resp.get("detail"))
        self.lbl_status.configure(text=msg[:80])

    def _on_conn_error(self, msg):
        self._connected = False
        self.light.configure(text="●", text_color="#ff5555")
        self.lbl_status.configure(text=msg)

    # ============================ 操作 ============================
    def _manual_refresh(self):
        def run():
            try:
                r = requests.post(self.server_url.rstrip("/") + "/api/refresh", timeout=15)
                if r.status_code == 202:
                    self.q.put(("flash", "已触发全量刷新"))
                elif r.status_code == 409:
                    self.q.put(("flash", "上一轮刷新还在进行中"))
                else:
                    self.q.put(("flash", f"刷新失败 HTTP {r.status_code}"))
            except requests.exceptions.RequestException:
                self.q.put(("flash", "刷新失败:无法连接服务器"))
        threading.Thread(target=run, daemon=True).start()

    def _add_accounts(self):
        raw = self.txt_add.get("1.0", "end").strip()
        accounts = [ln.strip() for ln in raw.splitlines() if ln.strip()]
        if not accounts:
            self._flash("请先粘贴要添加的账号")
            return

        def run():
            try:
                r = requests.post(self.server_url.rstrip("/") + "/api/accounts",
                                  json={"accounts": accounts}, timeout=60)
                if r.status_code == 200:
                    d = r.json()
                    msg = f"添加完成:新增 {d.get('added', 0)},失败 {d.get('failed', 0)},跳过 {d.get('skipped', 0)}"
                    failed = [x for x in d.get("results", []) if not x.get("ok")]
                    if failed:
                        msg += "  ·  " + failed[0].get("error", "")[:60]
                    self.q.put(("flash", msg))
                    # 触发一轮全量刷新,尽快把新账号的数据抓进来
                    try:
                        requests.post(self.server_url.rstrip("/") + "/api/refresh", timeout=15)
                    except requests.exceptions.RequestException:
                        pass
                else:
                    self.q.put(("flash", f"添加失败 HTTP {r.status_code}"))
            except requests.exceptions.RequestException:
                self.q.put(("flash", "添加失败:无法连接服务器"))
        threading.Thread(target=run, daemon=True).start()

    def _delete_by_input(self):
        account = self.del_var.get().strip().strip('"\'')
        if not account:
            self._flash("请先输入要删除的账号")
            return
        if not messagebox.askyesno("删除账号", f"确认把账号 {account} 从监控清单移除?"):
            return

        def run():
            try:
                r = requests.delete(self.server_url.rstrip("/") + "/api/accounts/" +
                                    requests.utils.quote(account, safe=""), timeout=15)
                if r.status_code == 200:
                    self.q.put(("flash", f"已删除 {account}"))
                    self._dirty = True          # 删除后立刻重拉列表
                else:
                    self.q.put(("flash", f"删除失败 HTTP {r.status_code}"))
            except requests.exceptions.RequestException:
                self.q.put(("flash", "删除失败:无法连接服务器"))
        threading.Thread(target=run, daemon=True).start()

    def _on_double_click(self, _event):
        iid = self.tree.focus()
        url = self._item_urls.get(iid)
        if url:
            webbrowser.open(url)

    def _toggle_sort(self):
        """切换上架时间排序方向:最新在前 ↔ 最早在前。"""
        self._sort_earliest = not self._sort_earliest
        self.btn_sort.configure(
            text="上架时间: 最早在前" if self._sort_earliest else "上架时间: 最新在前")
        self._apply_view()

    def _delete_selected_item(self):
        """删除选中商品(已售的删掉后不占位;未售的也允许删)。"""
        iid = self.tree.focus()
        item_id = self._item_ids.get(iid)
        if item_id is None:
            self._flash("请先选中要删除的商品")
            return
        vals = self.tree.item(iid, "values")
        title = vals[0][:30] if vals and vals[0] else str(item_id)
        if not messagebox.askyesno("删除商品", f"确认删除商品「{title}」?\n删除后不再出现在列表,已售/未售都会从库里移除。"):
            return
        base = self.server_url.rstrip("/")

        def run():
            try:
                r = requests.delete(base + f"/api/items/{item_id}", timeout=15)
                if r.status_code in (200, 404):
                    self.q.put(("flash", f"已删除商品 {item_id}"))
                    self._dirty = True          # 重拉列表(服务器已过滤已删商品)
                else:
                    self.q.put(("flash", f"删除失败 HTTP {r.status_code}"))
            except requests.exceptions.RequestException:
                self.q.put(("flash", "删除失败:无法连接服务器"))
        threading.Thread(target=run, daemon=True).start()

    def _delete_all_sold(self):
        """一键删除全部已售商品(未售保留)。"""
        sold_n = sum(1 for it in self._all_items if it.get("status") == "sold")
        if sold_n == 0:
            self._flash("当前没有已售商品")
            return
        if not messagebox.askyesno("删除全部已售",
                                   f"确认删除全部 {sold_n} 件已售商品?\n未售出的商品会保留。"):
            return
        base = self.server_url.rstrip("/")

        def run():
            try:
                r = requests.post(base + "/api/items/delete-sold", timeout=30)
                if r.status_code == 200:
                    n = r.json().get("deleted", 0)
                    self.q.put(("flash", f"已删除 {n} 件已售商品"))
                    self._dirty = True
                else:
                    self.q.put(("flash", f"删除失败 HTTP {r.status_code}"))
            except requests.exceptions.RequestException:
                self.q.put(("flash", "删除失败:无法连接服务器"))
        threading.Thread(target=run, daemon=True).start()

    # ============================ 杂项 ============================
    def _flash(self, text):
        self.lbl_status.configure(text=text)  # 下轮 status 轮询(≤15s)会覆盖回实时信息

    def _load_config(self):
        cfg = {}
        try:
            if CONFIG_PATH.exists():
                p = configparser.ConfigParser()
                p.read(CONFIG_PATH, encoding="utf-8")
                cfg = dict(p["client"])
        except Exception:
            pass
        return cfg

    def _save_config(self, url):
        try:
            CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
            p = configparser.ConfigParser()
            p["client"] = {"server_url": url}
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                p.write(f)
        except Exception:
            pass  # 写不了也不影响使用

    def _on_close(self):
        self._running = False
        try:
            self.destroy()
        except Exception:
            pass


def main():
    app = VintedClient()
    app.protocol("WM_DELETE_WINDOW", app._on_close)
    app.mainloop()


if __name__ == "__main__":
    main()
