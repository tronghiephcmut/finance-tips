#!/usr/bin/env python3
"""
fetch_data.py - lấy dữ liệu thị trường từ nguồn miễn phí và đổ vào index.html

Cách dùng:
    pip install yfinance feedparser requests
    python fetch_data.py                 # lấy dữ liệu thật, ghi index.html
    python fetch_data.py --sample        # dùng sample_data.json (xem thử giao diện)
    python fetch_data.py --out out.html  # ghi ra file khác

Mỗi khối dữ liệu có một "provider" chọn trong sources.json. Muốn thêm nguồn mới:
viết một hàm nhận (cfg) trả về dict, rồi đăng ký vào PROVIDERS.
Mọi provider đều được bọc try/except: nguồn nào lỗi thì khối đó để trống,
trang vẫn được tạo và ghi rõ nguồn lỗi ở phần "sources".
"""
import argparse, json, math, os, re, sys, datetime as dt

HERE = os.path.dirname(os.path.abspath(__file__))
def load(name):
    with open(os.path.join(HERE, name), encoding="utf-8") as f:
        return json.load(f)

ERRORS = []
def safe(name, fn, *a, **k):
    try:
        return fn(*a, **k)
    except Exception as e:  # noqa
        ERRORS.append(f"{name}: {type(e).__name__}: {e}")
        return None

