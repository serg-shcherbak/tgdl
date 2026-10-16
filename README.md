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

At startup choose `1` (or Enter) for a new export in a separate folder. Previous
exports, including their media and any user files, are left unchanged. A narrow
date range or an empty result never replaces an older dataset. Choose `2` to add
unfinished comments to an existing channel folder. Mode 2 asks only for the saved folder;
it uses the saved posts and skips completed threads without downloading media.
You can also start that mode directly:

```bash
python3 telegram_stats_bot.py --resume-comments output/shcherbaks
```

After Cmd+C, use this mode again: partially collected threads continue from
their saved comment IDs, and saved comments are retained without duplicates.
Every active exporter holds an exclusive OS writer lock for its dataset folder.
A second resume is refused before reading that snapshot or requesting Telegram
data. The lock is released after completion, cancellation or process exit.
The `.export.lock` file remains on disk; its existence does not mean the folder
is busy. Do not delete it to bypass an active writer.
Use the exact folder path printed for the intended export, including its
timestamp suffix when present.
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

- one `@channel`, channel name, `t.me/channel` or `https://t.me/channel` URL
  (post links and `t.me/s/channel` previews also select the channel);
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
Poll questions, answers and available results are exported as text in every mode,
including `N`, without a separate option. Read `text` together with `poll_text` in
the same post or comment record. Original text and its formatting remain intact.
Missing poll results are unknown, not zero. Locations (including live locations
and venues) are excluded; the message row and its original text remain.
Contacts, link previews, dice and giveaways retain their available metadata.
Stories and paid media retain references/metadata; their nested files are not
downloaded by this exporter.
The choice applies to posts and exported comments. Protected attachments are
never downloaded. One set of settings applies to the full
batch; duplicate aliases are resolved once per Telegram channel ID.

The CLI prints separate live counts for posts, comments, completed threads and
media, and a final summary with each saved folder's path. Empty periods and
incomplete comments/media receive explicit warnings. A saved dataset can be
partial; inspect its status fields. The exit code still reports channel-level
failures, not attachment failures or incomplete threads.
Comment counts can grow during export: a normally finished thread is complete
when exported records meet or exceed Telegram's initial advertised count.
Permanent errors for one channel do not prevent later channels from
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
    ├── .export.lock           # persistent file; OS lock is held only by a writer
    └── media/                 # only when files were downloaded
```

If that channel folder already exists, a new export uses
`<name>__<UTC timestamp>_<random suffix>/`, for example
`shcherbaks__20261015T072125Z_a1b2c3d4/`. Folder creation reserves a unique name,
including when the previous folder is still empty or contains only `.export.lock`.
A base folder with other contents owned
by a different or unknown channel is refused without changing its contents.
`output/shcherbaks` remains the original export; it does not point to the latest
run. Consumers must use the returned or printed folder path. Each new export
downloads its own media files, so repeated exports need additional disk space.

Posts are collected first while up to three media downloads run concurrently
on the same Telegram client. The CLI prints `Посты готовы: <folder>`: all posts,
channel information and `AGENTS.md` are readable immediately. Requested comments
then load in up to two separate tasks, while media continues downloading.
Cmd+C before `Посты готовы` also publishes the posts already collected, retaining
completed media and marking unfinished work. `posts_export_status=interrupted`
in `channel.csv` and the run-control record means the requested post history is
incomplete. The CLI prints the saved folder and count. Mode 2 can add comments
to those saved posts, but does not fetch missing posts: use mode 1 for a complete
period. Older datasets without this field are treated as complete histories.
If the disk is full or unwritable, saving progress can fail explicitly; earlier
datasets remain intact. A forced kill or power loss cannot save unpublished
in-memory posts before the first publication.
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

See [the UX safety review](docs/ux-safety-review.md) for the data-loss regression
and remaining risks.
