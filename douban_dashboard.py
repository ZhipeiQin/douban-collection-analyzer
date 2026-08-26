#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
douban_dashboard.py — 豆瓣书影音记录：抓取 + 分析 dashboard + AI 总结 (Streamlit 应用)

启动:
    streamlit run douban_dashboard.py

依赖 (requirements.txt):
    streamlit  plotly  pandas  requests  beautifulsoup4  lxml

功能:
  * 抓取: 电影 / 音乐 / 书籍 x 想看(听/读) / 在看(听/读) / 看(听/读)过 x 时间范围
    (近3个月 / 近1年 / 近3年 / 历史所有), 边抓边落盘, 断点续抓, 软限流自动重试
  * 数据落在本脚本所在目录, 命名 douban_{类别}_{状态}_{uid}.csv,
    与 douban_export.py 的输出格式完全兼容 (可直接复用已抓的数据)
  * 深度抓取 (电影): 详情页提取导演/类型/IMDb 编号, 进度可断点,
    自动导入已有的 imdb_progress.csv
  * 分析 dashboard: 年份分布 / 评分分布 / 按月标记趋势 / Top 导演·艺术家·作者 /
    Top 类型·标签, 可按时间范围筛选, 可导出筛选后的 CSV
  * AI 总结: 调用 Anthropic API, 基于 dashboard 的统计数据生成文字总结