def sanitize_json(obj):
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return obj
    elif isinstance(obj, dict):
        return {k: sanitize_json(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [sanitize_json(x) for x in obj]
    return obj

# ----------------------------------------------------------------- Yahoo Finance
def yahoo_quotes(cfg):
    """Trả về {label: {value, pct, change}} cho danh sách ticker."""
    import yfinance as yf
    out = {}
    for label, sym in cfg["symbols"].items():
        h = yf.Ticker(sym).history(period="5d", interval="1d")
        closes = h["Close"].dropna() if "Close" in h and not h.empty else []
        if len(closes) < 2:
            continue
        last, prev = float(closes.iloc[-1]), float(closes.iloc[-2])
        if math.isnan(last) or math.isnan(prev):
            continue
        chg = last - prev
        pct = (last / prev - 1) * 100 if prev != 0 else 0
        out[label] = {"label": label, "value": round(last, 2),
                      "change": round(chg, 2),
                      "pct": round(pct, 2)}
    return out

# ----------------------------------------------------------------- Chỉ số VN: nhiều nguồn, thử lần lượt
_UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124 Safari/537.36",
       "Accept": "application/json, text/plain, */*", "Accept-Language": "vi,en;q=0.9"}
TIMEOUT = 12

def _pack(ts, closes, vols, days, label):
    closes = [float(x) for x in closes][-days:]
    ts = list(ts)[-days:]
    vols = list(vols or [])[-days:]
    if len(closes) < 2:
        raise RuntimeError("ít hơn 2 phiên")
    dates = [dt.datetime.fromtimestamp(int(t)).strftime("%d.%m") for t in ts]
    last, prev = closes[-1], closes[-2]
    return {"index": {"label": label, "value": round(last, 2),
                      "change": round(last - prev, 2), "pct": round((last / prev - 1) * 100, 2)},
            "history": [{"d": d, "v": round(v, 2)} for d, v in zip(dates, closes)],
            "date": dates[-1], "volume": float(vols[-1]) if vols else None}

def _snip(r):
    return r.text[:120].replace("\n", " ")

def idx_tcbs(sym, days, frm, to):
    import requests
    r = requests.get("https://apipubaws.tcbs.com.vn/stock-insight/v2/stock/bars-long-term",
                     params={"ticker": sym, "type": "index", "resolution": "D", "from": frm, "to": to},
                     headers=_UA, timeout=TIMEOUT)
    j = r.json()
    rows = j.get("data") or []
    if not rows:
        raise RuntimeError("TCBS trống: " + _snip(r))
    ts = [int(dt.datetime.fromisoformat(x["tradingDate"][:19]).timestamp()) for x in rows]
    return _pack(ts, [x["close"] for x in rows], [x.get("volume") for x in rows], days, sym)

def idx_vietcap(sym, days, frm, to):
    import requests
    r = requests.post("https://trading.vietcap.com.vn/api/chart/OHLCChart/gap-chart",
                      json={"timeFrame": "ONE_DAY", "symbols": [sym], "from": frm, "to": to},
                      headers={**_UA, "Content-Type": "application/json"}, timeout=TIMEOUT)
    j = r.json()
    d = j[0] if isinstance(j, list) else j
    if not d.get("c"):
        raise RuntimeError("Vietcap trống: " + _snip(r))
    return _pack(d["t"], d["c"], d.get("v"), days, sym)

def idx_ssi(sym, days, frm, to):
    import requests
    r = requests.get("https://iboard-api.ssi.com.vn/statistics/charts/history",
                     params={"resolution": "1D", "symbol": sym, "from": frm, "to": to},
                     headers=_UA, timeout=TIMEOUT)
    j = r.json()
    d = j.get("data", j)
    if not d.get("c"):
        raise RuntimeError("SSI trống: " + _snip(r))
    return _pack(d["t"], d["c"], d.get("v"), days, sym)

def idx_vndirect(sym, days, frm, to):
    import requests
    r = requests.get("https://dchart-api.vndirect.com.vn/dchart/history",
                     params={"resolution": "D", "symbol": sym, "from": frm, "to": to},
                     headers=_UA, timeout=TIMEOUT)
    j = r.json()
    if not j.get("c"):
        raise RuntimeError("VNDirect trống: " + _snip(r))
    return _pack(j["t"], j["c"], j.get("v"), days, sym)

INDEX_SOURCES = {"tcbs": idx_tcbs, "vietcap": idx_vietcap, "ssi": idx_ssi, "vndirect": idx_vndirect}

def vn_index_auto(cfg):
    """Thử lần lượt các nguồn trong cfg["order"]; nguồn nào trả dữ liệu thì dùng."""
    import time
    sym = cfg.get("symbol", "VNINDEX")
    days = cfg.get("history_days", 6)
    now = int(time.time())
    frm = now - 86400 * (days * 2 + 5)
    errs = []
    for name in cfg.get("order", list(INDEX_SOURCES)):
        try:
            res = INDEX_SOURCES[name](sym, days, frm, now)
            res["index"]["label"] = "VN-Index" if sym == "VNINDEX" else sym
            res["source"] = name
            return res
        except Exception as e:  # noqa
            errs.append(f"{name}: {type(e).__name__}: {str(e)[:100]}")
    raise RuntimeError(" | ".join(errs))

# ----------------------------------------------------------------- Khối ngoại
def foreign_manual(cfg):
    """Nhập tay trong manual_macro.json (mục "foreign") khi không có API miễn phí ổn định."""
    m = load(cfg.get("file", "manual_macro.json")).get("foreign")
    if not m:
        raise RuntimeError("chưa có mục 'foreign' trong manual_macro.json")
    return m

def foreign_ssi(cfg):
    """Thử lấy bảng giá HOSE từ SSI iBoard và tìm cột giá trị ròng khối ngoại (tên trường có thể đổi)."""
    import requests
    top = cfg.get("top", 3)
    r = requests.get("https://iboard-query.ssi.com.vn/stock/exchange/hose", headers=_UA, timeout=TIMEOUT)
    rows = r.json().get("data") or []
    if not rows:
        raise RuntimeError("SSI trống: " + _snip(r))
    keys = rows[0].keys()
    buy_k = next((k for k in keys if k.lower() in ("fbvalue", "fbval", "foreignbuyvalue")), None)
    sell_k = next((k for k in keys if k.lower() in ("fsvalue", "fsval", "foreignsellvalue")), None)
    if not (buy_k and sell_k):
        raise RuntimeError("không thấy cột khối ngoại; các cột: " + ", ".join(list(keys)[:30]))
    data = [(x.get("ss") or x.get("symbol"), (float(x[buy_k] or 0) - float(x[sell_k] or 0)) / 1e9) for x in rows]
    data.sort(key=lambda x: x[1])
    return {"sell": [{"code": c, "value": round(v, 0)} for c, v in data[:top]],
            "buy": [{"code": c, "value": round(v, 0)} for c, v in data[::-1][:top]],
            "total": round(sum(v for _, v in data), 0)}

def foreign_auto(cfg):
    errs = []
    for name in cfg.get("order", ["ssi", "manual"]):
        fn = {"ssi": foreign_ssi, "manual": foreign_manual}[name]
        try:
            return fn(cfg)
        except Exception as e:  # noqa
            errs.append(f"{name}: {str(e)[:120]}")
    raise RuntimeError(" | ".join(errs))

# ----------------------------------------------------------------- vnstock (tùy chọn; gói đã rời PyPI, cài theo hướng dẫn của vnstock.vn)
def vnstock_index(cfg):
    from vnstock import Vnstock
    days = cfg.get("history_days", 6)
    end = dt.date.today()
    start = end - dt.timedelta(days=days * 2 + 3)
    st = Vnstock().stock(symbol="VNINDEX", source=cfg.get("source", "VCI"))
    df = st.quote.history(start=str(start), end=str(end), interval="1D").tail(days)
    df = df.rename(columns=str.lower)
    closes = df["close"].astype(float).tolist()
    dates = [dt.datetime.fromisoformat(str(x)[:10]).strftime("%d.%m") for x in df["time"]]
    last, prev = closes[-1], closes[-2]
    vol_val = None
    if "volume" in df:
        vol_val = float(df["volume"].iloc[-1])
    return {
        "index": {"label": "VN-Index", "value": round(last, 2),
                  "change": round(last - prev, 2), "pct": round((last / prev - 1) * 100, 2)},
        "history": [{"d": d, "v": round(v, 2)} for d, v in zip(dates, closes)],
        "date": dates[-1],
        "volume": vol_val,
    }

def vnstock_foreign(cfg):
    """Top mua/bán ròng khối ngoại. API thay đổi theo phiên bản vnstock,
    nên bọc kỹ; nếu không có thì để trống và ghi chú."""
    from vnstock import Vnstock
    top = cfg.get("top", 3)
    tr = Vnstock().stock(symbol="VNINDEX", source=cfg.get("source", "VCI")).trading
    # vnstock >=3.x: trading.foreign_trade() hoặc screener; thử lần lượt
    for name in ("foreign_trade", "price_board"):
        fn = getattr(tr, name, None)
        if not fn:
            continue
        try:
            df = fn()
        except Exception:
            continue
        cols = [c for c in df.columns if "foreign" in str(c).lower() and "net" in str(c).lower()]
        if not cols:
            continue
        col = cols[0]
        df = df[[c for c in df.columns if str(c).lower() in ("symbol", "ticker")][0:1] + [col]]
        df.columns = ["code", "value"]
        df["value"] = df["value"].astype(float) / 1e9  # -> tỷ đồng
        df = df.sort_values("value")
        sell = [{"code": r.code, "value": round(r.value, 0)} for r in df.head(top).itertuples()]
        buy = [{"code": r.code, "value": round(r.value, 0)} for r in df.tail(top)[::-1].itertuples()]
        return {"buy": buy, "sell": sell, "total": round(float(df["value"].sum()), 0)}
    raise RuntimeError("phiên bản vnstock này không có dữ liệu khối ngoại; đổi provider hoặc nhập tay")

# ----------------------------------------------------------------- Tỷ giá, vàng
def vietcombank_fx(cfg):
    import requests, xml.etree.ElementTree as ET
    r = requests.get("https://portal.vietcombank.com.vn/Usercontrols/TVPortal.TyGia/pXML.aspx", timeout=20)
    root = ET.fromstring(r.content)
    for e in root.iter("Exrate"):
        if e.get("CurrencyCode") == "USD":
            return {"usd_buy": float(e.get("Buy").replace(",", "")),
                    "usd_sell": float(e.get("Sell").replace(",", ""))}
    raise RuntimeError("không thấy USD trong XML")

def _gold_sjc_api():
    import requests
    r = requests.post("https://sjc.com.vn/GoldPrice/Services/PriceService.ashx",
                      headers={**_UA, "X-Requested-With": "XMLHttpRequest"}, timeout=TIMEOUT)
    rows = r.json().get("data") or []
    row = next((x for x in rows if "1L" in str(x.get("TypeName", ""))), rows[0] if rows else None)
    if not row:
        raise RuntimeError("không có dòng")
    return {"buy": float(row["BuyValue"]) / 1e6, "sell": float(row["SellValue"]) / 1e6, "src": "SJC"}

def _gold_btmc():
    """Bảo Tín Minh Châu - API JSON công khai, vào được từ nước ngoài, có cả giá SJC."""
    import requests
    r = requests.get("http://api.btmc.vn/api/BTMCAPI/getpricebtmc",
                     params={"key": "3kd8ub1llcg9t45hnoh8hmn7t5kc2v"}, headers=_UA, timeout=TIMEOUT)
    rows = r.json().get("DataList", {}).get("Data", [])
    def field(x, name):  # các khóa dạng "@n_1", "@pb_1"...
        return next((v for k, v in x.items() if k.startswith("@" + name + "_")), None)
    sjc = next((x for x in rows if "SJC" in str(field(x, "n")).upper()), None)
    if not sjc:
        raise RuntimeError("BTMC không có dòng SJC")
    return {"buy": float(field(sjc, "pb")) / 1e6, "sell": float(field(sjc, "ps")) / 1e6, "src": "BTMC"}

def _gold_pnj():
    import requests
    r = requests.get("https://giavang.pnj.com.vn/", headers=_UA, timeout=TIMEOUT)
    m = re.search(r"SJC.{0,400}?(\d{3}[.,]\d{3})[^\d]{1,80}(\d{3}[.,]\d{3})", r.text, re.S)
    if not m:
        raise RuntimeError("PNJ không parse được")
    f = lambda x: float(x.replace(".", "").replace(",", "")) / 1000   # 140.000 (nghìn đ/chỉ) -> triệu/lượng
    return {"buy": f(m.group(1)), "sell": f(m.group(2)), "src": "PNJ"}

def _gold_manual():
    m = load("manual_macro.json").get("vn_gold")
    if not m:
        raise RuntimeError("chưa có mục vn_gold trong manual_macro.json")
    return {"buy": m["buy"], "sell": m["sell"], "src": "nhập tay"}

GOLD_SOURCES = {"sjc": _gold_sjc_api, "btmc": _gold_btmc, "pnj": _gold_pnj, "manual": _gold_manual}

def sjc_gold(cfg):
    """Giá vàng SJC 1 lượng (triệu đồng). Thử lần lượt theo cfg["order"]."""
    errs = []
    for name in cfg.get("order", ["sjc", "btmc", "pnj", "manual"]):
        try:
            return GOLD_SOURCES[name]()
        except Exception as e:  # noqa
            errs.append(f"{name}: {type(e).__name__}")
    raise RuntimeError("; ".join(errs))

# ----------------------------------------------------------------- FRED (API key tùy chọn → CSV → nhập tay)
def fred_series(cfg):
    import requests, os
    errs = []
    key = cfg.get("api_key") or os.environ.get("FRED_API_KEY")
    start = (dt.date.today() - dt.timedelta(days=cfg.get("days", 90))).isoformat()
    rows = []
    if key:
        try:
            r = requests.get("https://api.stlouisfed.org/fred/series/observations",
                             params={"series_id": cfg["series"], "api_key": key, "file_type": "json",
                                     "observation_start": start}, headers=_UA, timeout=cfg.get("timeout", 20))
            rows = [(o["date"], float(o["value"])) for o in r.json().get("observations", []) if o["value"] != "."]
        except Exception as e:  # noqa
            errs.append(f"api: {type(e).__name__}")
    if len(rows) < 2:
        try:
            r = requests.get("https://fred.stlouisfed.org/graph/fredgraph.csv",
                             params={"id": cfg["series"], "cosd": start}, headers=_UA, timeout=cfg.get("timeout", 20))
            for line in r.text.strip().splitlines()[1:]:
                d, v = line.split(",")[:2]
                if v not in (".", ""):
                    rows.append((d, float(v)))
        except Exception as e:  # noqa
            errs.append(f"csv: {type(e).__name__}")
    if len(rows) >= 2:
        (d1, v1), (d0, v0) = rows[-1], rows[-2]
        return {"label": cfg.get("label", cfg["series"]), "value": v1, "decimals": 0,
                "pct": round((v1 / v0 - 1) * 100, 2), "sub": d1}
    m = load("manual_macro.json").get("us_m2")
    if m:
        return {"label": cfg.get("label", cfg["series"]), "decimals": 0, **m}
    raise RuntimeError("; ".join(errs) + "; chưa có mục us_m2 trong manual_macro.json")

# ----------------------------------------------------------------- RSS
def rss_news(cfg):
    import feedparser
    items = []
    for feed in cfg["feeds"]:
        d = feedparser.parse(feed["url"])
        for e in d.entries[: cfg.get("max_per_feed", 3)]:
            items.append({"title": e.get("title", ""), "url": e.get("link", ""), "source": feed["name"]})
    return items

# ----------------------------------------------------------------- Nhận định: theo quy tắc, không cần LLM
def rules_takeaways(data):
    t = []
    m = data.get("vn_market", {})
    idx = m.get("index")
    if idx:
        d = "tăng" if idx["change"] > 0 else "giảm"
        t.append({"title": f"VN-Index {d} {abs(idx['pct']):.2f}%.",
                  "body": f"Đóng cửa {fmt_vn(idx['value'])} điểm, {d} {abs(idx['change']):.2f} điểm so với phiên trước."})
    sell = m.get("foreign_sell") or []
    if sell:
        t.append({"title": "Khối ngoại bán ròng tập trung.",
                  "body": "Bán ròng mạnh nhất: " + ", ".join(f"{s['code']} ({s['value']:.0f} tỷ)" for s in sell) + "."})
    w = data.get("world", {}).get("tiles", [])
    if w:
        sp = next((x for x in w if "S&P" in x["label"]), w[0])
        t.append({"title": f"Phố Wall {'tăng' if sp['pct'] > 0 else 'giảm'}.",
                  "body": f"{sp['label']} {sp['pct']:+.2f}%; theo dõi lợi suất và DXY cho dòng vốn ngoại."})
    return t[:3]

def claude_takeaways(data):
    """Tùy chọn: gọi Claude Haiku một lần/ngày để viết 3 nhận định. Chỉ chạy khi có ANTHROPIC_API_KEY."""
    import anthropic
    client = anthropic.Anthropic()
    slim = {k: data[k] for k in ("world", "commodities", "vn_market", "vn_macro") if k in data}
    msg = client.messages.create(
        model="claude-haiku-4-5", max_tokens=600,
        messages=[{"role": "user", "content":
            "Dựa trên dữ liệu JSON sau, viết đúng 3 nhận định ngắn cho nhà đầu tư Việt Nam. "
            "Trả về JSON thuần: [{\"title\":..., \"body\":...}], không markdown.\n" + json.dumps(slim, ensure_ascii=False)}])
    txt = "".join(b.text for b in msg.content if b.type == "text")
    return json.loads(re.sub(r"```json|```", "", txt).strip())

PROVIDERS = {
    "yahoo": yahoo_quotes,
    "auto_index": vn_index_auto,
    "auto_foreign": foreign_auto,
    "manual_foreign": foreign_manual,
    "vnstock_index": vnstock_index,
    "vnstock_foreign": vnstock_foreign,
    "vietcombank": vietcombank_fx,
    "sjc": sjc_gold,
    "rss": rss_news,
    "fred": fred_series,
}

def fmt_vn(n, d=2):
    s = f"{n:,.{d}f}"
    return s.replace(",", "X").replace(".", ",").replace("X", ".")

def pct_note(label, q):
    return f"{label} {q['pct']:+.2f}%".replace(".", ",")

# ----------------------------------------------------------------- Lắp ráp
def build(cfg):
    today = dt.date.today()
    data = {"meta": {"date": today.strftime("%d.%m.%Y"),
                     "generated_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                     "author": "Bản tin tự động",
                     "note": "Dữ liệu sau đóng cửa phiên Việt Nam gần nhất và phiên Mỹ gần nhất",
                     "sources": [], "alert_pct": cfg.get("alert_pct", 4)},
            "summary": ""}
    used = data["meta"]["sources"]

    def run(block):
        c = cfg[block]
        p = c["provider"]
        key = p if p in PROVIDERS else f"{p}_{re.sub(r'[0-9]+$', '', block.split('_', 1)[1])}"
        fn = PROVIDERS.get(key) or PROVIDERS.get(p)
        if not fn:
            ERRORS.append(f"{block}: provider '{p}' không tồn tại"); return None
        res = safe(block, fn, c)
        if res is not None:
            used.append(f"{block}: {p}")
        return res

    # 1. Thế giới
    w = run("world_indices") or {}
    other = run("world_other") or {}
    rates = run("us_rates") or {}
    m2 = run("us_m2")
    fxw = run("fx_world") or {}
    data["world"] = {"tiles": list(w.values())[:3], "rows": list(other.values()), "notes": []}
    if m2:
        data["world"]["rows"].append(m2)
    if rates:
        data["world"]["notes"].append("Lợi suất: " + "; ".join(
            f"{k} {v['value']:.3f}% ({v['change']*100:+.1f}bp)" for k, v in rates.items()).replace(".", ","))

    # 2. Hàng hóa
    cm = run("commodities") or {}
    cr = run("crypto") or {}
    for k in ("USD/VND",):
        if k in fxw:
            fxw[k]["decimals"] = 0
    for k in ("EUR/USD", "USD/JPY"):
        if k in fxw:
            fxw[k]["decimals"] = 3
    data["commodities"] = {"tiles": list(cm.values())[:6], "rows": list(cr.values()) + list(fxw.values()), "notes": []}
    gold = run("vn_gold")
    if gold:
        data["commodities"]["notes"].append(f"Vàng SJC {fmt_vn(gold['buy'], 1)}–{fmt_vn(gold['sell'], 1)} triệu đồng/lượng (nguồn {gold.get('src', 'SJC')}).")

    # 3. Vĩ mô VN
    fx = run("vn_fx")
    manual = safe("manual_macro", load, cfg["vn_macro_manual"]["file"]) or {}
    tiles = []
    if fx:
        tiles.append({"label": "USD/VND (VCB bán)", "value": fx["usd_sell"], "decimals": 0, "sub": f"mua {fmt_vn(fx['usd_buy'], 0)}"})
    for k in ("cpi_yoy", "gdp"):
        if k in manual:
            tiles.append({**manual[k], "unit": "%"})
    data["vn_macro"] = {"tiles": tiles, "rows": manual.get("rates", {}).get("rows", []), "notes": manual.get("notes", [])}

    # 4. Chứng khoán VN
    vi = run("vn_index") or {}
    vi2 = run("vn_index2") or {}
    fo = run("vn_foreign") or {}
    m = {"index": vi.get("index"), "index2": vi2.get("index"), "history": vi.get("history", []), "tiles": [], "footnote": ""}
    if vi.get("date"):
        data["meta"]["date"] = vi["date"] + "." + str(today.year)
    if vi.get("volume"):
        m["tiles"].append({"label": "Khối lượng HOSE", "text": f"{vi['volume']/1e6:,.0f} triệu cp".replace(",", ".")})
    if fo:
        m["tiles"].append({"label": "Khối ngoại", "text": f"{fo['total']:+,.0f} tỷ".replace(",", "."),
                           "sub": "bán ròng" if fo["total"] < 0 else "mua ròng", "tone": "down" if fo["total"] < 0 else "up"})
        m["foreign_buy"], m["foreign_sell"] = fo["buy"], fo["sell"]
    else:
        m["footnote"] = "Chưa lấy được số liệu khối ngoại từ nguồn tự động."
    data["vn_market"] = m

    # 5 + 6 + tin
    data["strategy"] = manual.get("strategy", {})
    data["news"] = run("news") or []
    tk = cfg.get("takeaways", {}).get("provider", "rules")
    if tk == "claude" and os.environ.get("ANTHROPIC_API_KEY"):
        data["takeaways"] = safe("takeaways_claude", claude_takeaways, data) or rules_takeaways(data)
    else:
        data["takeaways"] = rules_takeaways(data)

    if m.get("index"):
        i = m["index"]
        data["summary"] = (f"VN-Index {'tăng' if i['change']>0 else 'giảm'} {abs(i['change']):.2f} điểm "
                           f"({i['pct']:+.2f}%) còn {fmt_vn(i['value'])}. ").replace(".", ",", 0)
    if ERRORS:
        used.append("LỖI: " + " | ".join(ERRORS))
    return data

def write_html(data, out):
    tpl_path = os.path.join(HERE, "index.html")
    with open(tpl_path, encoding="utf-8") as f:
        html = f.read()
    clean_data = sanitize_json(data)
    js = json.dumps(clean_data, ensure_ascii=False, indent=1, allow_nan=False).replace("</", "<\\/")
    html = re.sub(r"(<!-- DATA_START -->\s*<script id=\"market-data\" type=\"application/json\">)(.*?)(</script>)",
                  lambda mm: mm.group(1) + "\n" + js + "\n" + mm.group(3), html, flags=re.S)
    with open(out, "w", encoding="utf-8") as f:
        f.write(html)

if __name__ == "__main__":
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", action="store_true", help="dùng sample_data.json")
    ap.add_argument("--out", default=os.path.join(HERE, "index.html"))
    ap.add_argument("--dump", help="ghi thêm dữ liệu JSON ra file này")
    a = ap.parse_args()
    data = load("sample_data.json") if a.sample else build(load("sources.json"))
    write_html(data, a.out)
    if a.dump:
        with open(a.dump, "w", encoding="utf-8") as f:
            json.dump(sanitize_json(data), f, ensure_ascii=False, indent=1, allow_nan=False)
    print("Đã ghi", a.out)
    for e in ERRORS:
        print("  cảnh báo:", e, file=sys.stderr)
