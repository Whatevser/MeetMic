# MeetMic — Google Meet PPT

System-wide hard push-to-talk for Google Meet on Windows.

- `Google_Meet_PPT.py` — tray app. Reads the PTT key with `GetAsyncKeyState` (no keyboard hook) and serves `127.0.0.1:8875`.
- `Google_Meet_PPT.user.js` — Tampermonkey script. Long-polls the app and drives Meet's mic button. Open its **Raw** link to install.
- Tray icons: `mic.ico` armed, `redmic.ico` mic live, `fadedmic.ico` not working (reason in the tooltip).

## Build

```powershell
pip install pyinstaller pystray pillow
pyinstaller --noconfirm --onefile --windowed --icon mic.ico --add-data "mic.ico;." --add-data "redmic.ico;." --add-data "fadedmic.ico;." Google_Meet_PPT.py
```

Output: `dist\Google_Meet_PPT.exe`.
