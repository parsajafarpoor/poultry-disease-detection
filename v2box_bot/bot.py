"""Agentic Telegram bot that finds fresh V2Box/V2Ray subscription links on GitHub
and posts them to a Telegram channel.

Claude drives the search with a small set of tools (GitHub search, repo file
listing, subscription checking). Only links that the `check_subscription` tool
actually downloaded and validated in this run can be published, so the model
cannot post a URL it made up.

Environment variables:
    ANTHROPIC_API_KEY     Claude API key
    TELEGRAM_BOT_TOKEN    token from @BotFather
    TELEGRAM_CHANNEL_ID   e.g. @my_channel or -100123456789 (the bot must be a channel admin)
    GITHUB_TOKEN          optional, raises GitHub API rate limits (set automatically in Actions)
    DRY_RUN               optional, "1" prints the post instead of sending it
"""

from __future__ import annotations

import base64
import binascii
import html
import json
import os
import re
import sys
from datetime import datetime, timezone
from urllib.parse import quote, urlparse

import anthropic
import requests
from anthropic import beta_tool

MODEL = "claude-opus-5-5"
MAX_LINKS = 8
HTTP_TIMEOUT = 30
PROTOCOLS = ("vless", "vmess", "trojan", "ss", "ssr", "hysteria2", "hy2", "tuic", "wireguard")
CONFIG_RE = re.compile(r"^(%s)://" % "|".join(PROTOCOLS), re.IGNORECASE | re.MULTILINE)
ALLOWED_HOSTS = {"raw.githubusercontent.com", "github.com", "gist.githubusercontent.com"}

session = requests.Session()
session.headers["User-Agent"] = "v2box-telegram-bot"
# Sent only to api.github.com, never to the raw subscription hosts
GITHUB_HEADERS = {"Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}"} if os.environ.get("GITHUB_TOKEN") else {}

# url -> stats, filled only by check_subscription
verified: dict[str, dict] = {}
# what Claude chose to publish, filled by publish_links
selected: list[dict] = []


def _github_api(path: str, params: dict | None = None) -> dict | list:
    resp = session.get(f"https://api.github.com{path}", params=params, headers=GITHUB_HEADERS, timeout=HTTP_TIMEOUT)
    if not resp.ok:
        raise RuntimeError(f"GitHub API {resp.status_code}: {resp.text[:200]}")
    return resp.json()


def _decode_subscription(text: str) -> str:
    """Subscriptions are either plain config lines or one base64 blob."""
    if CONFIG_RE.search(text):
        return text
    compact = "".join(text.split())
    try:
        return base64.b64decode(compact + "=" * (-len(compact) % 4)).decode("utf-8", "ignore")
    except (binascii.Error, ValueError):
        return text


@beta_tool
def search_github_repos(query: str, pushed_within_days: int = 3) -> str:
    """Search GitHub for repositories that publish free V2Ray subscription links.

    Args:
        query: GitHub search keywords, e.g. "v2ray configs subscription" or "vless sub".
        pushed_within_days: Only return repos pushed to within this many days.
    """
    since = datetime.now(timezone.utc).date().toordinal() - pushed_within_days
    since_date = datetime.fromordinal(since).date().isoformat()
    try:
        data = _github_api(
            "/search/repositories",
            {"q": f"{query} pushed:>={since_date}", "sort": "stars", "order": "desc", "per_page": 15},
        )
    except (RuntimeError, requests.RequestException) as e:
        return json.dumps({"error": str(e)})
    repos = [
        {
            "repo": r["full_name"],
            "stars": r["stargazers_count"],
            "pushed_at": r["pushed_at"],
            "default_branch": r["default_branch"],
            "description": (r.get("description") or "")[:200],
        }
        for r in data.get("items", [])
    ]
    return json.dumps(repos, ensure_ascii=False)


@beta_tool
def list_repo_files(repo: str, path: str = "") -> str:
    """List files in a GitHub repository directory, with their raw download URLs.

    Args:
        repo: "owner/name".
        path: Directory inside the repo; empty for the root.
    """
    try:
        items = _github_api(f"/repos/{repo}/contents/{quote(path)}")
    except (RuntimeError, requests.RequestException) as e:
        return json.dumps({"error": str(e)})
    if isinstance(items, dict):
        items = [items]
    files = [
        {"name": i["name"], "type": i["type"], "size": i.get("size"), "raw_url": i.get("download_url")}
        for i in items
    ]
    return json.dumps(files[:100], ensure_ascii=False)


