# Room: a web IRC client for your LAN (think KiwiIRC, with your themes)

Flask + SQLite, **no extra packages** (just `flask`). People on your network open this site in a browser and chat on a
real IRC network (made for **Libera.Chat**, works with other servers). The server keeps each person's IRC connection
open like a small bouncer, so you see the same chats on your phone and your laptop, and nothing is missed while a
page is closed.

## Run

    pip install -r requirements.txt
    python app.py            # or ./run.sh  (on Termux it also takes a wake-lock)

The console prints the URLs and, on the very first run, a **setup code**. Open the site, enter the code and create the
admin account. After that anyone can register at `/register`.

## How logging in works

1. **Local chat account** (`/login`, `/register`): this is only for *this* site. It holds your look (themes, CSS, JS
   gimmicks) and your IRC settings. Nobody on IRC ever sees it.
2. **IRC connection** (`/irc`, shown automatically the first time): server (default `irc.libera.chat`), nick, account
   name (only if different from the nick) and password. Press **Save & connect**.
3. From then on the app connects by itself and logs in with **SASL** (or, if a server has no SASL, sends `IDENTIFY` to
   NickServ). A rejected password is reported and **not retried** (retrying would only get you locked out); fix the
   password and press Save & connect again.

**No account on the network yet?** On `/irc` open *Sign up*: fill in nick and password, enter your email, press
*Sign up*. The app connects as a guest and sends `NickServ REGISTER` for you, then you paste the verification code the
network emails you and press *Verify*. Your email is used for that one command and is not stored. These commands are the
Libera/Atheme ones; on other networks use the status box and `/ns ...` commands.

## What you can do in the chat

* **Windows:** the server window (replies, MOTD, NickServ), channels, and private chats (tabs; `+` joins a channel, `×` closes).
* **Commands** (type `/help`): `/join #chan [key]`, `/part`, `/close`, `/msg nick text`, `/query nick`, `/me`, `/notice`,
  `/nick`, `/topic [text]`, `/whois`, `/away [text]`, `/mode`, `/kick`, `/invite`, `/ns ...` (NickServ, never logged),
  `/identify`, `/ignore nick`, `/unignore nick`, `/quote RAW`, `/quit`. Start a message with `//` to send a literal `/`.
* Tab completes a nick (`bo` + Tab), or type `@` and a few letters. Links are clickable; your own nick is highlighted.
* **Features follow the server.** The app asks the server what it supports (IRCv3 capabilities and `CLIENTTAGDENY`)
  and shows or hides buttons accordingly: typing indicators, replies, reactions, echo of your own messages, away status,
  account names. Libera allows few client tags, so on Libera you will usually see typing but not replies/reactions; on a
  server that allows them (for example Ergo) they appear by themselves. IRC has **no** edit, delete, picture upload or
  moderation reports, so those are gone.
* Mute / Block are personal: they only change what *you* see.

## Themes and gimmicks are only yours

CSS themes, the theme maker, custom CSS/JS, gimmicks, name colours and arrival effects all run **in your own browser**.
Other IRC users cannot see them and are never sent anything about them. Other people on your LAN with their own account
don't see them either. (The theme gallery and theme codes are for sharing looks between local accounts.)

## Privacy model

* Every local account has **its own IRC connection and its own logs**. Windows and messages are tagged with their owner
  and every query is filtered by it; another account cannot read them (this is tested).
* IRC passwords are **encrypted in the database** (`crypto_box.py`, standard library only). The key comes from
  `database/secret.key` (or `ROOM_SECRET`). Someone who copies only `chat.db` or a backup cannot read them; someone who also
  has the key, **or whoever runs this server, can connect as any user**. Be honest about that with the people you invite.
