# What svc-sales-api keeps (ADR-011). The data runner builds it in every region of the app, apart
# from the app itself, and the app receives TABLE_SALES and TOPIC_SALES_CREATED.

tables = {
  sales = { hash_key = "id" } # global table: written in any region, replicated to the others
}

# Every sale created is published here; svc-sales-worker's queues subscribe to it in every region.
topics = ["sales-created"]
