[Unit]
Description=woddi-harbor module __HARBOR_MODULE_NAME__
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=__HARBOR_WORKDIR__
EnvironmentFile=__HARBOR_WORKER_ENV_FILE__
ExecStart=__HARBOR_MODULE_COMMAND__
Restart=on-failure
RestartSec=2
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=__HARBOR_WORKDIR__/data

[Install]
WantedBy=default.target
