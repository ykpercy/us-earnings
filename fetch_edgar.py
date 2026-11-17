#!/usr/bin/env python3
"""
从 SEC EDGAR 批量数据（XBRL companyfacts）生成美股全市场最新季度财报摘要 data.json。

用法：
    export SEC_USER_AGENT="你的名字 your@email.com"   # SEC 要求声明身份，否则会被拒绝
    python3 fetch_edgar.py                              # 自动下载并生成 data.json
    python3 fetch_edgar.py --facts companyfacts.zip --subs submissions.zip   # 使用本地已下载文件

数据源（均为 SEC 官方免费数据，每晚更新）：
    companyfacts.zip   全部公司的 XBRL 财报事实（约 1GB+）
    submissions.zip    公司资料，用于取行业（SIC）分类（可用 --no-industry 跳过）
    company_tickers_exchange.json   代码与交易所，用于只保留当前上市公司

输出字段（金额单位：美元；增长率单位：%）：
    t 代码, n 名称, ex 交易所, ind 行业, cq 日历季度(如 2026Q3), end 报告期末, d 公告日期,
    eps 每股收益(稀释优先), rev 营业总收入, revY 同比, revQ 环比, ni 净利润, niY 同比, niQ 环比,
    bvps 每股净资产, roe 净资产收益率(年初至今净利润/期末净资产), ocf 每股经营现金流(年初至今),
    gm 销售毛利率(本季度)
说明：Q4 没有单独的 10-Q，脚本用「全年 − 前三季度累计」推算。
"""
import argparse, json, os, re, sys, time, urllib.request, zipfile
from datetime import date, datetime, timedelta, timezone

FACTS_URL = "https://www.sec.gov/Archives/edgar/daily-index/xbrl/companyfacts.zip"
SUBS_URL = "https://www.sec.gov/Archives/edgar/daily-index/bulkdata/submissions.zip"
TICKERS_URL = "https://www.sec.gov/files/company_tickers_exchange.json"

REV = ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax",
       "RevenueFromContractWithCustomerIncludingAssessedTax", "SalesRevenueNet", "SalesRevenueGoodsNet"]
