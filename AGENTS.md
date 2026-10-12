# Working in this repository

## Scope

The canonical exporter is `telegram_stats_bot.py`; `test_telegram_stats_bot.py`
is its focused fake-client suite. `export_marketing_chat.py` supplies the reused
media parser. Treat the other scripts and files as legacy utilities or local
artifacts unless a task names them.

## Run and verify

Use an interpreter with `requirements.txt` installed. In this workspace,
`/usr/bin/python3` has Telethon and runs the suite:

```bash
/usr/bin/python3 -m unittest -v
```

Tests must remain offline: they use fake clients and temporary directories.
An authenticated smoke test is a separate manual check; do not present it as
completed without running it against the intended account and channels.

## Output contract

Read [docs/output-contract.md](docs/output-contract.md) before changing export
records, paths, or publishing. `channel.csv`, `posts.csv`, and `comments.jsonl`
share one `export_id`; preserve atomic replacement, relative media paths,
channel-folder ownership checks, and the distinction between Telegram's comment
count and the comments actually exported.

If a schema or output behavior changes, update that contract and the relevant
fake-client test in the same change.

## Runtime and safety

Runtime needs `API_ID`, `API_HASH`, and `PHONE`; `OUTPUT_DIR` defaults to
`output`. Keep `.env`, `.session*`, generated exports, and downloaded media
local. The exporter must not join channels automatically or silently overwrite
a folder owned by a different channel ID.

For a batch, collect run-level settings once, reuse the single client, isolate
per-channel permanent failures, and stop the run before later Telegram requests
after a long flood wait or disk-full error.
