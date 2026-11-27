runtime           = "ecs"
name              = "svc-sales-api"
port              = 8080
health_check_path = "/health"

# TABLE_SALES and TOPIC_SALES_CREATED come from deploy/data.hcl, REGION_STATE_PARAMETER from the
# platform. Only the sales site may call the API from a browser; {zone} is the platform's zone.
env = {
  ALLOWED_ORIGINS = "https://vendas.{zone}"
}

slo = {
  availability   = 99.5
  latency_p95_ms = 300
}
