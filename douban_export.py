#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
douban_export.py — 抓取豆瓣用户已标记「看过」的电影和「听过」的音乐，导出 CSV。

依赖:
    pip install requests beautifulsoup4 lxml

基本用法:
    python douban_export.py --uid 你的豆瓣ID
    python douban_export.py --uid 你的豆瓣ID --type movie
    python douban_export.py --uid 你的豆瓣ID --cookie "$(cat cookie.txt)"

豆瓣ID的取法: 打开自己的豆瓣主页, 地址形如
    https://www.douban.com/people/ahbei/   -> uid 是 ahbei
    https://www.douban.com/people/12345678/ -> uid 是 12345678

设计说明:
  * 边抓边写 CSV。中途被限流中断时, 已抓到的数据不会丢。
  * 自带断点续抓: 再次运行同一命令会读取已有 CSV, 从上次的页码继续。
  * 默认请求间隔 3~6 秒。豆瓣对匿名高频访问封禁很快, 不建议调低。
"""

import argparse
import csv
import os
import random
import re
import sys
import time

import requests
from bs4 import BeautifulSoup

PAGE_SIZE = 15  # grid 模式每页条目数

# 输出默认放在脚本所在目录, 与运行时的当前目录无关
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

CATEGORIES = {
    "movie": {
        "host": "https://movie.douban.com",
        "label": "电影",
        "status_label": "看过",
    },
    "music": {
        "host": "https://music.douban.com",
        "label": "音乐",
        "status_label": "听过",
    },
    "book": {
        "host": "https://book.douban.com",
        "label": "书籍",
        "status_label": "读过",
    },
}

FIELDS = [
    "category",     # movie / music
    "title",        # 主标题
    "alt_title",    # 副标题 / 原名
    "subject_id",   # 豆瓣条目 ID
    "url",          # 条目链接
    "my_rating",    # 我的评分 1-5, 未评分为空
    "mark_date",    # 我的标记日期
    "my_tags",      # 我打的标签, 用 | 分隔
    "my_comment",   # 我的短评
    "intro",        # 条目信息行 (上映日期与主演 / 艺术家与出版方等)
    "year",         # 年份, 从 intro 提取 (电影为上映年, 音乐为发行年)
    "cover",        # 封面图链接
]

DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


class BlockedError(RuntimeError):
    """被豆瓣拦截 (403 / 跳转登录 / 出现验证码)。"""


def build_session(cookie: str | None, user_agent: str) -> requests.Session:
    s = requests.Session()
    s.headers.update(
        {
            "User-Agent": user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Connection": "keep-alive",
        }
    )
    if cookie:
        s.headers["Cookie"] = cookie.strip()
    return s


def fetch(session: requests.Session, url: str, referer: str | None,
          max_retry: int = 3, timeout: int = 20) -> str:
    """取一个页面。403/需要登录/验证码 直接抛 BlockedError, 不做无意义重试。"""
    headers = {"Referer": referer} if referer else {}
    last_err = None

    for attempt in range(1, max_retry + 1):
        try:
            r = session.get(url, headers=headers, timeout=timeout, allow_redirects=True)
        except requests.RequestException as e:
            last_err = e
            time.sleep(2 * attempt)
            continue

        if r.status_code == 403:
            raise BlockedError(f"403 被拒绝: {url}")
        if r.status_code == 404:
            raise RuntimeError(f"404 页面不存在, 检查 uid 是否正确: {url}")
        if r.status_code >= 500:
            last_err = RuntimeError(f"服务端 {r.status_code}")
            time.sleep(3 * attempt)
            continue
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}: {url}")

        r.encoding = r.apparent_encoding or "utf-8"
        html = r.text

        final = r.url
        if "accounts.douban.com" in final or "/login" in final:
            raise BlockedError("被跳转到登录页, 该主页可能不公开, 需要 --cookie")
        if ("sec.douban.com" in final or "sec.douban.com" in html
                or "验证码" in html or "有异常请求" in html
                or "禁止访问" in html):
            raise BlockedError("触发了豆瓣的人机验证, 请换 IP 或等一段时间再跑")

        return html

    raise RuntimeError(f"重试 {max_retry} 次仍失败: {url} ({last_err})")


def parse_rating(li_or_span) -> str:
    """从 class="rating4-t" 这样的 class 里取出星级数字。"""
    if li_or_span is None:
        return ""
    for span in li_or_span.find_all("span"):
        for cls in span.get("class", []):
            m = re.match(r"rating(\d)-t$", cls)
            if m:
                return m.group(1)
    return ""


def clean(text: str | None) -> str:
    if not text:
        return ""
    return re.sub(r"\s+", " ", text).strip()


def extract_year(intro: str) -> str:
    """从 intro 里取第一个像年份的四位数 (1880-2039)。"""
    m = re.search(r"\b(18[89]\d|19\d{2}|20[0-3]\d)\b", intro or "")
    return m.group(1) if m else ""


def parse_book_items(html: str) -> list[dict]:
    """书籍收藏页结构与影音不同 (li.subject-item), 单独解析。"""
    soup = BeautifulSoup(html, "lxml")
    rows = []

    for item in soup.select("li.subject-item"):
        info = item.select_one("div.info")
        if info is None:
            continue
        a = info.select_one("h2 a")
        if a is None:
            continue

        url = a.get("href", "")
        raw_title = clean(a.get("title") or a.get_text(" ", strip=True))
        parts = [p.strip() for p in raw_title.split("/") if p.strip()]
        title = parts[0] if parts else raw_title
        alt_title = " / ".join(parts[1:]) if len(parts) > 1 else ""

        m = re.search(r"/subject/(\d+)", url)
        subject_id = m.group(1) if m else ""

        pub = info.select_one("div.pub")
        intro = clean(pub.get_text(" ", strip=True)) if pub else ""

        date_span = info.select_one("span.date")
        mark_date = ""
        if date_span:
            dm = re.search(r"(\d{4}-\d{2}-\d{2})", date_span.get_text())
            mark_date = dm.group(1) if dm else clean(date_span.get_text())

        my_rating = parse_rating(info)

        tags_span = info.select_one("span.tags")
        my_tags = ""
        if tags_span:
            t = re.sub(r"^标签[:：]\s*", "", clean(tags_span.get_text()))
            my_tags = "|".join(x for x in t.split() if x)

        comment = info.select_one("p.comment")
        my_comment = clean(comment.get_text(" ", strip=True)) if comment else ""

        img = item.select_one("div.pic img")
        cover = img.get("src", "") if img else ""

        rows.append(
            {
                "category": "book",
                "title": title,
                "alt_title": alt_title,
                "subject_id": subject_id,
                "url": url,
                "my_rating": my_rating,
                "mark_date": mark_date,
                "my_tags": my_tags,
                "my_comment": my_comment,
                "intro": intro,
                "year": extract_year(intro),
                "cover": cover,
            }
        )

    return rows


def parse_items(html: str, category: str) -> list[dict]:
    """解析一页收藏列表, 返回条目列表。"""
    if category == "book":
        return parse_book_items(html)

    soup = BeautifulSoup(html, "lxml")
    grid = soup.select_one("div.grid-view") or soup
    rows = []

    for item in grid.select("div.item"):
        info = item.select_one("div.info")
        if info is None:
            continue

        title_a = info.select_one("li.title a") or info.select_one("a")
        if title_a is None:
            continue

        url = title_a.get("href", "")
        raw_title = clean(title_a.get_text(" ", strip=True))

        # 豆瓣把中文名和原名放在同一个 em 里, 用 " / " 分隔
        parts = [p.strip() for p in raw_title.split("/") if p.strip()]
        title = parts[0] if parts else raw_title
        alt_title = " / ".join(parts[1:]) if len(parts) > 1 else ""

        m = re.search(r"/subject/(\d+)", url)
        subject_id = m.group(1) if m else ""

        intro_li = info.select_one("li.intro")
        intro = clean(intro_li.get_text(" ", strip=True)) if intro_li else ""

        date_span = info.select_one("span.date")
        mark_date = clean(date_span.get_text()) if date_span else ""

        my_rating = parse_rating(info)

        tags_span = info.select_one("span.tags")
        my_tags = ""
        if tags_span:
            t = clean(tags_span.get_text())
            t = re.sub(r"^标签[:：]\s*", "", t)
            my_tags = "|".join(x for x in t.split() if x)

        comment_li = info.select_one("li.comment")
        my_comment = clean(comment_li.get_text(" ", strip=True)) if comment_li else ""

        img = item.select_one("div.pic img")
        cover = img.get("src", "") if img else ""

        rows.append(
            {
                "category": category,
                "title": title,
                "alt_title": alt_title,
                "subject_id": subject_id,
                "url": url,
                "my_rating": my_rating,
                "mark_date": mark_date,
                "my_tags": my_tags,
                "my_comment": my_comment,
                "intro": intro,
                "year": extract_year(intro),
                "cover": cover,
            }
        )

    return rows


def parse_total(html: str) -> int | None:
    """从 "1-15 / 328" 这样的计数里取总数, 只用来显示进度。"""
    soup = BeautifulSoup(html, "lxml")
    node = soup.select_one("span.subject-num") or soup.select_one("span.count")
    if node:
        m = re.search(r"/\s*(\d+)", node.get_text())
        if m:
            return int(m.group(1))
    m = re.search(r"(?:看过|听过|读过)的(?:电影|音乐|书|图书)\s*\((\d+)\)", html)
    if m:
        return int(m.group(1))
    return None


def has_next_page(html: str) -> bool:
    soup = BeautifulSoup(html, "lxml")
    nxt = soup.select_one("span.next a") or soup.select_one("a.next")
    return nxt is not None and bool(nxt.get("href"))


def migrate_csv(path: str):
    """旧格式 CSV (缺 year 等列) 自动升级为当前 FIELDS 格式。
    缺失列补空; year 为空时从已存的 intro 回填。"""
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        header = reader.fieldnames or []
        if header == FIELDS:
            return
        rows = list(reader)
    for row in rows:
        for col in FIELDS:
            row.setdefault(col, "")
        if not row["year"]:
            row["year"] = extract_year(row.get("intro", ""))
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    os.replace(tmp, path)
    print(f"已将 {path} 升级为新格式 (共 {len(rows)} 行, year 已从 intro 回填)")


def load_existing(path: str) -> tuple[set[str], int]:
    """返回 (已有 subject_id 集合, 已有行数), 用于断点续抓和去重。"""
    if not os.path.exists(path):
        return set(), 0
    seen, n = set(), 0
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            n += 1
            sid = (row.get("subject_id") or "").strip()
            if sid:
                seen.add(sid)
    return seen, n


def crawl_category(session: requests.Session, uid: str, category: str,
                   out_path: str, delay_min: float, delay_max: float,
                   max_pages: int | None) -> int:
    conf = CATEGORIES[category]
    host = conf["host"]
    base = f"{host}/people/{uid}/collect"
    per_page = 15  # 三类收藏页每页均为 15 条

    migrate_csv(out_path)
    seen, existing = load_existing(out_path)
    if existing:
        print(f"[{category}] 已有 {existing} 条, 从第 {existing // PAGE_SIZE + 1} 页附近续抓")

    start = (existing // PAGE_SIZE) * PAGE_SIZE
    new_file = not os.path.exists(out_path)

    f = open(out_path, "a", encoding="utf-8-sig", newline="")
    writer = csv.DictWriter(f, fieldnames=FIELDS)
    if new_file:
        writer.writeheader()

    added = 0
    pages = 0
    empty_retries = 0
    referer = f"{host}/people/{uid}/"
    total = None

    try:
        while True:
            url = (f"{base}?start={start}&sort=time&rating=all"
                   f"&filter=all&mode=grid")
            html = fetch(session, url, referer)

            if total is None:
                total = parse_total(html)
                if total:
                    print(f"[{category}] 主页显示共 {total} 条{conf['status_label']}的{conf['label']}")

            items = parse_items(html, category)
            if not items:
                # 空页面分两种: 真的到头了(条目数刚好整除每页数), 或被软限流。
                # 如果已抓数量明显少于主页显示的总数, 按限流处理: 存证据, 等待后重试。
                # total 未知时(比如续抓的第一页就为空)也按疑似限流处理,
                # 宁可多等几分钟, 也不要在还有两千条没抓时误判为结束。
                if empty_retries < 3 and (total is None or len(seen) < total):
                    empty_retries += 1
                    debug_path = os.path.join(
                        os.path.dirname(out_path) or ".",
                        f"douban_debug_{category}_{start}.html")
                    with open(debug_path, "w", encoding="utf-8") as df:
                        df.write(html)
                    wait = 60 * empty_retries
                    print(f"[{category}] start={start} 返回空页面但远未抓完 "
                          f"({len(seen)}/{total or '?'}), 疑似被限流。"
                          f"页面已存到 {debug_path}, 等待 {wait} 秒后重试 "
                          f"({empty_retries}/3)")
                    time.sleep(wait)
                    continue
                print(f"[{category}] start={start} 没有解析到条目, 抓取结束")
                break
            empty_retries = 0

            fresh = [it for it in items if it["subject_id"] not in seen]
            for it in fresh:
                writer.writerow(it)
                seen.add(it["subject_id"])
            f.flush()
            added += len(fresh)
            pages += 1

            done = start + len(items)
            tail = f" / {total}" if total else ""
            print(f"[{category}] start={start} 本页 {len(items)} 条, 新增 {len(fresh)} 条, 累计 {done}{tail}")

            if not has_next_page(html):
                print(f"[{category}] 已到最后一页")
                break
            if max_pages and pages >= max_pages:
                print(f"[{category}] 达到 --max-pages 上限, 停止")
                break

            referer = url
            start += PAGE_SIZE
            time.sleep(random.uniform(delay_min, delay_max))

    except BlockedError as e:
        print(f"[{category}] 被豆瓣拦截: {e}", file=sys.stderr)
        print(f"[{category}] 已写入的数据保留在 {out_path}, 稍后重跑同一条命令会自动续抓。",
              file=sys.stderr)
        raise
    finally:
        f.close()

    return added


def main():
    ap = argparse.ArgumentParser(
        description="抓取豆瓣「看过」的电影和「听过」的音乐, 导出 CSV"
    )
    ap.add_argument("--uid", required=True, help="豆瓣用户 ID (主页 /people/ 后面那段)")
    ap.add_argument("--type",
                    choices=["movie", "music", "book", "all", "both"],
                    default="all",
                    help="抓哪一类, 默认三类都抓 (both 等同 all, 保留兼容)")
    ap.add_argument("--outdir", default=SCRIPT_DIR,
                    help="CSV 输出目录, 默认为脚本所在目录")
    ap.add_argument("--cookie", default=os.environ.get("DOUBAN_COOKIE", ""),
                    help="可选。主页不公开或被限流时填浏览器里的 Cookie 整行, "
                         "也可以用环境变量 DOUBAN_COOKIE")
    ap.add_argument("--delay-min", type=float, default=3.0, help="每页最小间隔秒数")
    ap.add_argument("--delay-max", type=float, default=6.0, help="每页最大间隔秒数")
    ap.add_argument("--max-pages", type=int, default=None,
                    help="每类最多抓多少页, 调试时用, 比如 --max-pages 2")
    ap.add_argument("--user-agent", default=DEFAULT_UA)
    args = ap.parse_args()

    os.makedirs(args.outdir, exist_ok=True)
    session = build_session(args.cookie or None, args.user_agent)

    targets = (["movie", "music", "book"]
               if args.type in ("all", "both") else [args.type])
    results = {}
    failed = []

    for cat in targets:
        out_path = os.path.join(args.outdir, f"douban_{cat}_collect_{args.uid}.csv")
        print(f"\n=== 开始抓取 {CATEGORIES[cat]['label']} -> {out_path} ===")
        try:
            results[cat] = crawl_category(
                session, args.uid, cat, out_path,
                args.delay_min, args.delay_max, args.max_pages,
            )
        except BlockedError:
            failed.append(cat)
        except Exception as e:
            print(f"[{cat}] 出错: {e}", file=sys.stderr)
            failed.append(cat)
        if cat != targets[-1]:
            time.sleep(random.uniform(args.delay_min, args.delay_max))

    print("\n=== 汇总 ===")
    for cat, n in results.items():
        print(f"{CATEGORIES[cat]['label']}: 本次新增 {n} 条")
    if failed:
        print(f"未完成: {', '.join(failed)} (重跑同一命令会续抓)")
        sys.exit(1)


if __name__ == "__main__":
    main()
