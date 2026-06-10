scrape_configs:
  - job_name: woddi-harbor
    scrape_interval: 15s
    metrics_path: /metrics
    basic_auth:
      username: __HARBOR_METRICS_USER__
      password: __HARBOR_METRICS_PASSWORD__
    static_configs:
      - targets: ["127.0.0.1:9680"]
