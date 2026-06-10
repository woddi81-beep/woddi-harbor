limit_req_zone $binary_remote_addr zone=harbor_login:10m rate=10r/m;

upstream woddi_harbor {
    server 127.0.0.1:9680;
    keepalive 32;
}

server {
    listen 80;
    server_name __HARBOR_HOSTNAME__;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl http2;
    server_name __HARBOR_HOSTNAME__;

    ssl_certificate /etc/letsencrypt/live/__HARBOR_HOSTNAME__/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/__HARBOR_HOSTNAME__/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;

    client_max_body_size 2m;
    proxy_read_timeout 300s;
    proxy_send_timeout 300s;

    location / {
        proxy_pass http://woddi_harbor;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_buffering off;
    }
}
