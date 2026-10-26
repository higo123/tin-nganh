#!/usr/bin/env python3
"""Lấy tin Bảo hiểm, Nhân sự, Năng lượng xanh từ RSS các báo và ghi ra news.json.

Chạy tự động bằng GitHub Actions (xem news.yml). Chạy tay để thử: python3 update_news.py
Chỉ dùng thư viện có sẵn của Python 3.8+.
"""
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote
from urllib.request import Request, urlopen

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "news.json")
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
MAX_ITEMS = 20
DAYS = 14

# Mỗi nguồn là một feed RSS của báo.
SRC = {
    "bhxh":     ("BHXH Việt Nam", "https://baohiemxahoi.gov.vn/pages/chi-tiet-kenh-rss.aspx?ItemID=2"),
    "vecoTC":   ("VnEconomy", "https://vneconomy.vn/tai-chinh.rss"),
    "vecoDS":   ("VnEconomy", "https://vneconomy.vn/dan-sinh.rss"),
    "vecoXanh": ("VnEconomy", "https://vneconomy.vn/kinh-te-xanh.rss"),
    "vecoHT":   ("VnEconomy", "https://vneconomy.vn/dau-tu-ha-tang.rss"),
    "vecoMoi":  ("VnEconomy", "https://vneconomy.vn/tin-moi.rss"),
    "vnxKD":    ("VnExpress", "https://vnexpress.net/rss/kinh-doanh.rss"),
    "vnxKH":    ("VnExpress", "https://vnexpress.net/rss/khoa-hoc-cong-nghe.rss"),
    "vnxMoi":   ("VnExpress", "https://vnexpress.net/rss/tin-moi-nhat.rss"),
}
RE_INS = "bảo hiểm|bhxh|bhyt|bhtn|phi nhân thọ|nhân thọ|bồi thường|lương hưu|trợ cấp|an sinh"
RE_HR = ("lao động|tuyển dụng|nhân sự|nhân lực|việc làm|tiền lương|lương tối thiểu|thất nghiệp|"
         "nghỉ việc|sa thải|hợp đồng lao động|thị trường lao động|ứng viên|nhà tuyển dụng")
RE_GRN = ("năng lượng|điện mặt trời|điện gió|tái tạo|hydrogen|net zero|carbon|các-bon|phát thải|"
          "lưu trữ điện|pin lưu trữ|xe điện|kinh tế xanh|tăng trưởng xanh|điện sinh khối|quy hoạch điện")

# feeds: (nguồn, bộ lọc từ khóa hoặc None). gnews: từ khóa tìm trên Google News, bổ sung thêm tin.
TOPICS = {
    "ins": {"feeds": [("bhxh", None), ("vecoTC", RE_INS), ("vecoDS", RE_INS), ("vnxKD", RE_INS), ("vecoMoi", RE_INS)],
            "gnews": ["bảo hiểm", "bảo hiểm xã hội", "bảo hiểm nhân thọ"]},
    "hr":  {"feeds": [("vecoDS", RE_HR), ("vecoTC", RE_HR), ("vnxKD", RE_HR), ("vnxMoi", RE_HR), ("bhxh", RE_HR)],
            "gnews": ["tuyển dụng nhân sự", "thị trường lao động", "tiền lương người lao động"]},
    "grn": {"feeds": [("vecoXanh", None), ("vecoHT", RE_GRN), ("vnxKD", RE_GRN), ("vnxKH", RE_GRN), ("vecoMoi", RE_GRN)],
            "gnews": ["năng lượng tái tạo", "điện mặt trời điện gió", "hydrogen xanh"]},
}


def get(url):
    req = Request(url, headers={"User-Agent": UA, "Accept": "application/rss+xml, application/xml, text/xml, */*"})
    with urlopen(req, timeout=25) as r:
        return r.read().decode("utf-8", errors="replace")


