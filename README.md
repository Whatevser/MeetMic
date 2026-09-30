# MeetMic — Google Meet PTT

System-wide hard push-to-talk for Google Meet on Windows.

- `Google_Meet_PTT.py` — tray app. Reads the PTT key with `GetAsyncKeyState` (no keyboard hook) and serves `127.0.0.1:8875`.
- `Google_Meet_PTT.user.js` — Tampermonkey script. Long-polls the app and drives Meet's mic button. Open its **Raw** link to install.
- Tray icons: `mic.ico` armed, `redmic.ico` mic live, `fadedmic.ico` not working (reason in the tooltip).

## Download

Every push to `main` builds the exe on GitHub Actions and puts it on the [latest release](https://github.com/Whatevser/MeetMic/releases/tag/latest): [Google_Meet_PTT.exe](https://github.com/Whatevser/MeetMic/releases/download/latest/Google_Meet_PTT.exe).

## Build locally

```powershell
pip install pyinstaller pystray pillow
pyinstaller --noconfirm --onefile --windowed --icon mic.ico --add-data "mic.ico;." --add-data "redmic.ico;." --add-data "fadedmic.ico;." Google_Meet_PTT.py
```

Output: `dist\Google_Meet_PTT.exe`.
