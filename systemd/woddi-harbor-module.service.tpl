[Unit]
Description=woddi-harbor module __HARBOR_MODULE_NAME__
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=__HARBOR_WORKDIR__
ExecStart=__HARBOR_WORKDIR__/.venv/bin/woddi-harbor worker __HARBOR_MODULE_ID__
Restart=on-failure
RestartSec=2

[Install]
WantedBy=default.target
