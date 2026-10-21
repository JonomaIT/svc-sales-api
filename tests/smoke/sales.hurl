# Smoke test after each deploy (Contrato da app): the public path end to end.
#   hurl --test --variable host=https://sales.joshuel.com.br tests/smoke/sales.hurl

GET {{host}}/health
HTTP 200

# A sale is accepted and stored as PENDING.
POST {{host}}/sales
{"product": "smoke-test", "amount": 1.5}
HTTP 201
[Captures]
id: jsonpath "$.id"
[Asserts]
jsonpath "$.status" == "PENDING"
jsonpath "$.amount" == "1.50"

# It can be read back.
GET {{host}}/sales/{{id}}
HTTP 200
[Asserts]
jsonpath "$.product" == "smoke-test"

# Bad input is refused with a reason.
POST {{host}}/sales
{"product": "smoke-test"}
HTTP 400
[Asserts]
jsonpath "$.error" contains "amount"

# Clean up.
DELETE {{host}}/sales/{{id}}
HTTP 204
