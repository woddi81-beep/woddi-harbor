[Unit]
Description=woddi-harbor local TLS reverse proxy
After=network-online.target woddi-harbor.service
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/bin/caddy run --config __HARBOR_WORKDIR__/deploy/Caddyfile.local --adapter caddyfile
Restart=on-failure
RestartSec=2
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=%h/.local/share/caddy %h/.config/caddy

[Install]
WantedBy=default.target