NI = ["NetIncomeLoss", "ProfitLoss"]
EPS_D = ["EarningsPerShareDiluted", "EarningsPerShareBasicAndDiluted"]
EPS_B = ["EarningsPerShareBasic"]
GP = ["GrossProfit"]
COGS = ["CostOfRevenue", "CostOfGoodsAndServicesSold", "CostOfGoodsSold"]
OCF = ["NetCashProvidedByUsedInOperatingActivities"]
EQ = ["StockholdersEquity", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"]
SH_DUR = ["WeightedAverageNumberOfSharesOutstandingBasic"]


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def http_get(url, ua):
    return urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": ua}), timeout=120)


def download(url, dest, ua):
    log(f"下载 {url}")
    with http_get(url, ua) as r, open(dest, "wb") as f:
        total, done = int(r.headers.get("Content-Length") or 0), 0
        while True:
            b = r.read(1 << 20)
            if not b:
                break
            f.write(b)
            done += len(b)
            if total:
                print(f"\r  {done / total:.0%}", end="", file=sys.stderr)
    print(file=sys.stderr)


def D(s):
    return date.fromisoformat(s)


# ---------- 事实提取 ----------
def durations(facts, concepts, unit="USD", ns="us-gaap"):
    """区间型事实 -> [(start, end, val, filed)]；概念按优先级，同一区间取最新申报。"""
    best = {}
    for rank, c in enumerate(concepts):
        for e in facts.get(ns, {}).get(c, {}).get("units", {}).get(unit, []):
            if "start" not in e or not str(e.get("form", "")).startswith(("10-Q", "10-K")):
                continue
            k = (e["start"], e["end"])
            old = best.get(k)
            if old is None or rank < old[0] or (rank == old[0] and e["filed"] > old[1]["filed"]):
                best[k] = (rank, e)
    return [(D(k[0]), D(k[1]), v[1]["val"], v[1]["filed"]) for k, v in best.items()]


def instants(facts, concepts, unit="USD", ns="us-gaap"):
    best = {}
    for rank, c in enumerate(concepts):
        for e in facts.get(ns, {}).get(c, {}).get("units", {}).get(unit, []):
            if "start" in e or not str(e.get("form", "")).startswith(("10-Q", "10-K")):
                continue
            old = best.get(e["end"])
            if old is None or rank < old[0] or (rank == old[0] and e["filed"] > old[1]["filed"]):
                best[e["end"]] = (rank, e)
    return [(D(k), v[1]["val"], v[1]["filed"]) for k, v in best.items()]


def quarter(ds, end, tol=10):
    """end 附近结束的单季值；没有则用 全年 − 前三季度累计 推算 Q4。返回 (val, end, filed) 或 None。"""
    c = [x for x in ds if abs((x[1] - end).days) <= tol and 75 <= (x[1] - x[0]).days <= 105]
    if c:
        x = min(c, key=lambda x: abs((x[1] - end).days))
        return x[2], x[1], x[3]
    for a in (x for x in ds if abs((x[1] - end).days) <= tol and 340 <= (x[1] - x[0]).days <= 380):
        ytd = [y for y in ds if y[0] == a[0] and 255 <= (y[1] - y[0]).days <= 290 and 60 <= (a[1] - y[1]).days <= 105]
        if ytd:
            y = max(ytd, key=lambda y: y[1])
            return a[2] - y[2], a[1], a[3]
    return None


def ytd(ds, end, tol=10):
    """截至 end 的年初至今累计（取同一会计年度起点、最长且不超过一年的区间）。"""
    c = [x for x in ds if abs((x[1] - end).days) <= tol and 75 <= (x[1] - x[0]).days <= 380]
    return max(c, key=lambda x: (x[1] - x[0]).days)[2] if c else None


def at(inst, end, tol=10):
    c = [x for x in inst if abs((x[0] - end).days) <= tol]
    return min(c, key=lambda x: abs((x[0] - end).days))[1] if c else None


def growth(cur, prev):
    if cur is None or prev is None or prev == 0:
        return None
    return round((cur - prev) / abs(prev) * 100, 2)


def latest_end(*series):
    cands = set()
    for ds in series:
        for x in ds:
            n = (x[1] - x[0]).days
            if 75 <= n <= 105 or 340 <= n <= 380:
                cands.add(x[1])
    for e in sorted(cands, reverse=True):
        if any(quarter(ds, e, 3) for ds in series):
            return e
    return None


def cal_quarter(end):
    d = end - timedelta(days=15)  # 52/53 周会计年度的季末可能落在下月初
    return f"{d.year}Q{(d.month - 1) // 3 + 1}"


def shares_out(facts, ds_basic, end):
    best = None
    for e in facts.get("dei", {}).get("EntityCommonStockSharesOutstanding", {}).get("units", {}).get("shares", []):
        if not str(e.get("form", "")).startswith(("10-Q", "10-K")):
            continue
        f = D(e["filed"])
        if end <= f <= end + timedelta(days=130) and (best is None or f < best[0]):
            best = (f, e["val"])
    if best:
        return best[1]
    q = quarter(ds_basic, end)
    return q[0] if q else None


def build_row(cik, doc):
    facts = doc.get("facts", {})
    rev, ni = durations(facts, REV), durations(facts, NI)
    end = latest_end(rev, ni)
    if not end:
        return None
    r, n = quarter(rev, end, 3), quarter(ni, end, 3)
    if not (r or n):
        return None
    py, pq = end - timedelta(days=364), end - timedelta(days=91)
    r_y, r_q = quarter(rev, py), quarter(rev, pq, 12)
    n_y, n_q = quarter(ni, py), quarter(ni, pq, 12)

    eps_ds = durations(facts, EPS_D, "USD/shares") or durations(facts, EPS_B, "USD/shares")
    eps_b = durations(facts, EPS_B, "USD/shares")
    e = quarter(eps_ds, end, 3) or quarter(eps_b, end, 3)

    gp = quarter(durations(facts, GP), end, 3)
    gm = None
    if r and r[0] > 0:
        if gp:
            gm = round(gp[0] / r[0] * 100, 2)
        else:
            cg = quarter(durations(facts, COGS), end, 3)
            if cg:
                gm = round((r[0] - cg[0]) / r[0] * 100, 2)

    equity = at(instants(facts, EQ), end)
    sh = shares_out(facts, durations(facts, SH_DUR, "shares"), end)
    ytd_ni = ytd(ni, end)
    ytd_ocf = ytd(durations(facts, OCF), end)

    filed = max(x[2] for x in (r, n) if x)
    return {
        "cik": cik, "cq": cal_quarter(end), "end": end.isoformat(), "d": filed,
        "eps": e[0] if e else None,
        "rev": r[0] if r else None, "revY": growth(r and r[0], r_y and r_y[0]), "revQ": growth(r and r[0], r_q and r_q[0]),
        "ni": n[0] if n else None, "niY": growth(n and n[0], n_y and n_y[0]), "niQ": growth(n and n[0], n_q and n_q[0]),
        "bvps": round(equity / sh, 2) if equity is not None and sh else None,
        "roe": round(ytd_ni / equity * 100, 2) if ytd_ni is not None and equity and equity > 0 else None,
        "ocf": round(ytd_ocf / sh, 4) if ytd_ocf is not None and sh else None,
        "gm": gm,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--facts"), ap.add_argument("--subs"), ap.add_argument("--tickers")
    ap.add_argument("--out", default="data.json")
    ap.add_argument("--no-industry", action="store_true", help="不下载 submissions.zip（行业列为空）")
    ap.add_argument("--include-otc", action="store_true", help="包含场外（OTC）股票")
    ap.add_argument("--max-age-days", type=int, default=400, help="忽略报告期早于此天数的公司")
    a = ap.parse_args()

    ua = os.environ.get("SEC_USER_AGENT", "")
    need_net = not (a.facts and (a.subs or a.no_industry) and a.tickers)
    if need_net and not re.search(r"\S+@\S+", ua):
        sys.exit('请先设置：export SEC_USER_AGENT="你的名字 your@email.com"（SEC 要求）')

    # 1) 当前上市公司
    if a.tickers:
        tk = json.load(open(a.tickers))
    else:
        tk = json.load(http_get(TICKERS_URL, ua))
    fields = tk["fields"]
    listed = {}
    for row in tk["data"]:
        d = dict(zip(fields, row))
        ex = (d.get("exchange") or "")
        if not a.include_otc and ex.upper() in ("OTC", ""):
            continue
        listed.setdefault(int(d["cik"]), (d["ticker"], d["name"], ex))
    log(f"当前上市公司：{len(listed)}")

    # 2) 行业
    industry = {}
    if not a.no_industry:
        subs = a.subs or "submissions.zip"
        if not a.subs:
            download(SUBS_URL, subs, ua)
        with zipfile.ZipFile(subs) as z:
            for name in z.namelist():
                m = re.fullmatch(r"CIK(\d{10})\.json", name)
                if m and int(m.group(1)) in listed:
                    industry[int(m.group(1))] = json.load(z.open(name)).get("sicDescription") or ""
        log(f"行业信息：{len(industry)}")

    # 3) 财报事实
    facts_zip = a.facts or "companyfacts.zip"
    if not a.facts:
        download(FACTS_URL, facts_zip, ua)
    rows, today = [], date.today()
    with zipfile.ZipFile(facts_zip) as z:
        for name in z.namelist():
            m = re.fullmatch(r"CIK(\d{10})\.json", name)
            if not m or int(m.group(1)) not in listed:
                continue
            cik = int(m.group(1))
            try:
                row = build_row(cik, json.load(z.open(name)))
            except Exception as ex:  # 单个公司数据异常不应中断全量
                log(f"跳过 {name}: {ex}")
                continue
            if not row or (today - D(row["end"])).days > a.max_age_days:
                continue
            t, n, ex = listed[cik]
            row.update({"t": t, "n": n, "ex": ex, "ind": industry.get(cik, "")})
            row.pop("cik")
            rows.append(row)

    out = {"updated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"), "count": len(rows), "rows": rows}
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    log(f"完成：{len(rows)} 家公司 -> {a.out}")


if __name__ == "__main__":
    main()