@beta_tool
def check_subscription(url: str) -> str:
    """Download a subscription URL and report how many working-looking configs it holds.

    Only URLs checked with this tool can be published.

    Args:
        url: Raw subscription URL on raw.githubusercontent.com, github.com/.../raw/..., or a gist.
    """
    host = urlparse(url).hostname or ""
    if host not in ALLOWED_HOSTS:
        return json.dumps({"ok": False, "error": f"host {host!r} not allowed"})
    try:
        resp = session.get(url, timeout=HTTP_TIMEOUT)
    except requests.RequestException as e:
        return json.dumps({"ok": False, "error": str(e)})
    if resp.status_code != 200:
        return json.dumps({"ok": False, "error": f"HTTP {resp.status_code}"})
    configs = CONFIG_RE.findall(_decode_subscription(resp.text))
    counts: dict[str, int] = {}
    for proto in configs:
        counts[proto.lower()] = counts.get(proto.lower(), 0) + 1
    stats = {"ok": bool(configs), "total": len(configs), "by_protocol": counts, "size_kb": len(resp.content) // 1024}
    if configs:
        verified[url] = stats
    return json.dumps(stats)


@beta_tool
def publish_links(links: list[dict]) -> str:
    """Hand over the final list of subscription links to post in the channel. Call once, at the end.

    Args:
        links: Up to 8 items, each {"url": <checked subscription URL>, "title": <short label, e.g. repo name and what the file holds>}.
    """
    accepted, rejected = [], []
    for item in links[:MAX_LINKS]:
        url = item.get("url", "")
        if url in verified:
            accepted.append({"url": url, "title": str(item.get("title", ""))[:80], **verified[url]})
        else:
            rejected.append(url)
    selected[:] = accepted
    return json.dumps({"accepted": len(accepted), "rejected_unchecked": rejected})


SYSTEM_PROMPT = f"""You find free, public V2Ray subscription links on GitHub for users of the V2Box app.

Work like this:
1. Use search_github_repos with a few different queries (e.g. "v2ray configs", "free v2ray subscription",
   "vless vmess sub", "v2ray-configs") to find repos updated in the last few days. Well-known sources include
   Epodonios/v2ray-configs, barry-far/V2ray-Config, MatinGhanbari/v2ray-configs, 0xRadikal/Free-v2ray-Configs,
   ebrasha/free-v2ray-public-list, but prefer whatever is fresh and active today.
2. Use list_repo_files to find each repo's main subscription file(s) (often named *sub*.txt, All_Configs*,
   *base64*, or inside folders like sub/, Splitted-By-Protocol/).
3. Verify every candidate with check_subscription. Skip anything that fails or has very few configs.
4. Pick up to {MAX_LINKS} good links from different repos, mixing "all configs" files with a few
   protocol-specific ones (vless, vmess, trojan, ss) when available. Then call publish_links once.

Only publish URLs that check_subscription returned ok for. Keep tool calls efficient."""


def run_agent() -> None:
    client = anthropic.Anthropic()
    runner = client.beta.messages.tool_runner(
        model=MODEL,
        max_tokens=16000,
        thinking={"type": "adaptive"},
        output_config={"effort": "medium"},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        system=SYSTEM_PROMPT,
        tools=[search_github_repos, list_repo_files, check_subscription, publish_links],
        messages=[{"role": "user", "content": "Find today's best V2Box subscription links and publish them."}],
    )
    for message in runner:
        if message.stop_reason == "refusal":
            raise RuntimeError(f"model declined: {message.stop_details}")
        for block in message.content:
            if block.type == "tool_use":
                print(f"-> {block.name}({json.dumps(block.input, ensure_ascii=False)[:200]})", flush=True)


def build_post() -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [f"🔗 <b>لینک‌های اشتراک V2Box</b>\n🕒 {now}\n"]
    for i, link in enumerate(selected, 1):
        protos = ", ".join(f"{k}:{v}" for k, v in sorted(link["by_protocol"].items(), key=lambda kv: -kv[1])[:4])
        lines.append(
            f"<b>{i}. {html.escape(link['title'])}</b>\n"
            f"📊 {link['total']} کانفیگ ({html.escape(protos)})\n"
            f"<code>{html.escape(link['url'])}</code>\n"
        )
    lines.append(
        "📲 V2Box ← Configs ← ➕ ← Add Subscription ← لینک را paste کنید ← Update\n"
        "⚠️ این سرورها عمومی و ناشناس هستند؛ برای کارهای حساس (بانک، ایمیل کاری) استفاده نکنید."
    )
    return "\n".join(lines)


def send_to_telegram(text: str) -> None:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    resp = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={
            "chat_id": os.environ["TELEGRAM_CHANNEL_ID"],
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        timeout=HTTP_TIMEOUT,
    )
    if not resp.ok:
        raise RuntimeError(f"Telegram error {resp.status_code}: {resp.text}")


def main() -> int:
    run_agent()
    if not selected:
        print("No verified links were published; nothing sent.", file=sys.stderr)
        return 1
    post = build_post()
    if os.environ.get("DRY_RUN") == "1":
        print(post)
    else:
        send_to_telegram(post)
        print(f"Posted {len(selected)} links.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
