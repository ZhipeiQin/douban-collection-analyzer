#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
douban_imdb_enrich.py — 读取 douban_export.py 导出的电影 CSV,
逐条访问豆瓣详情页, 提取 IMDb 编号 (以及原名/年份/导演, 供后续匹配用)。

依赖:
    pip install requests beautifulsoup4 lxml

用法 (强烈建议分批跑):
    $env:DOUBAN_COOKIE = '你的Cookie'
    python douban_imdb_enrich.py --input douban_movie_collect_<你的ID>.csv --limit 300
    # 之后每次重跑同一条命令, 会自动跳过已完成的条目, 抓下一批 300 条
    # 全部抓完后, 会生成合并文件 douban_movie_with_imdb.csv

设计:
  * 进度保存在 imdb_progress.csv, 每抓一条立刻落盘, 断在哪都不丢。
  * 详情页没有 IMDb 编号的条目记为 NOT_FOUND, 不会反复重抓。
  * 404 (条目被删) 记为 GONE。
  * 被限流时自动等 2/4/6 分钟重试, 三次失败保存进度退出。
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

DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

PROGRESS_FIELDS = ["subject_id", "imdb_id", "orig_title", "year", "directors"]

# 输出默认放在脚本所在目录, 与运行时的当前目录无关
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


class BlockedError(RuntimeError):
    pass


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


def fetch_subject(session: requests.Session, subject_id: str,
                  referer: str, timeout: int = 20) -> str | None:
    """返回 HTML; 条目不存在返回 None; 被拦截抛 BlockedError。"""
    url = f"https://movie.douban.com/subject/{subject_id}/"
    r = session.get(url, headers={"Referer": referer}, timeout=timeout,
                    allow_redirects=True)
    if r.status_code == 404:
        return None
    if r.status_code == 403:
        raise BlockedError(f"403: {url}")
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}: {url}")
    r.encoding = r.apparent_encoding or "utf-8"
    html = r.text
    final = r.url
    if "accounts.douban.com" in final or "/login" in final:
        raise BlockedError("被跳转到登录页")
    if ("sec.douban.com" in final or "sec.douban.com" in html
            or "检测到有异常请求" in html or "禁止访问" in html):
        raise BlockedError("触发人机验证")
    return html


def parse_subject(html: str) -> dict:
    """从详情页提取 imdb_id / 原名 / 年份 / 导演。"""
    soup = BeautifulSoup(html, "lxml")

    # IMDb 编号: div#info 里 <span class="pl">IMDb:</span> tt0111161
    imdb_id = ""
    info = soup.select_one("#info")
    info_text = info.get_text(" ", strip=True) if info else ""
    m = re.search(r"IMDb[:：]?\s*(tt\d{6,10})", info_text or html)
    if m:
        imdb_id = m.group(1)

    # 完整标题: "肖申克的救赎 The Shawshank Redemption"
    title_node = soup.select_one('span[property="v:itemreviewed"]')
    full_title = title_node.get_text(strip=True) if title_node else ""
    # 去掉开头的中文名, 剩下的当原名; 纯中文片名时原名为空
    m2 = re.match(r"^([^\x00-\x7f].*?)\s+(\S.*)$", full_title)
    orig_title = ""
    if m2 and re.search(r"[A-Za-z]", m2.group(2)):
        orig_title = m2.group(2).strip()

    year = ""
    year_node = soup.select_one("span.year")
    if year_node:
        m3 = re.search(r"(\d{4})", year_node.get_text())
        if m3:
            year = m3.group(1)

    directors = ", ".join(
        a.get_text(strip=True) for a in soup.select('a[rel="v:directedBy"]')
    )

    return {"imdb_id": imdb_id, "orig_title": orig_title,
            "year": year, "directors": directors}


def load_progress(path: str) -> dict[str, dict]:
    done = {}
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                sid = (row.get("subject_id") or "").strip()
                if sid:
                    done[sid] = row
    return done