* Messages you send to services (`NickServ`, `ChanServ` ...) are never logged, including the copy the server echoes back.
* The IRC connection is made by this machine, so it only connects to ports 6660-6669, 6697, 7000, 7070, 9999 and refuses
  this machine itself (`127.0.0.1`). Change with `ROOM_IRC_PORTS` (`any` switches the check off) and `ROOM_IRC_ALLOW_LOCAL=1`.
* Old IRC lines are deleted after 90 days (`ROOM_IRC_LOG_DAYS`) and each window keeps at most 20,000 lines.

## Please turn on HTTPS

IRC passwords are typed into this site. On plain `http://` over Wi-Fi they travel unencrypted. Make a certificate and start with it:

    openssl req -x509 -newkey rsa:2048 -nodes -days 825 -keyout key.pem -out cert.pem -subj "/CN=room.local" \
            -addext "subjectAltName=IP:192.168.1.50"        # use your server's LAN IP
    ROOM_SSL_CERT=cert.pem ROOM_SSL_KEY=key.pem python app.py

Devices must trust the certificate (or you click through the warning once). Putting the app behind something that already
does HTTPS, such as Tailscale's `tailscale serve`, works too. HTTPS also lets phones *install* the site as an app.

## Things to know about IRC networks

* Networks limit connections per IP address and every person here connects from **your one public IP**. A handful of
  people is normally fine; check your network's rules if you have many users. Authenticating with SASL helps.
* On Libera you must register a nick to log in with SASL. Unregistered people can still connect as guests (tick off
  "log in with SASL").
* If your server restarts, accounts that were online reconnect by themselves (`ROOM_IRC_AUTOSTART=0` turns that off).
  **Log out of the site does not disconnect IRC**; use *Disconnect* on `/irc` or `/quit`.

## What changed from the old local chat

The local rooms, direct messages, pictures, edit/delete, reports, announcements, slow mode and temp mute are gone
(IRC does not have them). Old messages stay in `database/chat.db` but are no longer shown; nothing is deleted. The
minimum password length for new local passwords is now 8 (it protects an IRC login).

## What's where

    app.py            web pages and JSON API (login, /irc, /api/sync, /api/messages ...)
    irc_client.py     one IRC connection: TLS, CAP negotiation, SASL PLAIN, state tracking, reconnect, flood control
    irc_manager.py    one session per local account: events -> database, slash commands, feature detection
    irc_proto.py      parsing/formatting IRC lines, tags, splitting, casemapping
    crypto_box.py     encryption of stored IRC passwords
    db.py             SQLite schema and helpers          static/js/room.js   the chat page
    themes.py, static/themes, static/js/gimmicks         looks (client side only)

## Config worth knowing

`ROOM_PORT`, `ROOM_DB_DIR`, `ROOM_SECRET`, `ROOM_SSL_CERT` / `ROOM_SSL_KEY`, `ROOM_IRC_SERVER` (default server on the
setup page), `ROOM_IRC_AUTOSTART`, `ROOM_IRC_PORTS`, `ROOM_IRC_ALLOW_LOCAL`, `ROOM_IRC_LOG_DAYS`, `ROOM_HISTORY_LIMIT`,
`ROOM_BACKUPS`, `ROOM_BACKUP_DIR`. Message length, rate limits and the reactions palette are in `config.py`.
`MACRODROID_WEBHOOK_URL` is your old "someone opened the page" signal (empty disables it).

## Not done (yet)

SASL SCRAM-SHA-256 (PLAIN over TLS is used), SASL EXTERNAL / client certificates, `chathistory` from the server (your own
logs are kept here instead), message edit/delete extensions, WHOX/account lists, DCC, CTCP other than ACTION/VERSION/PING.

## Tests

    python -m unittest discover -s tests -v      # 63 tests, throw-away database, a fake IRC server on 127.0.0.1

`tests/fake_irc.py` is a tiny IRC server (CAP 302, SASL, channels, tags, NickServ) used to test the client and the whole
app without touching a real network. It has not been run against the real Libera.Chat: try it with a test account first.
