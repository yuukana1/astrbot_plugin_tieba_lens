"""Explicit opt-in live public-read probe, intended to run on the bot server.

python client_probe.py 'https://tieba.baidu.com/f?kw=原神' --output /tmp/tieba-forum.json
Never reads cookies, credentials, vaults, or chat messages. No remote mutations.
"""
from __future__ import annotations
import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from astrbot_plugin_tieba_lens.client import TiebaClient, TiebaError
from astrbot_plugin_tieba_lens.links import parse_url


async def run(args):
    target = parse_url(args.url)
    if not target:
        raise SystemExit("Unsupported Tieba URL")
    provider = TiebaClient({"request_timeout": 20, "request_delay": 1.0, "retries": 1,
                           "comment_limit": 8, "subcomment_limit": 6, "forum_thread_limit": 8})
    try:
        doc = await provider.parse(target)
        if args.output:
            Path(args.output).write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"ok": True, "kind": doc["kind"], "title": doc["title"],
            "url": doc["url"], "posts": len(doc["posts"]), "threads": len(doc["threads"]),
            "levels_present": sum(post["author"]["level"] is not None for post in doc["posts"]),
            "subcomments": sum(len(post["comments"]) for post in doc["posts"]),
            "warnings": doc["warnings"], "diagnostics": provider.diagnostics()}, ensure_ascii=False))
    except TiebaError as exc:
        print(json.dumps({"ok": False, "code": exc.code, "error": str(exc)}, ensure_ascii=False))
        raise SystemExit(2)
    finally:
        await provider.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url")
    parser.add_argument("--output")
    asyncio.run(run(parser.parse_args()))
