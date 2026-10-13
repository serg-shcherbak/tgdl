# tgdl — Telegram channel exporter

`telegram_stats_bot.py` is an interactive Telethon CLI that exports one or more
Telegram channels into compact, analysis-ready files. It can include channel
metadata, posts, comments, and downloadable media.

## Run

Create a virtual environment and install the dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
cp .env.example .env
```

Fill `API_ID`, `API_HASH`, and `PHONE` in your local `.env` file. Get your API
credentials from [my.telegram.org](https://my.telegram.org). `PHONE` is the phone
number of your Telegram account, including the country code. The first run may
ask for the Telegram login code and your two-step verification password.

Then run:

```bash
python3 telegram_stats_bot.py
```

At startup choose `1` (or Enter) for a new export, or `2` to add unfinished
comments to an existing channel folder. Mode 2 asks only for the saved folder;
it uses the saved posts and skips completed threads without downloading media.
You can also start that mode directly:

```bash
python3 telegram_stats_bot.py --resume-comments output/shcherbaks
```

After Cmd+C, use this mode again: partially collected threads continue from
their saved comment IDs, and saved comments are retained without duplicates.
Finish or interrupt the previous export before resuming into the same folder.
Existing post text, sorting and downloaded media remain available. This resumes
the saved snapshot; it does not fetch new posts or new replies above an existing
cursor/in completed threads. A new export is needed to refresh the channel.

`API_ID`, `API_HASH`, and `PHONE` are read only at runtime, so importing the
module and running its fake-client tests does not require secrets. `OUTPUT_DIR`
is optional and defaults to `output`.

Run the offline tests with the same interpreter:

```bash
python3 -m unittest -v
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

The CLI prints separate live counts for posts, comments, completed threads and
media, and a final success/failure summary. Permanent errors for one channel do not prevent later channels from
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

Posts are collected first while up to three media downloads run concurrently
on the same Telegram client. The CLI prints `Посты готовы: <folder>`: all posts,
channel information and `AGENTS.md` are readable immediately. Requested comments
then load in up to two separate tasks, while media continues downloading.
Final publication adds comments, paths and statuses once collection and downloads
finish. Cmd+C after `Посты готовы` preserves posts and saves comments already
collected where possible, marking unfinished threads explicitly. A failed
attachment keeps its text and metadata. There are no new dependencies or background services.

The three data files form one snapshot only when both `export_id` and
`export_revision` match. They are replaced atomically per file. Revision 1 is
the first snapshot; revision 2 is the final update when comments or media were queued.
Before that update the exporter preserves revision 1 in a temporary
`.text-ready-<export_id>/` directory, removed after successful verification.
If the update fails, it restores revision 1 where possible; otherwise the
preserved directory remains readable. `AGENTS.md` explains this fallback.

History and reply pages share a limit of one request start per second; messages
inside a page are processed without a per-message delay. Short FloodWait responses
pause all new requests and reduce the rate. A wait over 60 seconds stops the run.
This reduces flood risk but cannot guarantee its absence. Network reconnection
timeouts can still slow an export independently of comments.

Pass the channel folder to an agent to analyze it or build HTML articles.
Album IDs, explicit attachment order, raw captions and text entities preserve
the context needed for reconstruction. The exporter does not build HTML itself.
See [the output contract](docs/output-contract.md) for schemas, joins, and
status fields. A user-created `AGENTS.md` without the generator marker is never
silently overwritten.

The exporter accesses only channels already available to the authenticated
account. It never joins a channel automatically. Downloaded media and session
files may contain sensitive data; keep them out of version control.
The repository ignores local credentials, Telegram sessions, downloaded media
and generated exports. `.env.example` contains empty placeholders only; never
put real credentials in it or publish your `.session` files.

`telegram_stats_bot.py` is the supported exporter. The other scripts are legacy
utilities; `export_marketing_chat.py` supplies its reused media parser.

## Verification scope

The test suite validates parsing, output schemas, generation integrity, comments,
media handling, folder collisions, flood waits, disk-full cleanup, batch
isolation, and console progress with fake Telegram clients. It does not replace
an authenticated live Telegram smoke test.
