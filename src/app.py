"""svc-sales-api: takes sales and hands them over to processing (JonomaIT lab).

  POST   /sales        {"product": "...", "amount": 10.5} -> 201, the sale with status PENDING,
                       published on the sales-created topic of this region (it reaches the queue of
                       every region, ADR-012)
  GET    /sales/<id>   -> 200, the sale (PROCESSED once a worker of the ACTIVE region handled it)
  DELETE /sales/<id>   -> 204
  GET    /info         -> this task, its region and the region state (ACTIVE or PASSIVE)
  GET    /health       -> 200 "ok", the ALB health check (never calls AWS)

Everything it needs arrives as environment variables: TABLE_SALES and TOPIC_SALES_CREATED from its
deploy/data.hcl (data runner, ADR-011), REGION_STATE_PARAMETER from the platform, AWS_REGION from
ECS. Its permissions come from the data-access policy: no name, ARN or credential in the code.
"""

import decimal
import json
import os
import re
import signal
import socket
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("PORT", "8080"))
REGION = os.environ.get("AWS_REGION", "unknown")
SALE_PATH = re.compile(r"^/sales/([0-9a-f-]{36})$")


def log(**fields):
    """One JSON line on stdout, read by CloudWatch Logs."""
    print(json.dumps({"time": now_iso(), "region": REGION, **fields}, default=str), flush=True)


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def to_json(item):
    """DynamoDB numbers come back as Decimal; money goes out as text ("12.50"), never as float."""
    return {k: f"{v:.2f}" if isinstance(v, decimal.Decimal) else v for k, v in item.items()}


class BadRequest(Exception):
    pass


def parse_sale(body):
    """Validates a new sale. Money never goes through float: JSON numbers are read as Decimal and
    rounded half up to cents (DynamoDB stores Decimal)."""
    try:
        data = json.loads(body or b"{}", parse_float=decimal.Decimal)
    except ValueError:
        raise BadRequest("body must be JSON") from None
    if not isinstance(data, dict):
        raise BadRequest("body must be a JSON object")
    product, amount = data.get("product"), data.get("amount")
    if not isinstance(product, str) or not product.strip() or len(product.strip()) > 100:
        raise BadRequest("product: text, 1 to 100 characters")
    if isinstance(amount, bool) or not isinstance(amount, (int, decimal.Decimal)) or not 0 < amount <= 1_000_000:
        raise BadRequest("amount: number greater than 0 and at most 1000000")
    return product.strip(), decimal.Decimal(amount).quantize(decimal.Decimal("0.01"), rounding=decimal.ROUND_HALF_UP)


class RegionState:
    """ACTIVE or PASSIVE (ADR-012), read from the platform's parameter, cached for a few seconds."""

    def __init__(self, ssm, name, ttl=30):
        self.ssm, self.name, self.ttl = ssm, name, ttl
        self.value, self.read_at = "UNKNOWN", None

    def get(self):
        if not self.name:
            return "UNKNOWN"
        if self.read_at is None or time.monotonic() - self.read_at > self.ttl:
            try:
                self.value = self.ssm.get_parameter(Name=self.name)["Parameter"]["Value"]
                self.read_at = time.monotonic()
            except Exception as err:  # keep the last known value; the API works either way
                log(msg="region state unavailable", error=str(err))
        return self.value


class Sales:
    def __init__(self, table, sns, topic_arn):
        self.table, self.sns, self.topic_arn = table, sns, topic_arn

    def create(self, product, amount):
        sale = {
            "id": str(uuid.uuid4()),
            "product": product,
            "amount": amount,
            "status": "PENDING",
            "created_at": now_iso(),
            "created_region": REGION,
        }
        self.table.put_item(Item=sale, ConditionExpression="attribute_not_exists(id)")
        try:
            self.sns.publish(
                TopicArn=self.topic_arn,
                Message=json.dumps(to_json(sale)),
                MessageAttributes={"event": {"DataType": "String", "StringValue": "sale.created"}},
            )
        except Exception:
            # Without the event nobody processes the sale: undo the write and let the client retry.
            # (A transactional outbox, e.g. DynamoDB Streams to the topic, would make this atomic.)
            self.table.delete_item(Key={"id": sale["id"]})
            raise
        return sale

    def get(self, sale_id):
        return self.table.get_item(Key={"id": sale_id}).get("Item")

    def delete(self, sale_id):
        old = self.table.delete_item(Key={"id": sale_id}, ReturnValues="ALL_OLD")
        return "Attributes" in old


# Set by main() (or by the tests).
SALES = None
STATE = None


class Handler(BaseHTTPRequestHandler):
    server_version = "svc-sales-api"
    sys_version = ""

    def _send(self, status, body=None):
        data = b"" if body is None else (body.encode() if isinstance(body, str) else json.dumps(body, indent=2).encode())
        self.send_response(status)
        if body is not None:
            self.send_header("Content-Type", "text/plain" if isinstance(body, str) else "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Region", REGION)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802 (http.server naming)
        if self.path == "/health":
            return self._send(200, "ok\n")
        if self.path == "/info":
            return self._send(200, {"service": "svc-sales-api", "region": REGION, "region_state": STATE.get(), "task": socket.gethostname()})
        match = SALE_PATH.match(self.path)
        if not match:
            return self._send(404, {"error": "not found"})
        sale = SALES.get(match.group(1))
        self._send(200, to_json(sale)) if sale else self._send(404, {"error": "sale not found"})

    def do_POST(self):  # noqa: N802
        if self.path != "/sales":
            return self._send(404, {"error": "not found"})
        try:
            length = min(int(self.headers.get("Content-Length") or 0), 10_000)
            product, amount = parse_sale(self.rfile.read(length))
        except BadRequest as err:
            return self._send(400, {"error": str(err)})
        try:
            sale = SALES.create(product, amount)
        except Exception as err:
            log(msg="sale not created", error=str(err))
            return self._send(503, {"error": "sale not created, try again"})
        log(msg="sale created", id=sale["id"], region_state=STATE.get())
        self._send(201, to_json(sale))

    def do_DELETE(self):  # noqa: N802
        match = SALE_PATH.match(self.path)
        if not match:
            return self._send(404, {"error": "not found"})
        self._send(204) if SALES.delete(match.group(1)) else self._send(404, {"error": "sale not found"})

    def log_message(self, fmt, *args):
        # One line per request; health checks stay quiet.
        if self.path != "/health":
            log(method=self.command, path=self.path, status=args[1] if len(args) > 1 else "")


def main():
    global SALES, STATE
    import boto3
    from botocore.config import Config

    # Short timeouts and standard retries: a slow dependency fails fast instead of piling up threads.
    config = Config(connect_timeout=2, read_timeout=5, retries={"mode": "standard", "max_attempts": 3})
    SALES = Sales(
        boto3.resource("dynamodb", config=config).Table(os.environ["TABLE_SALES"]),
        boto3.client("sns", config=config),
        os.environ["TOPIC_SALES_CREATED"],
    )
    STATE = RegionState(boto3.client("ssm", config=config), os.environ.get("REGION_STATE_PARAMETER"))

    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    # ECS stops a task with SIGTERM: finish the requests in flight, then exit.
    signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=server.shutdown).start())
    log(msg="listening", port=PORT, region_state=STATE.get())
    server.serve_forever()


if __name__ == "__main__":
    main()
