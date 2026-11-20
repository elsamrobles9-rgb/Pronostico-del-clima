import io
import tempfile
import re
import unittest
from urllib.parse import urlencode
from unittest.mock import Mock
from werkzeug.datastructures import MultiDict
from app import create_app
from contract import dumps, load_json
from services.endpoints import DemoClient, LambdaClient, EndpointClient, ApiError, sample

class PortalTests(unittest.TestCase):
    def setUp(self):
        self.backend = DemoClient()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.app = create_app(self.backend, auth_path=self.temp.name+"/auth.sqlite3")
        self.app.testing = True
        self.web = self.app.test_client()
        store=self.app.extensions['access_store']
        store.add_user('tester','test-password-123','ADMIN')
        with self.web.session_transaction() as session:
            session['sid']=store.login('tester','test-password-123')
        self.keys = {k: sample()[k] for k in ("location_id", "timestamp_utc")}

    def form(self, path):
        response = self.web.get(path)
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        result = {"csrf": re.search(r'name="csrf" value="([^"]+)"', html)[1]}
        token = re.search(r'name="snapshot" value="([^"]+)"', html)
        if token: result["snapshot"] = token[1]
        return result

    def path(self, action): return "/" + action + "?" + urlencode(self.keys)

    def test_list_and_english_fields(self):
        html = self.web.get("/").get_data(as_text=True)
        for text in ("Add record", "UV index", "Dew point", "LOC-001", "Edit record", "Delete record"):
            self.assertIn(text, html)
        self.assertNotIn("id_ubicacion", html)

    def test_create_normalizes_minute_and_generates_id(self):
        fields = {**sample(), **self.form("/add"), "timestamp_utc": "2026-10-10T08:05:48-06:00"}
        self.assertEqual(self.web.post("/add", data=fields).status_code, 302)
        row = self.backend.get({**self.keys, "timestamp_utc": "2026-10-10T14:05:00Z"})
        self.assertEqual(len(row["record_id"]), 36)
        self.assertEqual(row["version"], 1)

    def test_missing_uv_and_invalid_humidity_do_not_write(self):
        for changes in ({"uv_index": ""}, {"humidity_pct": "101"}, {"dew_point_c": "40"}):
            with self.subTest(changes=changes):
                data = {**sample(), **self.form("/add"), "location_id": "NEW", **changes}
                self.web.post("/add", data=data)
                self.assertEqual(len(self.backend.table.rows), 1)

    def test_csrf_and_duplicate_fields(self):
        self.assertEqual(self.web.post("/add", data=sample()).status_code, 400)
        fields = {**sample(), **self.form("/add")}
        duplicated = MultiDict(fields)
        duplicated.add("uv_index", "9")
        self.assertEqual(self.web.post("/add", data=duplicated).status_code, 400)

    def test_unknown_field_and_xss_are_rejected_or_escaped(self):
        fields = {**sample(), **self.form("/add"), "admin": "true", "location_id": '<script>alert(1)</script>'}
        html = self.web.post("/add", data=fields).get_data(as_text=True)
        self.assertIn("Unknown form fields", html)
        self.assertNotIn('<script>alert(1)</script>', html)
        self.assertEqual(len(self.backend.table.rows), 1)

    def test_edit_uses_original_version_and_preserves_uuid(self):
        original = self.backend.get(self.keys)
        fields = {**sample(), **self.form(self.path("edit")), "uv_index": "4.1"}
        self.assertEqual(self.web.post(self.path("edit"), data=fields).status_code, 302)
        updated = self.backend.get(self.keys)
        self.assertEqual(updated["version"], 2)
        self.assertEqual(updated["record_id"], original["record_id"])

    def test_stale_edit_cannot_overwrite_newer_change(self):
        fields = {**sample(), **self.form(self.path("edit")), "uv_index": "7"}
        self.backend.update({**self.keys, "expected_version": 1, "uv_index": 5})
        html = self.web.post(self.path("edit"), data=fields).get_data(as_text=True)
        self.assertIn("version conflict", html)
        self.assertEqual(self.backend.get(self.keys)["uv_index"], 5)

    def test_signed_snapshot_cannot_be_modified(self):
        fields = {**sample(), **self.form(self.path("edit")), "uv_index": "7"}
        fields["snapshot"] += "changed"
        html = self.web.post(self.path("edit"), data=fields).get_data(as_text=True)
        self.assertIn("expired or changed", html)
        self.assertEqual(self.backend.get(self.keys)["version"], 1)

    def test_keys_cannot_be_changed(self):
        fields = {**sample(), **self.form(self.path("edit")), "location_id": "OTHER"}
        html = self.web.post(self.path("edit"), data=fields).get_data(as_text=True)
        self.assertIn("keys cannot be changed", html)
        self.assertEqual(len(self.backend.table.rows), 1)

    def test_delete_requires_confirmation_and_checks_version(self):
        fields = self.form(self.path("delete"))
        self.assertEqual(len(self.backend.table.rows), 1)
        self.backend.update({**self.keys, "expected_version": 1, "uv_index": 5})
        html = self.web.post(self.path("delete"), data=fields).get_data(as_text=True)
        self.assertIn("version conflict", html)
        self.assertEqual(len(self.backend.table.rows), 1)
        fields = self.form(self.path("delete"))
        self.assertEqual(self.web.post(self.path("delete"), data=fields).status_code, 302)
        self.assertEqual(len(self.backend.table.rows), 0)

    def test_demo_pagination(self):
        for i in range(30): self.backend.create({**sample(), "location_id": f"PAGE-{i:02}"})
        rows, cursor = self.backend.list_records()
        rest, end = self.backend.list_records(cursor=cursor)
        self.assertEqual((len(rows), len(rest), end), (25, 6, ""))
        self.assertEqual(len({r["location_id"] for r in rows + rest}), 31)

