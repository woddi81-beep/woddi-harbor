[Unit]
Description=woddi-harbor scheduled backup

[Service]
Type=oneshot
WorkingDirectory=__HARBOR_WORKDIR__
ExecStart=__HARBOR_WORKDIR__/.venv/bin/woddi-harbor backup create --label scheduled
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=__HARBOR_WORKDIR__/config __HARBOR_WORKDIR__/data
