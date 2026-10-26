#!/usr/bin/env sh
# Starts the room. On Termux it also keeps the phone from sleeping the server (termux-wake-lock).
cd "$(dirname "$0")" || exit 1
command -v termux-wake-lock >/dev/null 2>&1 && termux-wake-lock
exec python app.py