class TransportTests(unittest.TestCase):
    def test_lambda_success_and_decimal_payload(self):
        sdk = Mock()
        stream = io.BytesIO(dumps({"statusCode": 200, "body": dumps({"message": "Record updated."})}).encode())
        sdk.invoke.return_value = {"StatusCode": 200, "Payload": stream}
        client = LambdaClient(sdk=sdk)
        client.update({"uv_index": load_json("4.1")})
        args = sdk.invoke.call_args.kwargs
        self.assertEqual(args["FunctionName"], "ClimateUpdateReadingV2")
        self.assertEqual(load_json(load_json(args["Payload"])["body"])["uv_index"], load_json("4.1"))
        self.assertTrue(stream.closed)

    def test_invoke_200_does_not_hide_lambda_409(self):
        sdk = Mock()
        sdk.invoke.return_value = {"StatusCode": 200, "Payload": io.BytesIO(dumps({"statusCode": 409, "body": dumps({"message": "Version conflict."})}).encode())}
        with self.assertRaisesRegex(ApiError, "Version conflict"):
            LambdaClient(sdk=sdk).update({})

    def test_function_error_is_not_success(self):
        sdk = Mock()
        sdk.invoke.return_value = {"StatusCode": 200, "FunctionError": "Unhandled", "Payload": io.BytesIO(b'{}')}
        with self.assertRaisesRegex(ApiError, "execution failed"):
            LambdaClient(sdk=sdk).get({})

    def test_api_sends_authentication_and_disables_redirects(self):
        send = Mock(return_value=Mock(status_code=200, text='{"items":[],"next_cursor":""}'))
        urls = {op: "https://example.invalid/" + op for op in ("read", "create", "update", "delete")}
        client = EndpointClient(urls, "test-token", transport=send)
        self.assertEqual(client.list_records(), ([], ""))
        self.assertFalse(send.call_args.kwargs["allow_redirects"])
        self.assertEqual(send.call_args.kwargs["headers"]["Authorization"], "Bearer test-token")
        with self.assertRaises(ValueError): EndpointClient(urls, "")

if __name__ == "__main__": unittest.main()
