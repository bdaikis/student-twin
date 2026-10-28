# Student Twin

A personal assistant for university students with many courses, built on
[Hermes Agent](https://github.com/NousResearch/hermes-agent) and Claude Haiku 5.5.

It is meant to:

- watch your university's Moodle site, your email and (optionally) your WhatsApp class groups for
  new deadlines and changes, and remind you of them;
- read course-information documents to build the semester's assessment calendar;
- track progress on group projects in shared Google Docs;
- answer questions about your lecture notes, with sources;
- take commands by push-to-talk voice on your computer, or by chat through a Telegram bot.

It reads what you give it access to and **never sends messages or emails on your behalf**.
Irreversible actions (deleting, uninstalling, paying, submitting) need an on-screen confirmation.

## Status

Early development, used daily by its author on Windows 11. It is built in thin slices, each one
usable before the next starts:

1. Chat with the twin through Telegram and the desktop. *(built)*
2. Push-to-talk voice commands on the desktop. *(built)*
3. Deadlines from Moodle: questions, a twice-daily digest, change messages and reminders. *(built)*
4. Semester calendar from course-information documents.
5. WhatsApp changes, group-project tracking, lecture-note search.

## Install (Windows 11)

The twin is three parts: [Hermes Agent](https://github.com/NousResearch/hermes-agent) (the agent,
Telegram bot and scheduler), this repo's MCP servers and scripts (run by Hermes), and the widget
(an Electron app). Nothing needs administrator rights.

### 1. Prerequisites

- Python 3.12, Node.js 24, Git and Google Chrome.
- An Anthropic API key, a Telegram bot token from [@BotFather](https://t.me/BotFather) and your
  numeric Telegram user ID (from @userinfobot).
- Your university's Moodle site, and a calendar holding your classes that can give a private iCal
  address (Google Calendar does).

### 2. Hermes Agent, at the pinned commit

The twin is tested against Hermes commit `e3c3d27390`. Hermes's documented one-line installer always
fetches the latest version, so download `install.ps1` from that commit instead, read it, and run it
with `-Commit`: without it, even this installer checks out the newest `main`.

```powershell
$hermes = "e3c3d273901894ea98aee33f1fa20f3b460ba2dc"
Invoke-WebRequest "https://raw.githubusercontent.com/NousResearch/hermes-agent/$hermes/scripts/install.ps1" -OutFile install.ps1
powershell -ExecutionPolicy Bypass -File install.ps1 -Commit $hermes -NonInteractive -SkipBrowser -SkipComputerUse
hermes pm install --extra ddgs          # keyless web search
hermes pm install --extra stt-whisper   # speech recognition for the widget
```

Open a new terminal afterwards so `hermes` is on the PATH. Set `HERMES_HOME` before installing to put
Hermes on another drive.

### 3. This repo

```powershell
git clone https://github.com/bdaikis/student-twin.git
cd student-twin
py -3.12 -m venv $env:USERPROFILE\.venvs\student-twin
& $env:USERPROFILE\.venvs\student-twin\Scripts\python.exe -m pip install -r requirements.txt
cd widget; npm install; node node_modules/electron/install.js; cd ..
```

`npm install` leaves out Electron's own program; `install.js` downloads it
(`widget\node_modules\electron\dist\electron.exe`).

### 4. Your settings

Copy `config.example.json` to `%LOCALAPPDATA%\student-twin\config.json` and fill it in: your Moodle
site and courses, the semester's dates, your town (for the weather), the Chrome profile folder where
you are signed in to Moodle (`chrome://version` shows it as "Profile Path"), folders the twin must
never touch, where course files go, and the words speech recognition should expect (`voice.vocabulary`:
your course codes and the apps you name). Paths may use environment variables such as `%OneDrive%`.

### 5. Secrets

Each script asks with hidden prompts, checks the secret against its service, and never prints it.
Run them yourself in a terminal, with the venv's Python:

```powershell
python scripts\setup_secrets.py        # Anthropic key, Telegram bot, your user ID, the widget's API key
python scripts\save_calendar_link.py   # the private iCal address of your class calendar
python scripts\save_portal_token.py    # your Moodle mobile token (it prints a sign-in link first)
```

The calendar address and the Moodle token go to Windows Credential Manager. The Anthropic key and the
bot token go to Hermes's `.env` file for now, because Hermes on Windows reads secrets only from there.

### 6. Connect the parts

```powershell
python scripts\setup_machine.py              # shows what it will write
python scripts\setup_machine.py --apply      # Hermes's servers and settings, job launchers, jobs
hermes gateway restart
```

`setup_machine.py` writes everything that holds this machine's paths, so the same repo works in any
user folder. Add `--widget-autostart` to start the widget at sign-in (the same as *Start with
Windows* in the widget's settings).

On a second machine while the first still runs the twin, add `--jobs-paused --telegram off` before
starting Hermes there: the jobs are created switched off, and Hermes doesn't poll the bot (two
machines polling one bot split its messages between them, and both would send every reminder).

### 7. Start and check

- Start the widget: `cd widget; npm start`. Hold the numpad `.` key and speak, or click it to type.
  No numpad? Hover over the widget, click the gear beside the grip, and pick another key or a
  mouse side button under *Hold to talk*. Settings are kept per machine in
  `%LOCALAPPDATA%\student-twin\widget.json`.
- Send your bot a message on Telegram, then `/new` after any change to tools or settings.
- Ask "what's due this week?" in both places.

### Moving to another computer

`scripts\transfer_state.py export <folder>` copies the twin's settings (shared rules, memory, the
skills Hermes saved, `config.json`) without secrets; `import <folder>` puts them in place on the
other machine. Add `--data` only at the moment of switching, after pausing the old machine's jobs,
so that no reminder is sent twice. Only one machine may run the Telegram bot at a time: stop Hermes
on the old machine first, then run `setup_machine.py --apply --telegram on` on the new one, resume
its jobs and restart Hermes there.

## Privacy

- Your settings, tokens and data stay on your machine. Secrets are kept in the operating system's
  credential store, never in this repository or in plain files.
- Requests to the language model go to Anthropic's API, including any messages the twin reads.
- WhatsApp reading is optional, Windows-only and read-only. Automating WhatsApp in any form is not
  permitted by its terms of service; read the risk notes before enabling it.
