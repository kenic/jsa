"""J-STAGE『日本の科学者』の新着論文を X に自動投稿するボット。

使い方:
  python bot.py --init     # 初回のみ: 現在フィードにある記事を「投稿済み」として記録（投稿はしない）
  python bot.py            # 新着を投稿
  python bot.py --dry-run  # 投稿せずに、投稿予定の文面を表示するだけ

環境変数（GitHub Secrets に登録）:
  X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_TOKEN_SECRET
任意:
  FEED_URL     ... 既定は『日本の科学者』の論文RSS
  MAX_POSTS    ... 1回の実行で投稿する最大件数（既定 5）
  POST_INTERVAL ... 投稿間隔の秒数（既定 30）
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import feedparser

FEED_URL = os.environ.get(
    "FEED_URL",
    "https://www.jstage.jst.go.jp/AF05S010NewRssDld?btnaction=JT0041&sryCd=jjsci&rssLang=ja",
)
JOURNAL_NAME = "日本の科学者"
STATE_FILE = Path(__file__).with_name("posted.json")
MAX_POSTS = int(os.environ.get("MAX_POSTS", "5"))
POST_INTERVAL = int(os.environ.get("POST_INTERVAL", "30"))
USER_AGENT = "jstage-x-bot/1.0 (+https://github.com/)"

DOI_RE = re.compile(r"10\.\d{4,9}/[^\s<>\"'\]]+")


# ---------- 記録ファイル ----------

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"posted": {}}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


# ---------- フィード取得 ----------

def fetch_entries() -> list[dict]:
    feed = feedparser.parse(FEED_URL, agent=USER_AGENT)
    if feed.bozo and not feed.entries:
        raise RuntimeError(f"フィードの取得に失敗しました: {feed.bozo_exception}")

    articles = []
    for e in feed.entries:
        link = e.get("link", "").strip()
        summary = e.get("summary", "")
        m = DOI_RE.search(summary) or DOI_RE.search(link)
        doi = m.group(0).rstrip(".,;)") if m else None
        authors = [a.get("name", "").strip() for a in e.get("authors", []) if a.get("name")]
        if not authors and e.get("author"):
            authors = [s.strip() for s in re.split(r"[,、，]", e["author"]) if s.strip()]
        articles.append(
            {
                "key": doi or e.get("id") or link,  # 重複判定に使うID
                "title": clean(e.get("title", "")),
                "authors": authors,
                "url": f"https://doi.org/{doi}" if doi else link,
                "published": e.get("published", e.get("updated", "")),
            }
        )
    # 古い順に投稿したいので公開日でソート
    articles.sort(key=lambda a: a["published"])
    return articles


def clean(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\s+", " ", text).strip()


# ---------- 投稿文の作成 ----------

def weighted_len(text: str) -> int:
    """X の文字数カウント（URLは23、日本語などは2、英数字は1）の近似。"""
    total = 0
    for token in re.split(r"(https?://\S+)", text):
        if token.startswith(("http://", "https://")):
            total += 23
            continue
        for ch in token:
            cp = ord(ch)
            light = (
                cp <= 0x10FF
                or 0x2000 <= cp <= 0x200D
                or 0x2010 <= cp <= 0x201F
                or 0x2032 <= cp <= 0x2037
            )
            total += 1 if light else 2
    return total


def build_text(a: dict, limit: int = 280) -> str:
    head = f"【{JOURNAL_NAME}】新着論文"
    if a["authors"]:
        author = a["authors"][0] + (" ほか" if len(a["authors"]) > 1 else "")
    else:
        author = ""

    def compose(title: str, author: str) -> str:
        lines = [head, title]
        if author:
            lines.append(author)
        lines.append(a["url"])
        return "\n".join(lines)

    title = a["title"]
    text = compose(title, author)
    if weighted_len(text) > limit and author:
        author = ""  # まず著者を省略
        text = compose(title, author)
    while weighted_len(text) > limit and len(title) > 1:
        title = title[:-1]
        text = compose(title.rstrip() + "…", author)
    return text


# ---------- X への投稿 ----------

def make_client():
    import tweepy

    keys = ["X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_TOKEN_SECRET"]
    missing = [k for k in keys if not os.environ.get(k)]
    if missing:
        raise RuntimeError(f"環境変数が足りません: {', '.join(missing)}")
    return tweepy.Client(
        consumer_key=os.environ["X_API_KEY"],
        consumer_secret=os.environ["X_API_SECRET"],
        access_token=os.environ["X_ACCESS_TOKEN"],
        access_token_secret=os.environ["X_ACCESS_TOKEN_SECRET"],
    )


# ---------- メイン ----------

def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--init", action="store_true", help="現在の記事を投稿済みとして記録するだけ")
    p.add_argument("--dry-run", action="store_true", help="投稿せず文面を表示する")
    args = p.parse_args()

    state = load_state()
    posted = state.setdefault("posted", {})
    articles = fetch_entries()
    new = [a for a in articles if a["key"] not in posted]
    print(f"フィード {len(articles)} 件 / 未投稿 {len(new)} 件")

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    if args.init:
        for a in new:
            posted[a["key"]] = {"title": a["title"], "at": now, "init": True}
        save_state(state)
        print(f"{len(new)} 件を投稿済みとして記録しました（投稿はしていません）")
        return 0

    if not posted and new and not args.dry_run:
        print("posted.json が空です。過去記事を一斉投稿しないよう、先に --init を実行してください。")
        return 1

    targets = new[:MAX_POSTS]
    client = None if args.dry_run else make_client()

    failures = 0
    for i, a in enumerate(targets):
        text = build_text(a)
        if args.dry_run:
            print("-" * 40)
            print(text)
            print(f"(weighted length: {weighted_len(text)})")
            continue
        try:
            res = client.create_tweet(text=text)
            tweet_id = res.data.get("id") if res and res.data else None
            posted[a["key"]] = {"title": a["title"], "at": now, "tweet_id": tweet_id}
            save_state(state)  # 1件ごとに保存して、途中で落ちても記録を残す
            print(f"投稿しました: {a['title']}")
        except Exception as ex:  # noqa: BLE001
            failures += 1
            print(f"投稿に失敗しました: {a['title']} ({ex})", file=sys.stderr)
        if i < len(targets) - 1:
            time.sleep(POST_INTERVAL)

    if len(new) > len(targets):
        print(f"残り {len(new) - len(targets)} 件は次回以降に投稿します")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
