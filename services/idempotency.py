"""Idempotency-Key support for POST and PUT in the REST API.

The first request with a key reserves it (status IN_PROGRESS) with a conditional
write, runs once, and stores the response. A retry with the same key and the same
body gets the stored response again instead of writing twice. The same key with a
different body is rejected (422). Keys expire after 24 hours.

AWS mode stores the keys in the existing DynamoDB table ClimateIdempotency; its key
names are read from the table itself (describe_table), so nothing is hard-coded.
"""
import copy
import hashlib
import re
import threading
import time
from datetime import datetime, timezone

KEY_PATTERN = re.compile(r"[A-Za-z0-9_.:-]{8,100}")
TTL_SECONDS = 24 * 3600


class IdempotencyError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def fingerprint(method, path, body, extra=""):
    digest = hashlib.sha256()
    for part in (method.encode(), path.encode(), extra.encode(), body or b""):
        digest.update(len(part).to_bytes(8, "big"))
        digest.update(part)
    return digest.hexdigest()


def check_key(value):
    if not value:
        raise IdempotencyError(400, "idempotency_key_required",
                               "Send an Idempotency-Key header (8–100 letters, digits, '-', '_', '.', ':').")
    if not KEY_PATTERN.fullmatch(value):
        raise IdempotencyError(400, "invalid_idempotency_key",
                               "Idempotency-Key must contain 8–100 letters, digits, '-', '_', '.' or ':'.")
    return value


def decide(saved, print_):
    """What to do when the key already exists."""
    if saved["fingerprint"] != print_:
        raise IdempotencyError(422, "idempotency_key_reused",
                               "This Idempotency-Key was already used with a different request.")
    if saved["status"] != "COMPLETED":
        raise IdempotencyError(409, "request_in_progress",
                               "A request with this Idempotency-Key is still being processed. Retry shortly.")
    return int(saved["response_status"]), saved["response_body"]


class MemoryIdempotency:
    """Demo mode and fallback: keys live in this process only."""
    description = "memory (demo)"

    def __init__(self):
        self.items, self.lock = {}, threading.Lock()

    def begin(self, scope, print_):
        now = time.time()
        with self.lock:
            saved = self.items.get(scope)
            if saved and saved["expires"] > now:
                return decide(saved, print_)
            self.items[scope] = {"status": "IN_PROGRESS", "fingerprint": print_, "expires": now + TTL_SECONDS}
        return None

    def finish(self, scope, status, body):
        with self.lock:
            self.items[scope].update(status="COMPLETED", response_status=status, response_body=copy.deepcopy(body))

    def abandon(self, scope):
        with self.lock:
            self.items.pop(scope, None)


class DynamoIdempotency:
    def __init__(self, table_name="ClimateIdempotency", region="us-east-1", resource=None):
        if resource is None:
            import boto3
            resource = boto3.resource("dynamodb", region_name=region)
        self.table = resource.Table(table_name)
        client = self.table.meta.client
        info = client.describe_table(TableName=table_name)["Table"]
        types = {a["AttributeName"]: a["AttributeType"] for a in info["AttributeDefinitions"]}
        keys = {k["KeyType"]: k["AttributeName"] for k in info["KeySchema"]}
        self.pk = keys["HASH"]
        if types[self.pk] != "S":
            raise ValueError(f"{table_name} uses a numeric partition key; a text key is required.")
        self.sk = keys.get("RANGE")
        self.sk_value = None
        if self.sk:
            self.sk_value = "REQUEST" if types[self.sk] == "S" else 0
        try:
            ttl = client.describe_time_to_live(TableName=table_name)["TimeToLiveDescription"]
            self.ttl_attr = ttl.get("AttributeName") if ttl.get("TimeToLiveStatus") in ("ENABLED", "ENABLING") else None
        except Exception:
            self.ttl_attr = None
        self.ttl_attr = self.ttl_attr or "expires_at_epoch"
        self.description = f"DynamoDB {table_name} (key {self.pk}{', ' + self.sk if self.sk else ''}; TTL {self.ttl_attr})"
        self.names = {"#pk": self.pk}

    def key(self, scope):
        key = {self.pk: scope}
        if self.sk:
            key[self.sk] = self.sk_value
        return key

    @staticmethod
    def conditional_failed(exc):
        response = getattr(exc, "response", None) or {}
        return response.get("Error", {}).get("Code") == "ConditionalCheckFailedException"

    def begin(self, scope, print_):
        now = int(time.time())
        item = {**self.key(scope), "status": "IN_PROGRESS", "fingerprint": print_,
                "created_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                self.ttl_attr: now + TTL_SECONDS}
        try:
            # Reserve the key only if nobody has it (or the previous one already expired).
            self.table.put_item(Item=item, ConditionExpression="attribute_not_exists(#pk) OR #ttl < :now",
                                ExpressionAttributeNames={**self.names, "#ttl": self.ttl_attr},
                                ExpressionAttributeValues={":now": now})
            return None
        except Exception as exc:
            if not self.conditional_failed(exc):
                raise
        saved = self.table.get_item(Key=self.key(scope), ConsistentRead=True).get("Item")
        if not saved:
            raise IdempotencyError(409, "request_in_progress", "Retry the request with the same Idempotency-Key.")
        from contract import load_json
        if saved.get("status") == "COMPLETED":
            saved = dict(saved, response_body=load_json(saved["response_body"]))
        return decide(saved, print_)

    def finish(self, scope, status, body):
        from contract import dumps
        self.table.update_item(Key=self.key(scope),
                               UpdateExpression="SET #s = :s, response_status = :rs, response_body = :rb, completed_at_utc = :c",
                               ExpressionAttributeNames={"#s": "status"},
                               ExpressionAttributeValues={":s": "COMPLETED", ":rs": int(status), ":rb": dumps(body),
                                                          ":c": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")})

    def abandon(self, scope):
        self.table.delete_item(Key=self.key(scope))
