[Unit]
Description=woddi-harbor durable job worker
After=network-online.target woddi-harbor.service
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=__HARBOR_WORKDIR__
ExecStart=__HARBOR_WORKDIR__/.venv/bin/woddi-harbor job-worker
Restart=always
RestartSec=2
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=__HARBOR_WORKDIR__/config __HARBOR_WORKDIR__/data

[Install]
WantedBy=default.target
