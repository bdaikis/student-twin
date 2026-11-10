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

### Snip and ask

Hold **Ctrl** on its own and move the pointer around what you want: a line follows the pointer from
where Ctrl went down. Releasing Ctrl closes the shape with a straight line back to the start (shown
dashed while you draw) and sends what is inside it to the twin; outside the shape comes out white.
The trace stays on the screen where it started. A **Win+Shift+S** snip goes to the twin
the same way once it reaches the clipboard. The twin solves a maths or physics problem straight
away; for anything else it says what it sees and asks what you want done with it. Answer by voice
or by typing.

Ctrl used for a shortcut doesn't snip: pressing another key, clicking or scrolling while Ctrl is
down cancels it, and so does a movement under 8 px or a shape under 12 px across. Nothing is
swallowed, so Ctrl+C and Ctrl+click work as before. It stays off while a full-screen app is in
front or the pointer is hidden (games hold Ctrl to crouch or sprint). The Ctrl snip is put on the
clipboard, as the Snipping Tool does. Turn both off with *Snips go to the twin* in the settings.

### Ask about a screenshot

With *Snips go to the twin* off, press **Win+Shift+S**, select the problem, diagram or text, then
ask the widget by voice or typing:
"Explain my latest screenshot" or "Solve the question I just screenshotted." No paste or file save
is needed. The `latest_screenshot` desktop tool reads the current clipboard image and gives that
image to the model; follow-up questions use the same image in the conversation. Take another snip
and say "Solve my new screenshot" to change it.

Only the image currently on the clipboard is available. If you copy text or a file afterwards,
snip again. The twin never replaces it with a new capture of your screen and asks for a clearer
snip when it cannot read the question. Image contents are sent to the configured model provider
when the tool is called and may remain in Hermes's conversation history; the tool creates no
separate image archive and leaves the clipboard untouched. It reads this computer's clipboard:
from Telegram, that is the computer running the desktop server. Laptop-to-desktop routing is
still part of the desktop host plan.

After updating from an earlier version, restart Hermes so it reloads the desktop server and
restart the widget for its new screenshot guidance. Send `/new` on Telegram so both channels see the new tool.

### Ask about the window you're in, and see where to click

Each widget request carries the title of the window you were using when you asked, so "what does
this mean?" or "where's the export button here?" needs no window name. A messaging or email
window's title is left out. When you ask where something is or what to click, the twin looks at
that window and rings the spot on your screen for a few seconds, with a short label, and says what
to click. The ring never clicks anything, and clicks pass straight through it. It goes when you
click or scroll, switch to another window, move the window, ask again, or after 12 seconds. Only
requests from the widget show it, not Telegram. Restart Hermes and the widget after updating.

### Moving to another computer

`scripts\transfer_state.py export <folder>` copies the twin's settings (shared rules, memory, the
skills Hermes saved, `config.json`) without secrets; `import <folder>` puts them in place on the
other machine. Add `--data` only at the moment of switching, after pausing the old machine's jobs,
so that no reminder is sent twice. Only one machine may run the Telegram bot at a time: stop Hermes
on the old machine first, then run `setup_machine.py --apply --telegram on` on the new one, resume
its jobs and restart Hermes there.

### The widget on a second computer

One computer runs Hermes; the widget can run on another and talk to it over
[Tailscale](https://tailscale.com), a private network between your own devices, so nothing is
opened to the internet.

1. Install Tailscale on both and sign in with the same personal account.
2. On the computer running Hermes, which keeps listening on `127.0.0.1` only, forward its port
   to your tailnet: `tailscale serve --bg --tcp 8642 tcp://127.0.0.1:8642`.
3. On the other computer, put that computer's `API_SERVER_KEY` in its Hermes `.env` (the widget
   reads its key from there), point the widget at it with
   `setup_machine.py --apply --hermes-url http://<computer name>:8642`, and restart the widget.
   `--hermes-url local` points it back.

Until that Hermes can use the second computer's own tools, its app, window, file, clipboard and
screen tools act on the computer running Hermes, and the widget tells the twin so; snips stay
off there. A remote widget keeps its own conversation rather than continuing Telegram's.

## Privacy

- Your settings, tokens and data stay on your machine. Secrets are kept in the operating system's
  credential store, never in this repository or in plain files.
- Requests to the language model go to Anthropic's API, including any messages the twin reads.
- WhatsApp reading is optional, Windows-only and read-only. Automating WhatsApp in any form is not
  permitted by its terms of service; read the risk notes before enabling it.
