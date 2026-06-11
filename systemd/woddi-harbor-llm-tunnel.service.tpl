[Unit]
Description=woddi-harbor secure tunnel to remote LLM
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/bin/ssh -F /dev/null -NT \
  -o BatchMode=yes \
  -o ExitOnForwardFailure=yes \
  -o ServerAliveInterval=15 \
  -o ServerAliveCountMax=3 \
  -L 127.0.0.1:__HARBOR_LLM_LOCAL_PORT__:127.0.0.1:__HARBOR_LLM_REMOTE_PORT__ \
  __HARBOR_LLM_SSH_TARGET__
Restart=always
RestartSec=3
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=default.target