def decode(s):
    s = re.sub(r"<!\[CDATA\[([\s\S]*?)\]\]>", r"\1", s or "")
    for a, b in (("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'), ("&#39;", "'"), ("&#039;", "'"),
                 ("&apos;", "'"), ("&nbsp;", " "), ("&amp;", "&")):
        s = s.replace(a, b)
    return s


def plain(s):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]*>", " ", decode(s))).strip()


def tag(block, name):
    m = re.search(r"<%s(?:\s[^>]*)?>([\s\S]*?)</%s>" % (name, name), block, re.I)
    return m.group(1) if m else ""


def parse(text):
    out = []
    for b in re.findall(r"<item[\s>][\s\S]*?</item>", text, re.I):
        try:
            dt = parsedate_to_datetime(plain(tag(b, "pubDate")))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            ts = dt.astimezone(timezone.utc).isoformat()
        except Exception:
            ts = ""
        out.append({"title": plain(tag(b, "title")), "url": plain(tag(b, "link")), "ts": ts,
                    "sum": plain(tag(b, "description")), "src": plain(tag(b, "source"))})
    return [i for i in out if i["title"] and i["url"]]


def cut(s, n=200):
    return s if len(s) <= n else re.sub(r"\s+\S*$", "", s[:n]) + "…"


def gnews_url(q):
    return ("https://news.google.com/rss/search?q=" + quote("%s when:%dd" % (q, DAYS)) +
            "&hl=vi&gl=VN&ceid=VN:vi")


def collect():
    urls = {}
    for key, (name, url) in SRC.items():
        urls[url] = name
    for t in TOPICS.values():
        for q in t["gnews"]:
            urls[gnews_url(q)] = "Google News"
    results, status = {}, {}

    def job(u):
        try:
            return u, parse(get(u)), None
        except Exception as e:
            return u, [], str(e)

    with ThreadPoolExecutor(max_workers=8) as ex:
        for u, items, err in ex.map(job, list(urls)):
            results[u] = items
            status[u] = err

    topics = {}
    for k, t in TOPICS.items():
        items = []
        for s, rx in t["feeds"]:
            name, url = SRC[s]
            r = re.compile(rx, re.I) if rx else None
            for it in results.get(url, []):
                if r and not r.search(it["title"] + " " + it["sum"]):
                    continue
                items.append({"title": it["title"], "url": it["url"], "source": name,
                              "ts": it["ts"], "sum": cut(it["sum"])})
        for q in t["gnews"]:
            for it in results.get(gnews_url(q), []):
                title, source = it["title"], it["src"] or "Google News"
                if source and title.endswith(" - " + source):
                    title = title[: -len(source) - 3].rstrip()
                items.append({"title": title, "url": it["url"], "source": source, "ts": it["ts"], "sum": ""})
        seen, uniq = set(), []
        for it in sorted(items, key=lambda x: x["ts"], reverse=True):
            k2 = re.sub(r"\W+", "", it["title"].lower())
            if k2 in seen:
                continue
            seen.add(k2)
            uniq.append(it)
        topics[k] = uniq[:MAX_ITEMS]
    return topics, status


def main():
    try:
        with open(OUT, encoding="utf-8") as f:
            old = json.load(f)
    except Exception:
        old = {"updated": None, "topics": {}}
    topics, status = collect()
    bad = [u for u, e in status.items() if e]
    for u in bad:
        print("LỖI", u, "->", status[u])
    print("Nguồn đọc được: %d/%d" % (len(status) - len(bad), len(status)))
    for k in TOPICS:
        print(" ", k, len(topics[k]), "tin")
        if not topics[k]:  # không lấy được thì giữ tin cũ
            topics[k] = old.get("topics", {}).get(k, [])
    if not any(topics.values()):
        sys.exit("Không lấy được tin nào")
    if topics == old.get("topics"):
        print("Không có thay đổi, giữ nguyên news.json")
        return
    data = {"updated": datetime.now(timezone.utc).isoformat(), "topics": topics}
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    print("Đã ghi news.json")


if __name__ == "__main__":
    main()
