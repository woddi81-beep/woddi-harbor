global:
  scrape_interval: 15s
  evaluation_interval: 15s

scrape_configs:
  - job_name: woddi-harbor
    metrics_path: /metrics
    authorization:
      type: Bearer
      credentials: __HARBOR_METRICS_TOKEN__
    static_configs:
      - targets: ["127.0.0.1:9680"]
