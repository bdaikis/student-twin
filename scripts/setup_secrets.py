"""Write the twin's secrets into Hermes's .env, checking each one works first.

Run it yourself in a terminal (the prompts hide what you type):
    python scripts/setup_secrets.py

Asks for the Anthropic API key and the Telegram bot token, checks each against its service, and
writes them to %LOCALAPPDATA%\\hermes\\.env together with your Telegram user ID, so the bot answers
only you. Other lines in the file are kept. Secrets are never printed.

Temporary: on Windows, Hermes can only read secrets from this plaintext file. A Windows Credential
Manager secret source will replace it before the first public release.
Standard library only.
"""
import getpass
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

ENV_PATH = Path(os.environ.get("HERMES_HOME") or Path(os.environ["LOCALAPPDATA"]) / "hermes") / ".env"
MODEL = "claude-haiku-5-5"


def check_anthropic(key: str) -> str:
    req = urllib.request.Request(
        f"https://api.anthropic.com/v1/models/{MODEL}",
        headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.load(resp).get("display_name", MODEL)
    except urllib.error.HTTPError as exc:
        sys.exit(f"Anthropic rejected the key or model (HTTP {exc.code}). Nothing was written.")


def check_telegram(token: str) -> str:
    try:
        with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/getMe", timeout=20) as resp:
            return "@" + json.load(resp)["result"]["username"]
    except urllib.error.HTTPError as exc:
        sys.exit(f"Telegram rejected the bot token (HTTP {exc.code}). Nothing was written.")


def write_env(values: dict) -> None:
    lines = ENV_PATH.read_text(encoding="utf-8").splitlines() if ENV_PATH.exists() else []
    kept = [ln for ln in lines if ln.split("=", 1)[0].strip() not in values]
    kept += [f"{k}={v}" for k, v in values.items()]
    ENV_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = ENV_PATH.with_suffix(".env.tmp")
    tmp.write_text("\n".join(kept) + "\n", encoding="utf-8")
    tmp.replace(ENV_PATH)


def main() -> None:
    user_id = input("Your numeric Telegram user ID (from @userinfobot): ").strip()
    if not re.fullmatch(r"\d{5,15}", user_id):
        sys.exit("That doesn't look like a Telegram user ID (digits only).")

    key = getpass.getpass("Anthropic API key (hidden): ").strip()
    if not key.startswith("sk-ant-"):
        sys.exit("Anthropic keys start with 'sk-ant-'. Nothing was written.")
    token = getpass.getpass("Telegram bot token (hidden): ").strip()
    if not re.fullmatch(r"\d+:[A-Za-z0-9_-]{30,}", token):
        sys.exit("Bot tokens look like '123456789:AA...'. Nothing was written.")

    model_name = check_anthropic(key)
    bot_name = check_telegram(token)

    write_env({
        "ANTHROPIC_API_KEY": key,
        "TELEGRAM_BOT_TOKEN": token,
        "TELEGRAM_ALLOWED_USERS": user_id,
    })
    print(f"Anthropic key works ({model_name}). Telegram bot works ({bot_name}).")
    print(f"Saved to {ENV_PATH}; the bot will answer only user {user_id}.")


if __name__ == "__main__":
    main()
