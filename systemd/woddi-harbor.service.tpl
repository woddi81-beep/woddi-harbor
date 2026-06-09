[Unit]
Description=woddi-harbor AI Control Plane
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=__HARBOR_WORKDIR__
ExecStart=__HARBOR_WORKDIR__/.venv/bin/woddi-harbor serve --host __HARBOR_HOST__ --port __HARBOR_PORT__
Restart=on-failure
RestartSec=2
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=__HARBOR_WORKDIR__/config __HARBOR_WORKDIR__/data

[Install]
WantedBy=default.target
