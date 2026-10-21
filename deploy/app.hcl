runtime           = "ecs"
name              = "svc-sales-api"
port              = 8080
health_check_path = "/health"

# Nothing else to set: TABLE_SALES and TOPIC_SALES_CREATED come from deploy/data.hcl,
# REGION_STATE_PARAMETER from the platform.
env = {}

slo = {
  disponibilidade = 99.5
  latencia_p95_ms = 300
}