def merge_output(input_csv: str, progress: dict[str, dict], out_path: str):
    with open(input_csv, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        in_fields = reader.fieldnames or []

    extra = ["imdb_id", "orig_title", "year", "directors"]
    out_fields = in_fields + [c for c in extra if c not in in_fields]
    with open(out_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=out_fields)
        w.writeheader()
        for row in rows:
            p = progress.get((row.get("subject_id") or "").strip(), {})
            for c in extra:
                v = p.get(c, "")
                if v in ("NOT_FOUND", "GONE"):
                    v = ""
                # 详情页有值就用详情页的; 否则保留输入 CSV 里已有的值
                # (比如列表页提取的 year), 不要抹空
                row[c] = v or row.get(c, "")
            w.writerow(row)


def main():
    ap = argparse.ArgumentParser(description="给豆瓣电影 CSV 补充 IMDb 编号")
    ap.add_argument("--input", required=True,
                    help="douban_export.py 生成的电影 CSV")
    ap.add_argument("--progress",
                    default=os.path.join(SCRIPT_DIR, "imdb_progress.csv"),
                    help="进度文件, 默认在脚本所在目录")
    ap.add_argument("--output",
                    default=os.path.join(SCRIPT_DIR, "douban_movie_with_imdb.csv"),
                    help="合并输出文件, 默认在脚本所在目录")
    ap.add_argument("--limit", type=int, default=300,
                    help="本次最多抓多少条, 默认 300, 0 表示不限")
    ap.add_argument("--cookie", default=os.environ.get("DOUBAN_COOKIE", ""))
    ap.add_argument("--delay-min", type=float, default=8.0)
    ap.add_argument("--delay-max", type=float, default=15.0)
    ap.add_argument("--user-agent", default=DEFAULT_UA)
    args = ap.parse_args()

    # 输入文件: 当前目录找不到时, 去脚本所在目录找
    if not os.path.exists(args.input):
        alt = os.path.join(SCRIPT_DIR, args.input)
        if not os.path.isabs(args.input) and os.path.exists(alt):
            args.input = alt
        else:
            sys.exit(f"找不到输入文件 {args.input}")

    with open(args.input, "r", encoding="utf-8-sig", newline="") as f:
        subjects = []
        for row in csv.DictReader(f):
            sid = (row.get("subject_id") or "").strip()
            if sid:
                subjects.append((sid, row.get("title", "")))

    progress = load_progress(args.progress)
    todo = [(sid, t) for sid, t in subjects if sid not in progress]
    print(f"共 {len(subjects)} 部, 已完成 {len(progress)}, 待抓 {len(todo)}")

    if todo:
        session = build_session(args.cookie or None, args.user_agent)
        new_file = not os.path.exists(args.progress)
        pf = open(args.progress, "a", encoding="utf-8-sig", newline="")
        writer = csv.DictWriter(pf, fieldnames=PROGRESS_FIELDS)
        if new_file:
            writer.writeheader()

        batch = todo if args.limit == 0 else todo[: args.limit]
        referer = "https://movie.douban.com/"
        blocked_retries = 0

        try:
            for i, (sid, title) in enumerate(batch, 1):
                while True:
                    try:
                        html = fetch_subject(session, sid, referer)
                        blocked_retries = 0
                        break
                    except BlockedError as e:
                        blocked_retries += 1
                        if blocked_retries > 3:
                            print(f"\n连续被拦 3 次 ({e}), 保存进度退出。"
                                  f"等几小时后重跑同一命令即可续抓。",
                                  file=sys.stderr)
                            pf.close()
                            merge_output(args.input, load_progress(args.progress),
                                         args.output)
                            sys.exit(1)
                        wait = 120 * blocked_retries
                        print(f"  被拦截 ({e}), 等 {wait} 秒后重试 "
                              f"({blocked_retries}/3)")
                        time.sleep(wait)
                    except requests.RequestException as e:
                        print(f"  网络错误 {e}, 等 30 秒重试")
                        time.sleep(30)

                if html is None:
                    rec = {"subject_id": sid, "imdb_id": "GONE",
                           "orig_title": "", "year": "", "directors": ""}
                else:
                    d = parse_subject(html)
                    rec = {"subject_id": sid,
                           "imdb_id": d["imdb_id"] or "NOT_FOUND",
                           "orig_title": d["orig_title"],
                           "year": d["year"],
                           "directors": d["directors"]}
                writer.writerow(rec)
                pf.flush()
                progress[sid] = rec

                tag = rec["imdb_id"]
                print(f"[{len(progress)}/{len(subjects)}] {title} -> {tag}")

                referer = f"https://movie.douban.com/subject/{sid}/"
                if i < len(batch):
                    time.sleep(random.uniform(args.delay_min, args.delay_max))
        finally:
            pf.close()

    progress = load_progress(args.progress)
    merge_output(args.input, progress, args.output)

    n_ok = sum(1 for v in progress.values()
               if v["imdb_id"].startswith("tt"))
    n_nf = sum(1 for v in progress.values() if v["imdb_id"] == "NOT_FOUND")
    n_gone = sum(1 for v in progress.values() if v["imdb_id"] == "GONE")
    remain = len(subjects) - len(progress)
    print(f"\n=== 进度 ===")
    print(f"有 IMDb 编号: {n_ok}  无编号: {n_nf}  条目已删: {n_gone}  未抓: {remain}")
    print(f"合并结果已写入 {args.output}")
    if remain:
        print("重跑同一条命令继续抓下一批。")


if __name__ == "__main__":
    main()
