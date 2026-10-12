# Telegram channel exporter

`telegram_stats_bot.py` is an interactive Telethon CLI that exports one or more
Telegram channels into compact, analysis-ready files. It can include channel
metadata, posts, comments, and downloadable media.

## Run

Install the packages in `requirements.txt`, create a local `.env`, then run:

```bash
python3 -m pip install -r requirements.txt
python3 telegram_stats_bot.py
```

The required `.env` variables are:

```dotenv
API_ID=123456
API_HASH=your_telegram_api_hash
PHONE=+79990000000
# Optional. Defaults to output.
OUTPUT_DIR=output
```

`API_ID`, `API_HASH`, and `PHONE` are read only at runtime, so importing the
module and running its fake-client tests does not require secrets.

On the current workspace, `/usr/bin/python3` has the required Telethon package:

```bash
/usr/bin/python3 -m unittest -v
```

## Interactive input

At `Канал(ы):`, provide any of the following:

- one `@channel`, channel name, or `https://t.me/channel` URL;
- several names or URLs separated by spaces or commas;
- a quoted path to a UTF-8 text file with one channel per line.

Then enter the inclusive date range (`DD-MM-YYYY`, `DD.MM.YYYY`, or
`DD/MM/YYYY`), for example `12-10-2026`, choose sorting, and opt into comments.
Year-first dates remain accepted for compatibility. Day boundaries remain UTC;
dates in exported files use ISO 8601.

Choose media with one letter (case-insensitive):

| Choice | Download |
|---|---|
| `N` or Enter | Nothing; keep metadata |
| `P` | Photos and images sent as files |
| `V` | Videos and round video messages |
| `A` | All downloadable attachments, including audio, documents, animations and stickers |

GIF/animations and stickers belong to `A`, even when stored as video/image files.
The choice applies to posts and exported comments. Protected attachments are
never downloaded. One set of settings applies to the full
batch; duplicate aliases are resolved once per Telegram channel ID.

The CLI prints a live per-channel post counter and a final success/failure
summary. Permanent errors for one channel do not prevent later channels from
running. A long flood wait or disk-full error stops the batch before another
Telegram request.

## Output

Each successfully resolved channel gets its own subdirectory under `OUTPUT_DIR`:

```text
output/
└── <username-or-sanitized-title>/
    ├── channel.csv
    ├── posts.csv
    ├── comments.jsonl
    ├── AGENTS.md              # self-contained guide for an agent using this folder
    └── media/                 # only when files were downloaded
```

One task collects records while up to three media downloads run concurrently
on the same Telegram client. Once posts and requested comments are collected,
the CLI prints `Тексты готовы: <folder>`: the three data files and `AGENTS.md`
are readable while media continues downloading. Final publication updates paths
and statuses once all downloads finish. A failed attachment keeps its text and
metadata. There are no new dependencies or background services.

The three data files form one snapshot only when both `export_id` and
`export_revision` match. They are replaced atomically per file. Revision 1 is
the first snapshot; revision 2 is the final update when media was queued.
Before that update the exporter preserves revision 1 in a temporary
`.text-ready-<export_id>/` directory, removed after successful verification.
If the update fails, it restores revision 1 where possible; otherwise the
preserved directory remains readable. `AGENTS.md` explains this fallback.

Pass the channel folder to an agent to analyze it or build HTML articles.
Album IDs, explicit attachment order, raw captions and text entities preserve
the context needed for reconstruction. The exporter does not build HTML itself.
See [the output contract](docs/output-contract.md) for schemas, joins, and
status fields. A user-created `AGENTS.md` without the generator marker is never
silently overwritten.

The exporter accesses only channels already available to the authenticated
account. It never joins a channel automatically. Downloaded media and session
files may contain sensitive data; keep them out of version control.

## Verification scope

The test suite validates parsing, output schemas, generation integrity, comments,
media handling, folder collisions, flood waits, disk-full cleanup, batch
isolation, and console progress with fake Telegram clients. It does not replace
an authenticated live Telegram smoke test.
