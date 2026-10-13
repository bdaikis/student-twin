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

Early development. Nothing here is usable yet. It is built in thin slices, each one usable
before the next starts:

1. Chat with the twin through Telegram and the desktop.
2. Push-to-talk voice commands on the desktop.
3. Deadlines from Moodle.
4. Semester calendar from course-information documents.
5. WhatsApp changes, group-project tracking, lecture-note search.

## Privacy

- Your settings, tokens and data stay on your machine. Secrets are kept in the operating system's
  credential store, never in this repository or in plain files.
- Requests to the language model go to Anthropic's API, including any messages the twin reads.
- WhatsApp reading is optional, Windows-only and read-only. Automating WhatsApp in any form is not
  permitted by its terms of service; read the risk notes before enabling it.
