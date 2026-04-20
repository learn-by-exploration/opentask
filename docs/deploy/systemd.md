# Systemd Service (Linux)

Run OpenTask as a user-level systemd service that starts on boot and restarts on failure.

## Service File

Create `~/.config/systemd/user/taskpilot.service`:

```ini
[Unit]
Description=TaskPilot — Telegram AI Agent Bridge
After=network.target

[Service]
WorkingDirectory=/home/you/opentask
ExecStart=/home/you/opentask/.venv/bin/python -m app
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
```

Replace `/home/you/opentask` with your actual project path.

## Commands

```bash
# Reload after editing the service file
systemctl --user daemon-reload

# Enable and start
systemctl --user enable --now taskpilot

# Check status
systemctl --user status taskpilot

# View logs
journalctl --user -u taskpilot -f

# Restart
systemctl --user restart taskpilot

# Stop
systemctl --user stop taskpilot
```

## Enable Lingering

By default, user services only run while the user is logged in. Enable lingering to keep it running after logout:

```bash
sudo loginctl enable-linger $(whoami)
```
