"""Layer 2. No dotenv loading; demo never initializes an AWS client."""
import copy
import threading
from urllib.parse import urlsplit
from contract import dumps, load_json
from services.reading_service import ReadingService

FUNCTIONS = {op: f"Climate{name}ReadingV2" for op, name in
             (("create", "Create"), ("read", "Read"), ("update", "Update"), ("delete", "Delete"))}
METHODS = {"read": "GET", "create": "POST", "update": "PUT", "delete": "DELETE"}

class ApiError(Exception):
    pass

def unpack(status, body):
    if not isinstance(body, dict):
        raise ApiError("Invalid service response.")
    if status not in (200, 201):
        message = body.get("message")
        if status in (400, 404, 409, 413) and isinstance(message, str):
            raise ApiError(message[:300])
        raise ApiError({401: "Sign in again.", 403: "Access denied.",
                        429: "Too many requests. Try again later."}.get(status, "The service could not complete the request."))
    return body

class Client:
    def list_records(self, location="", cursor=""):
        params = {"list": "1"}
        if location: params["location_id"] = location
        if cursor: params["cursor"] = cursor
        result = self.call("read", params)
        rows = result.get("items")
        token = result.get("next_cursor", "")
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows) or not isinstance(token, str):
            raise ApiError("Invalid record list response.")
        return rows, token

    def get(self, keys): return self.call("read", keys)
    def create(self, data): return self.call("create", data)
    def update(self, data): return self.call("update", data)
    def delete(self, data): return self.call("delete", data)

class MemoryTable:
    def __init__(self): self.rows = {}
    @staticmethod
    def key(data): return data["location_id"], data["timestamp_utc"]
    def get_item(self, **options):
        row = self.rows.get(self.key(options["Key"]))
        return {"Item": copy.deepcopy(row)} if row else {}
    def put_item(self, **options):
        row = options["Item"]
        self.rows[self.key(row)] = copy.deepcopy(row)
    def delete_item(self, **options): self.rows.pop(self.key(options["Key"]), None)
    def page(self, rows, options):
        rows = sorted(rows, key=self.key)
        if "ExclusiveStartKey" in options:
            start = self.key(options["ExclusiveStartKey"])
            rows = [r for r in rows if self.key(r) > start]
        page = rows[:options["Limit"]]
        result = {"Items": copy.deepcopy(page)}
        if len(rows) > len(page):
            result["LastEvaluatedKey"] = {k: page[-1][k] for k in ("location_id", "timestamp_utc")}
        return result
    def scan(self, **options): return self.page(list(self.rows.values()), options)
    def query(self, **options):
        loc = options["ExpressionAttributeValues"][":loc"]
        return self.page([r for r in self.rows.values() if r["location_id"] == loc], options)

def sample():
    return dict(location_id="LOC-001", timestamp_utc="2026-10-10T00:00:00Z",
                station_id="ST-001", source_id="SRC-MANUAL", condition_id="COND-01",
                temperature_c=25.4, feels_like_c=26.5, humidity_pct=62.1,
                pressure_hpa=1015.2, wind_speed_kmh=12.5, wind_direction="NE",
                precipitation_mm=0, precipitation_period_minutes=60,
                cloud_cover_pct=15.5, visibility_km=10, uv_index=3.2, dew_point_c=17.6)

class DemoClient(Client):
    def __init__(self, seed=True):
        self.table = MemoryTable()
        self.service = ReadingService(self.table)
        self.lock = threading.RLock()
        if seed: self.create(sample())
    def call(self, operation, data):
        event = {"queryStringParameters": data} if operation == "read" else {"body": dumps(data)}
        # Serialize the full read/compare/write cycle in this single-process demo.
        with self.lock: result = self.service.handle(operation, event)
        return unpack(result["statusCode"], load_json(result["body"]))

class LambdaClient(Client):
    def __init__(self, region="us-east-1", sdk=None):
        self.region, self.sdk = region, sdk
    def call(self, operation, data):
        try:
            if self.sdk is None:
                import boto3
                from botocore.config import Config
                self.sdk = boto3.client("lambda", region_name=self.region,
                    config=Config(connect_timeout=5, read_timeout=20,
                                  retries={"total_max_attempts": 1}))
            event = {"queryStringParameters": data} if operation == "read" else {"body": dumps(data)}
            result = self.sdk.invoke(FunctionName=FUNCTIONS[operation], InvocationType="RequestResponse",
                                     Payload=dumps(event).encode("utf-8"))
            stream = result["Payload"]
            try: payload = stream.read()
            finally: stream.close()
            if result.get("FunctionError") or result.get("StatusCode") != 200:
                raise ApiError("Lambda execution failed. Check the function configuration.")
            response = load_json(payload)
            return unpack(int(response["statusCode"]), load_json(response["body"]))
        except ApiError: raise
        except Exception:
            raise ApiError("Cannot access AWS Lambda. Check your active lab session, region and InvokeFunction permission.") from None

class EndpointClient(Client):
    """Future JWT-protected HTTP API. No default URLs or legacy routes."""
    def __init__(self, urls, token, transport=None):
        if not token or any(c in token for c in "\r\n"):
            raise ValueError("A valid access token is required.")
        if set(urls) != set(METHODS): raise ValueError("Configure all four API URLs.")
        for url in urls.values():
            parsed = urlsplit(url)
            if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ValueError("Use HTTPS endpoint URLs without credentials, query strings or fragments.")
        self.urls, self.token, self.transport = urls, token, transport
    def call(self, operation, data):
        import requests
        send = self.transport or requests.request
        options = {"params": data} if operation == "read" else {"data": dumps(data).encode("utf-8")}
        try:
            response = send(METHODS[operation], self.urls[operation],
                headers={"Authorization": "Bearer " + self.token, "Content-Type": "application/json"},
                timeout=(5, 20), allow_redirects=False, **options)
            return unpack(response.status_code, load_json(response.text))
        except ApiError: raise
        except (requests.RequestException, ValueError, TypeError):
            raise ApiError("Cannot read the API response. Check connectivity and endpoint configuration.") from None