"""

import csv
import datetime as dt
import json
import os
import random
import re
import time

import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st
from bs4 import BeautifulSoup

# ----------------------------------------------------------------------------
# 常量与配置
# ----------------------------------------------------------------------------

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PAGE_SIZE = 15

CATS = {
    "movie": {"host": "https://movie.douban.com", "label": "电影",
              "status_labels": {"wish": "想看", "do": "在看", "collect": "看过"}},
    "music": {"host": "https://music.douban.com", "label": "音乐",
              "status_labels": {"wish": "想听", "do": "在听", "collect": "听过"}},
    "book": {"host": "https://book.douban.com", "label": "书籍",
             "status_labels": {"wish": "想读", "do": "在读", "collect": "读过"}},
}

RANGES = {"近3个月": 90, "近1年": 365, "近3年": 365 * 3, "历史所有": None}

FIELDS = ["category", "title", "alt_title", "subject_id", "url", "my_rating",
          "mark_date", "my_tags", "my_comment", "intro", "year", "cover"]

DETAIL_FIELDS = ["subject_id", "imdb_id", "orig_title", "year",
                 "directors", "genres"]

DEFAULT_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/124.0.0.0 Safari/537.36")

# dataviz 调色 (light mode): 单序列图统一用蓝, 中性灰做底和轴
C_BLUE = "#2a78d6"
C_SURFACE = "#fcfcfb"
C_GRID = "#e1e0d9"
C_INK = "#0b0b0b"
C_MUTED = "#898781"
FONT = 'system-ui, -apple-system, "Segoe UI", sans-serif'


class BlockedError(RuntimeError):
    pass


def douban_login(name: str, password: str):
    """用豆瓣移动端登录接口换取会话。返回 (session, None) 或 (None, 错误信息)。
    密码只发往豆瓣官方接口 (accounts.douban.com), 本应用不保存。
    触发验证码等风控时返回错误, 引导用户改用 Cookie 通道。"""
    s = build_session(None)
    s.headers.update({
        "Origin": "https://accounts.douban.com",
        "Referer": "https://accounts.douban.com/passport/login",
        "Content-Type": "application/x-www-form-urlencoded",
    })
    try:
        r = s.post("https://accounts.douban.com/j/mobile/login/basic",
                   data={"ck": "", "name": name, "password": password,
                         "remember": "true"},
                   timeout=20)
        j = r.json()
    except Exception as e:
        return None, f"登录请求失败: {e}"
    if j.get("status") == "success":
        return s, None
    desc = j.get("description") or j.get("message") or str(j)[:200]
    if "captcha" in str(j).lower() or "验证" in desc:
        return None, ("豆瓣要求验证码, 程序无法自动完成。"
                      "请改用下方的 Cookie 方式登录。")
    return None, f"登录失败: {desc}"


# ----------------------------------------------------------------------------
# 抓取核心 (与 douban_export.py 同源逻辑)
# ----------------------------------------------------------------------------

def build_session(cookie: str | None) -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": DEFAULT_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Connection": "keep-alive",
    })
    if cookie:
        s.headers["Cookie"] = cookie.strip()
    return s


def fetch(session, url, referer=None, timeout=20):
    r = session.get(url, headers={"Referer": referer} if referer else {},
                    timeout=timeout, allow_redirects=True)
    if r.status_code == 403:
        raise BlockedError(f"403 被拒绝: {url}")
    if r.status_code == 404:
        return None
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}: {url}")
    r.encoding = r.apparent_encoding or "utf-8"
    html, final = r.text, r.url
    if "accounts.douban.com" in final or "/login" in final:
        raise BlockedError("被跳转到登录页, 请检查 Cookie")
    if ("sec.douban.com" in final or "sec.douban.com" in html
            or "验证码" in html or "有异常请求" in html or "禁止访问" in html):
        raise BlockedError("触发人机验证, 请稍后再试")
    return html


def clean(t):
    return re.sub(r"\s+", " ", t).strip() if t else ""


def extract_year(intro):
    m = re.search(r"\b(18[89]\d|19\d{2}|20[0-3]\d)\b", intro or "")
    return m.group(1) if m else ""


def parse_rating_cls(node):
    for span in node.find_all("span"):
        for cls in span.get("class", []):
            m = re.match(r"rating(\d)-t$", cls)
            if m:
                return m.group(1)
    return ""


def split_title(raw):
    parts = [p.strip() for p in raw.split("/") if p.strip()]
    title = parts[0] if parts else raw
    alt = " / ".join(parts[1:]) if len(parts) > 1 else ""
    return title, alt


def parse_grid_items(html, category):
    """电影/音乐 collect 页 (grid 视图)。"""
    soup = BeautifulSoup(html, "lxml")
    rows = []
    for item in soup.select("div.grid-view div.item"):
        info = item.select_one("div.info")
        if not info:
            continue
        a = info.select_one("li.title a") or info.select_one("a")
        if not a:
            continue
        url = a.get("href", "")
        title, alt = split_title(clean(a.get_text(" ", strip=True)))
        m = re.search(r"/subject/(\d+)", url)
        intro_li = info.select_one("li.intro")
        intro = clean(intro_li.get_text(" ", strip=True)) if intro_li else ""
        date = info.select_one("span.date")
        tags = info.select_one("span.tags")
        my_tags = ""
        if tags:
            t = re.sub(r"^标签[:：]\s*", "", clean(tags.get_text()))
            my_tags = "|".join(x for x in t.split() if x)
        cm = info.select_one("li.comment")
        img = item.select_one("div.pic img")
        rows.append({
            "category": category, "title": title, "alt_title": alt,
            "subject_id": m.group(1) if m else "", "url": url,
            "my_rating": parse_rating_cls(info),
            "mark_date": clean(date.get_text()) if date else "",
            "my_tags": my_tags,
            "my_comment": clean(cm.get_text(" ", strip=True)) if cm else "",
            "intro": intro, "year": extract_year(intro),
            "cover": img.get("src", "") if img else "",
        })
    return rows


def parse_book_items(html):
    """书籍 collect 页 (subject-item 视图)。"""
    soup = BeautifulSoup(html, "lxml")
    rows = []
    for item in soup.select("li.subject-item"):
        info = item.select_one("div.info")
        if not info:
            continue
        a = info.select_one("h2 a")
        if not a:
            continue
        url = a.get("href", "")
        raw = clean(a.get("title") or a.get_text(" ", strip=True))
        title, alt = split_title(raw)
        m = re.search(r"/subject/(\d+)", url)
        pub = info.select_one("div.pub")
        intro = clean(pub.get_text(" ", strip=True)) if pub else ""
        date = info.select_one("span.date")
        mark_date = ""
        if date:
            dm = re.search(r"(\d{4}-\d{2}-\d{2})", date.get_text())
            mark_date = dm.group(1) if dm else clean(date.get_text())
        tags = info.select_one("span.tags")
        my_tags = ""
        if tags:
            t = re.sub(r"^标签[:：]\s*", "", clean(tags.get_text()))
            my_tags = "|".join(x for x in t.split() if x)
        cm = info.select_one("p.comment")
        img = item.select_one("div.pic img")
        rows.append({
            "category": "book", "title": title, "alt_title": alt,
            "subject_id": m.group(1) if m else "", "url": url,
            "my_rating": parse_rating_cls(info),
            "mark_date": mark_date, "my_tags": my_tags,
            "my_comment": clean(cm.get_text(" ", strip=True)) if cm else "",
            "intro": intro, "year": extract_year(intro),
            "cover": img.get("src", "") if img else "",
        })
    return rows


def parse_items(html, category):
    if category == "book":
        return parse_book_items(html)
    return parse_grid_items(html, category)


def has_next_page(html):
    soup = BeautifulSoup(html, "lxml")
    nxt = soup.select_one("span.next a") or soup.select_one("a.next")
    return bool(nxt and nxt.get("href"))


def master_path(category, status, uid):
    return os.path.join(SCRIPT_DIR, f"douban_{category}_{status}_{uid}.csv")


def compat_path(category, status, uid):
    """douban_export.py 的旧命名 (douban_movie_collect_<uid>.csv) 恰好一致。"""
    return master_path(category, status, uid)


def load_master(path):
    if not os.path.exists(path):
        return {}, []
    rows, seen = [], {}
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            sid = (row.get("subject_id") or "").strip()
            if sid:
                for c in FIELDS:
                    row.setdefault(c, "")
                if not row["year"]:
                    row["year"] = extract_year(row.get("intro", ""))
                seen[sid] = True
                rows.append(row)
    return seen, rows


def save_master(path, rows):
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def crawl(session, uid, category, status, cutoff: dt.date | None,
          log, max_pages=200):
    """抓列表页到 master CSV。cutoff 为 None 抓全量(带续抓), 否则抓到早于
    cutoff 的页为止 (列表按标记时间倒序)。返回 (新增条数, 总条数)。"""
    conf = CATS[category]
    path = master_path(category, status, uid)
    seen, rows = load_master(path)
    log(f"本地已有 {len(rows)} 条")

    # 全量模式且已有数据: 从已有行数推算页码续抓; 时间范围模式永远从第一页开始
    start = (len(rows) // PAGE_SIZE) * PAGE_SIZE if cutoff is None else 0
    base = f"{conf['host']}/people/{uid}/{status}"
    referer = f"{conf['host']}/people/{uid}/"
    added, pages, empty_retries = 0, 0, 0
    touched = []

    while pages < max_pages:
        url = f"{base}?start={start}&sort=time&rating=all&filter=all&mode=grid"
        html = fetch(session, url, referer)
        if html is None:
            raise RuntimeError("页面 404, 请检查豆瓣 ID")
        items = parse_items(html, category)

        if not items:
            if empty_retries < 3:
                empty_retries += 1
                dbg = os.path.join(SCRIPT_DIR,
                                   f"douban_debug_{category}_{status}_{start}.html")
                with open(dbg, "w", encoding="utf-8") as f:
                    f.write(html)
                wait = 60 * empty_retries
                log(f"start={start} 返回空页面, 疑似限流, 等 {wait} 秒重试 "
                    f"({empty_retries}/3)")
                time.sleep(wait)
                continue
            log("连续空页面, 结束")
            break
        empty_retries = 0

        fresh = [it for it in items if it["subject_id"] not in seen]
        stop = False
        for it in fresh:
            d = parse_date(it["mark_date"])
            if cutoff and d and d < cutoff:
                stop = True
                continue
            seen[it["subject_id"]] = True
            rows.append(it)
            added += 1
        save_master(path, rows)

        # 记录本轮抓取实际覆盖到的条目 (在时间范围内的, 无论新旧)
        for it in items:
            d = parse_date(it["mark_date"])
            if not (cutoff and d and d < cutoff):
                touched.append(it)

        oldest = min((parse_date(i["mark_date"]) for i in items
                      if parse_date(i["mark_date"])), default=None)
        log(f"start={start}: 本页 {len(items)} 条, 新增 "
            f"{len([i for i in fresh if i['subject_id'] in seen])} 条, "
            f"累计 {len(rows)} 条")

        if cutoff and oldest and oldest < cutoff:
            log(f"本页最早标记 {oldest} 已早于范围下限 {cutoff}, 停止")
            break
        if cutoff and not fresh:
            log("本页全部已在本地, 停止")
            break
        if stop or not has_next_page(html):
            log("已到最后一页")
            break

        referer = url
        start += PAGE_SIZE
        pages += 1
        time.sleep(random.uniform(3, 6))

    return added, len(rows), touched


def parse_date(s):
    try:
        return dt.date.fromisoformat((s or "").strip()[:10])
    except ValueError:
        return None


# ----------------------------------------------------------------------------
# 深度抓取 (电影详情页: 导演 / 类型 / IMDb)
# ----------------------------------------------------------------------------

DETAIL_PATH = os.path.join(SCRIPT_DIR, "movie_detail_progress.csv")
LEGACY_IMDB = os.path.join(SCRIPT_DIR, "imdb_progress.csv")


def load_details():
    done = {}
    for p in (LEGACY_IMDB, DETAIL_PATH):  # 后读的覆盖先读的
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8-sig", newline="") as f:
                for row in csv.DictReader(f):
                    sid = (row.get("subject_id") or "").strip()
                    if sid:
                        for c in DETAIL_FIELDS:
                            row.setdefault(c, "")
                        done[sid] = row
    return done


def parse_movie_detail(html):
    soup = BeautifulSoup(html, "lxml")
    info = soup.select_one("#info")
    text = info.get_text(" ", strip=True) if info else ""
    m = re.search(r"IMDb[:：]?\s*(tt\d{6,10})", text or html)
    title_node = soup.select_one('span[property="v:itemreviewed"]')
    full = title_node.get_text(strip=True) if title_node else ""
    m2 = re.match(r"^([^\x00-\x7f].*?)\s+(\S.*)$", full)
    orig = m2.group(2).strip() if (m2 and re.search(r"[A-Za-z]", m2.group(2))) else ""
    yn = soup.select_one("span.year")
    ym = re.search(r"(\d{4})", yn.get_text()) if yn else None
    return {
        "imdb_id": m.group(1) if m else "",
        "orig_title": orig,
        "year": ym.group(1) if ym else "",
        "directors": ", ".join(a.get_text(strip=True)
                               for a in soup.select('a[rel="v:directedBy"]')),
        "genres": "|".join(s.get_text(strip=True)
                           for s in soup.select('span[property="v:genre"]')),
    }


def deep_crawl_movies(session, subject_ids, limit, log,
                      dmin=8.0, dmax=15.0, backfill_genres=False):
    """backfill_genres=False 时只抓从未抓过的条目 (最快补齐导演);
    =True 时把旧 imdb_progress.csv 里缺类型字段的条目也重抓一遍。"""
    done = load_details()
    todo = [s for s in subject_ids if s not in done]
    if backfill_genres:
        extra = [s for s in subject_ids if s in done
                 and not done[s].get("genres", "")
                 and done[s].get("imdb_id") != "GONE"]
        todo += [s for s in extra if s not in todo]
    todo = todo[:limit] if limit else todo
    log(f"待抓 {len(todo)} 条 (已完成 {len(done)})")
    new_file = not os.path.exists(DETAIL_PATH)
    f = open(DETAIL_PATH, "a", encoding="utf-8-sig", newline="")
    w = csv.DictWriter(f, fieldnames=DETAIL_FIELDS, extrasaction="ignore")
    if new_file:
        w.writeheader()
    blocked = 0
    referer = "https://movie.douban.com/"
    try:
        for i, sid in enumerate(todo, 1):
            while True:
                try:
                    html = fetch(session, f"https://movie.douban.com/subject/{sid}/",
                                 referer)
                    blocked = 0
                    break
                except BlockedError as e:
                    blocked += 1
                    if blocked > 3:
                        log(f"连续被拦 3 次, 保存进度退出 ({e})")
                        return i - 1
                    wait = 120 * blocked
                    log(f"被拦截, 等 {wait} 秒重试 ({blocked}/3)")
                    time.sleep(wait)
            if html is None:
                rec = {"subject_id": sid, "imdb_id": "GONE", "orig_title": "",
                       "year": "", "directors": "", "genres": ""}
            else:
                d = parse_movie_detail(html)
                rec = {"subject_id": sid,
                       "imdb_id": d["imdb_id"] or "NOT_FOUND", **{
                           k: d[k] for k in ("orig_title", "year",
                                             "directors", "genres")}}
            w.writerow(rec)
            f.flush()
            log(f"[{i}/{len(todo)}] {sid} -> {rec['imdb_id']} "
                f"{rec['directors']} {rec['genres']}")
            referer = f"https://movie.douban.com/subject/{sid}/"
            if i < len(todo):
                time.sleep(random.uniform(dmin, dmax))
    finally:
        f.close()
    return len(todo)


# ----------------------------------------------------------------------------
# 分析
# ----------------------------------------------------------------------------

def load_df(category, status, uid, cutoff):
    path = master_path(category, status, uid)
    if not os.path.exists(path):
        return None
    df = pd.read_csv(path, dtype=str, encoding="utf-8-sig").fillna("")
    # 兼容旧格式 CSV: 缺失列补空, year 缺失/为空时从 intro 现场解析
    for c in FIELDS:
        if c not in df.columns:
            df[c] = ""
    need = df["year"].astype(str).str.strip() == ""
    if need.any():
        df.loc[need, "year"] = df.loc[need, "intro"].map(extract_year)
    df["mark_dt"] = pd.to_datetime(df["mark_date"], errors="coerce")
    if cutoff:
        df = df[df["mark_dt"] >= pd.Timestamp(cutoff)]
    df["year_num"] = pd.to_numeric(df["year"], errors="coerce")
    df["rating_num"] = pd.to_numeric(df["my_rating"], errors="coerce")
    return df


def creator_from_intro(row):
    """音乐: intro 首段是艺术家; 书籍: pub 首段是作者。"""
    seg = (row.get("intro", "") or "").split("/")[0].strip()
    # 过滤以日期/数字开头的段 (那是上映日期而不是人名)
    if not seg or re.match(r"^\d{4}", seg):
        return ""
    return seg


def base_layout(title):
    return dict(
        title=dict(text=title, font=dict(color=C_INK, size=15)),
        paper_bgcolor=C_SURFACE, plot_bgcolor=C_SURFACE,
        font=dict(family=FONT, color=C_MUTED, size=12),
        margin=dict(l=40, r=20, t=48, b=40),
        xaxis=dict(gridcolor=C_GRID, zerolinecolor=C_GRID, linecolor=C_GRID),
        yaxis=dict(gridcolor=C_GRID, zerolinecolor=C_GRID, linecolor=C_GRID),
        showlegend=False, height=340,
    )


def chart_year_dist(df, label):
    d = df.dropna(subset=["year_num"])
    if d.empty:
        return None
    counts = d.groupby(d["year_num"].astype(int)).size()
    fig = go.Figure(go.Bar(x=counts.index, y=counts.values,
                           marker_color=C_BLUE,
                           hovertemplate="%{x} 年: %{y} 部<extra></extra>"))
    fig.update_layout(**base_layout(f"{label}发行/上映年份分布"))
    return fig


def chart_rating_dist(df, label):
    d = df.dropna(subset=["rating_num"])
    if d.empty:
        return None
    counts = d.groupby(d["rating_num"].astype(int)).size().reindex(
        range(1, 6), fill_value=0)
    fig = go.Figure(go.Bar(x=[f"{i} 星" for i in counts.index], y=counts.values,
                           marker_color=C_BLUE,
                           hovertemplate="%{x}: %{y} 条<extra></extra>"))
    fig.update_layout(**base_layout(f"我的{label}评分分布"))
    return fig


def chart_monthly(df, label):
    d = df.dropna(subset=["mark_dt"])
    if d.empty:
        return None
    monthly = d.groupby(d["mark_dt"].dt.to_period("M")).size()
    x = monthly.index.astype(str)
    fig = go.Figure(go.Scatter(x=x, y=monthly.values, mode="lines",
                               line=dict(color=C_BLUE, width=2),
                               fill="tozeroy",
                               fillcolor="rgba(42,120,214,0.12)",
                               hovertemplate="%{x}: %{y} 条<extra></extra>"))
    fig.update_layout(**base_layout(f"每月标记{label}数量"))
    return fig


def chart_top_bar(series, title, unit="条", n=10):
    if series is None or series.empty:
        return None
    top = series.head(n)[::-1]
    fig = go.Figure(go.Bar(x=top.values, y=top.index, orientation="h",
                           marker_color=C_BLUE,
                           text=[f"{v} {unit}" for v in top.values],
                           textposition="outside", cliponaxis=False,
                           textfont=dict(color=C_MUTED, size=12),
                           hovertemplate="%{y}: %{x} " + unit + "<extra></extra>"))
    layout = base_layout(title)
    layout["height"] = max(280, 32 * len(top) + 90)
    layout["margin"]["l"] = 10
    layout["margin"]["r"] = 60
    layout["yaxis"]["automargin"] = True
    layout["xaxis"]["showgrid"] = False
    fig.update_layout(**layout)
    return fig


def explode_counts(df, col, sep="|"):
    vals = []
    for v in df[col]:
        vals += [x.strip() for x in str(v).split(sep) if x.strip()]
    return pd.Series(vals).value_counts() if vals else pd.Series(dtype=int)


def build_stats(df, category, details):
    """给 AI 总结用的统计摘要 (纯文本数据, 不发图)。"""
    label = CATS[category]["label"]
    s = {"类别": label, "条目总数": int(len(df))}
    d = df.dropna(subset=["year_num"])
    if not d.empty:
        s["发行年份"] = {"最早": int(d["year_num"].min()),
                       "最晚": int(d["year_num"].max()),
                       "按年代分布": {f"{int(k)}s": int(v) for k, v in
                                  d.groupby((d["year_num"] // 10 * 10)).size().items()}}
    r = df.dropna(subset=["rating_num"])
    if not r.empty:
        s["我的评分"] = {"平均": round(float(r["rating_num"].mean()), 2),
                     "分布": {f"{int(k)}星": int(v) for k, v in
                            r.groupby(r["rating_num"].astype(int)).size().items()}}
    m = df.dropna(subset=["mark_dt"])
    if not m.empty:
        s["标记时间跨度"] = {"最早": str(m["mark_dt"].min().date()),
                       "最晚": str(m["mark_dt"].max().date()),
                       "最活跃月份Top5": {str(k): int(v) for k, v in
                                     m.groupby(m["mark_dt"].dt.to_period("M"))
                                     .size().nlargest(5).items()}}
    if category == "movie" and details:
        sids = set(df["subject_id"].astype(str))
        covered = len(sids & set(details.keys()))
        if covered == len(sids):
            dd = df.merge(pd.DataFrame(details.values()), on="subject_id",
                          how="left", suffixes=("", "_d"))
            dirs = explode_counts(dd.fillna(""), "directors", sep=",")
            if not dirs.empty:
                s["看过最多的导演Top10"] = dirs.head(10).to_dict()
            gen = explode_counts(dd.fillna(""), "genres")
            if not gen.empty:
                s["类型分布"] = gen.head(15).to_dict()
        else:
            s["说明"] = (f"导演/类型详情仅覆盖 {covered}/{len(sids)} 条, "
                       f"未达全量, 本次总结不含导演与类型维度")
    elif category in ("music", "book"):
        creators = df.apply(creator_from_intro, axis=1)
        creators = creators[creators != ""]
        if not creators.empty:
            key = "听过最多的艺术家Top10" if category == "music" else "读过最多的作者Top10"
            s[key] = creators.value_counts().head(10).to_dict()
    return s


# 支持的模型厂商。models 取自各家官方文档 (2026-08 核对), 列表可能过时,
# 选「自定义…」可手工输入任意模型名。
PROVIDERS = {
    "Anthropic (Claude)": {
        "kind": "anthropic",
        "models": ["claude-sonnet-5", "claude-opus-5", "claude-fable-5",
                   "claude-haiku-4-5"],
        "key_hint": "sk-ant-...", "key_url": "https://console.anthropic.com/",
    },
    "OpenAI (GPT)": {
        "kind": "openai", "base_url": "https://api.openai.com/v1",
        "models": ["gpt-5.6-terra", "gpt-5.6-sol", "gpt-5.6-luna", "gpt-4o"],
        "key_hint": "sk-...",
        "key_url": "https://platform.openai.com/api-keys",
    },
    "Google (Gemini)": {
        "kind": "gemini",
        "models": ["gemini-3.7-flash", "gemini-3.6-flash", "gemini-3.5-flash",
                   "gemini-3.5-flash-lite", "gemini-2.5-pro"],
        "key_hint": "AIza...", "key_url": "https://aistudio.google.com/apikey",
    },
    "DeepSeek": {
        "kind": "openai", "base_url": "https://api.deepseek.com/v1",
        "models": ["deepseek-v4-flash", "deepseek-v4-pro"],
        "key_hint": "sk-...", "key_url": "https://platform.deepseek.com/",
    },
    "Moonshot (Kimi)": {
        "kind": "openai", "base_url": "https://api.moonshot.cn/v1",
        "models": ["kimi-k3", "kimi-k2.6", "kimi-k2.5", "moonshot-v1-128k",
                   "moonshot-v1-32k", "moonshot-v1-8k"],
        "key_hint": "sk-...", "key_url": "https://platform.moonshot.cn/",
    },
    "其它 OpenAI 兼容接口": {
        "kind": "openai", "base_url": "", "models": [],
        "key_hint": "自定义", "key_url": "",
    },
}

CUSTOM_MODEL = "自定义…"

PROMPT_TMPL = (
    "下面是我的豆瓣个人收藏数据的统计摘要 (JSON)。请用中文写一段观察式的总结, "
    "分析我的品味特点、年代偏好、评分习惯、活跃规律, 以及任何有意思的模式。"
    "语气自然, 不要逐条罗列数字, 而是提炼洞察。300-500 字。\n\n"
)


def call_llm(provider, api_key, model, stats, base_url=""):
    """调用所选厂商的模型生成总结。返回文本, 失败抛 RuntimeError。"""
    conf = PROVIDERS[provider]
    kind = conf["kind"]
    prompt = PROMPT_TMPL + json.dumps(stats, ensure_ascii=False, indent=1)

    if kind == "anthropic":
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": api_key, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": model, "max_tokens": 1500,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=120)
        if r.status_code != 200:
            raise RuntimeError(f"API 返回 {r.status_code}: {r.text[:300]}")
        return "".join(b.get("text", "")
                       for b in r.json().get("content", [])).strip()

    if kind == "openai":
        url = (base_url or conf.get("base_url", "")).rstrip("/")
        if not url:
            raise RuntimeError("请填写接口地址 (base URL)")
        r = requests.post(
            f"{url}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}",
                     "Content-Type": "application/json"},
            json={"model": model, "max_tokens": 1500,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=120)
        if r.status_code != 200:
            raise RuntimeError(f"API 返回 {r.status_code}: {r.text[:300]}")
        return r.json()["choices"][0]["message"]["content"].strip()

    if kind == "gemini":
        r = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{model}:generateContent",
            headers={"Content-Type": "application/json",
                     "x-goog-api-key": api_key},
            json={"contents": [{"parts": [{"text": prompt}]}]},
            timeout=120)
        if r.status_code != 200:
            raise RuntimeError(f"API 返回 {r.status_code}: {r.text[:300]}")
        cands = r.json().get("candidates", [])
        if not cands:
            raise RuntimeError(f"模型没有返回内容: {str(r.json())[:300]}")
        return "".join(p.get("text", "") for p in
                       cands[0]["content"]["parts"]).strip()

    raise RuntimeError(f"不支持的厂商: {provider}")


# ----------------------------------------------------------------------------
# Streamlit 界面
# ----------------------------------------------------------------------------

def main():
    st.set_page_config(page_title="豆瓣书影音", page_icon="📚", layout="wide")
    st.title("豆瓣书影音记录")

    with st.sidebar:
        st.header("配置")
        uid = st.text_input("豆瓣 ID", value="", placeholder="例如 ahbei 或 123456789",
                            help="打开自己的豆瓣主页, 地址 douban.com/people/后面"
                                 "那一段就是你的 ID")
        st.subheader("登录豆瓣")
        mode = st.radio("登录方式", ["账号密码", "Cookie (高级)"],
                        horizontal=True, label_visibility="collapsed")
        cookie = ""
        if mode == "账号密码":
            acc = st.text_input("手机号 / 邮箱")
            pwd = st.text_input("密码", type="password",
                                help="密码只发送给豆瓣官方登录接口, 本应用不保存")
            if st.button("登录"):
                if not acc or not pwd:
                    st.error("请填写账号和密码")
                else:
                    s, err = douban_login(acc.strip(), pwd)
                    if s:
                        st.session_state["douban_session"] = s
                        st.success("登录成功")
                    else:
                        st.error(err)
                        st.info("登录不通时, 把上方登录方式切到 "
                                "「Cookie (高级)」, 那里有图文步骤。")
            if st.session_state.get("douban_session"):
                st.caption("状态: 已登录 ✓")
        else:
            cookie_in = st.text_input(
                "豆瓣 Cookie", type="password",
                value=os.environ.get("DOUBAN_COOKIE", ""),
                help="是浏览器里的豆瓣登录信息, 不是 API Key")
            if st.button("确认 Cookie"):
                c = (cookie_in or "").strip()
                if not c:
                    st.error("请先粘贴 Cookie")
                elif c.startswith(("sk-", "AIza")):
                    st.error("这看起来是 API Key, 不是豆瓣 Cookie。"
                             "API Key 请填到下方「AI 总结模型」里。")
                elif "dbcl2" not in c and "bid=" not in c:
                    st.warning("没检测到 dbcl2 / bid 字段, 可能不是完整的豆瓣 "
                               "Cookie, 仍会尝试使用。")
                    st.session_state["douban_cookie"] = c
                else:
                    st.session_state["douban_cookie"] = c
                    st.success("Cookie 已确认")
            cookie = st.session_state.get("douban_cookie", "")
            if cookie:
                st.caption("状态: Cookie 已生效 ✓")
            with st.expander("怎么获取 Cookie?"):
                st.markdown(
                    "1. 用电脑浏览器打开 douban.com 并登录\n"
                    "2. 按 `F12` (或 `Ctrl+Shift+I`) 打开开发者工具\n"
                    "3. 切到 **Network / 网络** 标签, 按 `Ctrl+R` 刷新页面\n"
                    "4. 点请求列表**第一条** (一般叫 www.douban.com)\n"
                    "5. 右侧 **Headers → Request Headers** 里找到 `Cookie:`\n"
                    "6. 复制冒号后面**整段**内容, 粘贴到上面输入框")
        st.divider()
        st.subheader("AI 总结模型")
        provider = st.selectbox("厂商", list(PROVIDERS.keys()))
        pconf = PROVIDERS[provider]
        base_url = ""
        if provider == "其它 OpenAI 兼容接口":
            base_url = st.text_input("接口地址 (base URL)",
                                     placeholder="https://xxx.com/v1")
        api_key = st.text_input(
            "API Key", type="password",
            value=os.environ.get("LLM_API_KEY", ""),
            placeholder=pconf["key_hint"],
            help=f"到 {pconf['key_url']} 申请" if pconf["key_url"] else "自定义接口的密钥")
        opts = list(pconf["models"]) + [CUSTOM_MODEL]
        picked = st.selectbox("模型名", opts,
                              help="列表为各厂商官方文档中的可用模型 (2026-08 核对), "
                                   "如已更新可选「自定义…」手工填写")
        model = (st.text_input("自定义模型名", placeholder="填写完整模型 ID").strip()
                 if picked == CUSTOM_MODEL else picked)
        st.caption("Key 只用于直接调用该厂商接口, 不保存到文件")
        st.divider()
        st.caption(f"数据目录: {SCRIPT_DIR}")

    def get_session():
        """优先用账号密码登录的会话, 其次用 Cookie。都没有返回 None。"""
        if st.session_state.get("douban_session") is not None:
            return st.session_state["douban_session"]
        if cookie:
            return build_session(cookie)
        return None

    tab_crawl, tab_dash, tab_ai = st.tabs(["① 抓取数据", "② 分析 Dashboard", "③ AI 总结"])

    # ---------------- ① 抓取 ----------------
    with tab_crawl:
        c1, c2, c3 = st.columns(3)
        cat_label = c1.selectbox("类别", [CATS[c]["label"] for c in CATS],
                                 key="crawl_cat")
        category = next(k for k, v in CATS.items() if v["label"] == cat_label)
        slabels = CATS[category]["status_labels"]
        status_label = c2.selectbox("状态", list(slabels.values()), index=2)
        status = next(k for k, v in slabels.items() if v == status_label)
        range_label = c3.selectbox("时间范围", list(RANGES.keys()), index=3,
                                   key="crawl_range")

        if st.button(f"抓取{status_label}的{cat_label}", type="primary"):
            session = get_session()
            if session is None:
                st.error("请先在左侧登录豆瓣 (账号密码或 Cookie)")
            else:
                days = RANGES[range_label]
                cutoff = dt.date.today() - dt.timedelta(days=days) if days else None
                box = st.status(f"抓取中: {status_label}的{cat_label} ({range_label})",
                                expanded=True)
                try:
                    added, total, touched = crawl(session, uid, category,
                                                  status, cutoff,
                                                  lambda m: box.write(m))
                    st.session_state["last_crawl"] = {
                        "key": (category, status, range_label),
                        "rows": touched,
                        "label": f"{status_label}的{cat_label} · {range_label}",
                    }
                    box.update(label=f"完成: 本轮抓取 {len(touched)} 条 "
                                     f"(其中新增 {added} 条), 本地共 {total} 条",
                               state="complete")
                except BlockedError as e:
                    box.update(label=f"被豆瓣拦截: {e} (已抓数据已保存, 稍后重试可续抓)",
                               state="error")
                except Exception as e:
                    box.update(label=f"出错: {e}", state="error")

        # 导出
        def to_csv_text(rows):
            return "﻿" + "\r\n".join(
                [",".join(FIELDS)] +
                [",".join('"' + str(r.get(c, "") or "").replace('"', '""') + '"'
                          for c in FIELDS) for r in rows])

        last = st.session_state.get("last_crawl")
        if last and last["key"] == (category, status, range_label):
            st.download_button(
                f"导出本轮抓取结果到 CSV ({len(last['rows'])} 条)",
                to_csv_text(last["rows"]),
                file_name=f"douban_{category}_{status}_{range_label}_本轮.csv",
                mime="text/csv", type="primary")
        else:
            st.caption("完成一次抓取后, 这里可以导出本轮抓取结果")

        p = master_path(category, status, uid)
        if os.path.exists(p):
            _, rows = load_master(p)
            days = RANGES[range_label]
            cutoff = dt.date.today() - dt.timedelta(days=days) if days else None
            if cutoff:
                rows = [r for r in rows
                        if (parse_date(r["mark_date"]) or dt.date.min) >= cutoff]
            st.download_button(
                f"导出本地全部数据 · {range_label} ({len(rows)} 条)",
                to_csv_text(rows),
                file_name=f"douban_{category}_{status}_{range_label}.csv",
                mime="text/csv")
            st.caption(f"本地文件: {os.path.basename(p)}")

        st.divider()
        st.subheader("深度抓取 (电影详情页: 导演 / 类型 / IMDb 编号)")
        st.caption("已有 imdb_progress.csv 会自动导入。被拦截时进度自动保存, "
                   "重新点击即可续抓。")
        cc1, cc2 = st.columns(2)
        limit = cc1.number_input("本批最多抓取条数", 10, 3000, 700, step=50)
        speed = cc2.selectbox(
            "抓取速度", ["快速 (4-6 秒/条)", "保守 (8-15 秒/条)"], index=0,
            help="快速模式约 5 秒一条; 保守模式约 12 秒一条, 被风控概率更低")
        dmin, dmax = (4.0, 6.0) if speed.startswith("快速") else (8.0, 15.0)
        backfill = st.checkbox(
            "同时补抓旧数据的「类型」字段", value=False,
            help="旧的 imdb_progress.csv 只存了导演没存类型。勾选后会把这些条目"
                 "重新抓一遍以补齐类型, 耗时大幅增加; 只想补导演就别勾。")

        # 实际待抓量, 让预估贴近真实
        _p = master_path("movie", "collect", uid)
        if os.path.exists(_p):
            _, _rows = load_master(_p)
            _done = load_details()
            _sids = [r["subject_id"] for r in _rows]
            _never = [s for s in _sids if s not in _done]
            _nogen = [s for s in _sids if s in _done
                      and not _done[s].get("genres", "")
                      and _done[s].get("imdb_id") != "GONE"]
            _todo = len(_never) + (len(_nogen) if backfill else 0)
            _batch = min(int(limit), _todo)
            st.caption(
                f"本地 {len(_sids)} 部 · 已有详情 {len(_done)} 条 · "
                f"从未抓过 {len(_never)} 条"
                + (f" · 缺类型待补 {len(_nogen)} 条" if backfill else "")
                + f" → 待抓 {_todo} 条, 本批 {_batch} 条, "
                  f"预计约 {_batch * (dmin + dmax) / 2 / 60:.0f} 分钟")
        if st.button("开始深度抓取"):
            session = get_session()
            if session is None:
                st.error("请先在左侧登录豆瓣 (账号密码或 Cookie)")
            else:
                p2 = master_path("movie", "collect", uid)
                seen, rows = load_master(p2)
                if not rows:
                    st.error("请先抓取「看过的电影」列表")
                else:
                    box = st.status("深度抓取中", expanded=True)
                    try:
                        n = deep_crawl_movies(session,
                                              [r["subject_id"] for r in rows],
                                              int(limit), lambda m: box.write(m),
                                              dmin, dmax, backfill)
                        done = load_details()
                        box.update(label=f"本批处理 {n} 条, 详情累计 {len(done)} 条",
                                   state="complete")
                    except Exception as e:
                        box.update(label=f"出错: {e} (进度已保存)", state="error")

    # ---------------- ② 分析 ----------------
    with tab_dash:
        c1, c2, c3 = st.columns(3)
        cat_label2 = c1.selectbox("类别 ", [CATS[c]["label"] for c in CATS],
                                  key="dash_cat")
        category2 = next(k for k, v in CATS.items() if v["label"] == cat_label2)
        slabels2 = CATS[category2]["status_labels"]
        status_label2 = c2.selectbox("状态 ", list(slabels2.values()), index=2,
                                     key="dash_status")
        status2 = next(k for k, v in slabels2.items() if v == status_label2)
        range_label2 = c3.selectbox("时间范围 ", list(RANGES.keys()), index=3,
                                    key="dash_range")

        if st.button("生成分析", type="primary"):
            days = RANGES[range_label2]
            cutoff = dt.date.today() - dt.timedelta(days=days) if days else None
            df = load_df(category2, status2, uid, cutoff)
            if df is None or df.empty:
                st.warning("没有数据, 请先在 ① 抓取")
            else:
                st.session_state["analysis"] = {
                    "df": df, "category": category2,
                    "label": cat_label2, "range": range_label2,
                    "status_label": status_label2,
                }

        a = st.session_state.get("analysis")
        if a:
            df, label = a["df"], a["label"]
            details = load_details() if a["category"] == "movie" else {}

            m1, m2, m3, m4 = st.columns(4)
            m1.metric("条目数", len(df))
            rated = df.dropna(subset=["rating_num"])
            m2.metric("平均评分", f"{rated['rating_num'].mean():.2f}"
                      if not rated.empty else "—")
            yr = df.dropna(subset=["year_num"])
            m3.metric("年份跨度", f"{int(yr['year_num'].min())}–{int(yr['year_num'].max())}"
                      if not yr.empty else "—")
            md = df.dropna(subset=["mark_dt"])
            m4.metric("标记跨度",
                      f"{md['mark_dt'].min():%Y.%m}–{md['mark_dt'].max():%Y.%m}"
                      if not md.empty else "—")

            l, r = st.columns(2)
            for col, fig in [
                (l, chart_year_dist(df, label)),
                (r, chart_rating_dist(df, label)),
            ]:
                if fig:
                    col.plotly_chart(fig, use_container_width=True)
            fig = chart_monthly(df, label)
            if fig:
                st.plotly_chart(fig, use_container_width=True)

            if a["category"] == "movie":
                # 导演/类型必须全量口径: 详情没抓全就不出图, 防止部分覆盖误导
                sids = set(df["subject_id"].astype(str))
                covered = len(sids & set(details.keys()))
                missing = len(sids) - covered
                if missing > 0:
                    st.warning(
                        f"电影详情数据覆盖 {covered}/{len(sids)} 条, 还差 "
                        f"{missing} 条。导演与类型分析按全量口径统计, 请先在 "
                        f"① 完成深度抓取再生成 (每批可设 500 条, 分几批跑完)。")
                else:
                    dd = df.merge(pd.DataFrame(details.values()),
                                  on="subject_id", how="left",
                                  suffixes=("", "_d")).fillna("")
                    l2, r2 = st.columns(2)
                    f1 = chart_top_bar(explode_counts(dd, "directors", ","),
                                       "看过最多的导演 Top 10", "部")
                    f2 = chart_top_bar(explode_counts(dd, "genres"),
                                       "电影类型分布", "部", n=15)
                    if f1:
                        l2.plotly_chart(f1, use_container_width=True)
                    if f2:
                        r2.plotly_chart(f2, use_container_width=True)
            else:
                creators = df.apply(creator_from_intro, axis=1)
                creators = creators[creators != ""]
                name = "艺术家" if a["category"] == "music" else "作者"
                f1 = chart_top_bar(creators.value_counts(),
                                   f"{label}出现最多的{name} Top 10")
                if f1:
                    st.plotly_chart(f1, use_container_width=True)

    # ---------------- ③ AI 总结 ----------------
    with tab_ai:
        a = st.session_state.get("analysis")
        if not a:
            st.info("先在 ② 生成分析, 再来这里调用 AI 总结")
        else:
            st.caption(f"将基于: {a['status_label']}的{a['label']} / {a['range']} "
                       f"({len(a['df'])} 条) 的统计数据生成总结")
            st.caption(f"当前模型: {provider} / {model or '未填写模型名'}")
            if st.button("生成 AI 总结", type="primary"):
                if not api_key:
                    st.error(f"请先在左侧填入 {provider} 的 API Key")
                elif not model:
                    st.error("请先在左侧填写模型名")
                else:
                    details = load_details() if a["category"] == "movie" else {}
                    stats = build_stats(a["df"], a["category"], details)
                    with st.spinner(f"{provider} 分析中…"):
                        try:
                            text = call_llm(provider, api_key, model,
                                            stats, base_url)
                            st.session_state["ai_summary"] = text
                        except Exception as e:
                            st.error(f"调用失败: {e}")
            if st.session_state.get("ai_summary"):
                st.markdown(st.session_state["ai_summary"])


if __name__ == "__main__":
    main()
