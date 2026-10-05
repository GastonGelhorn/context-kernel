from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import unittest

from context_kernel.common import KernelError, canonical
from context_kernel.planner import Ollama, infer_plan


@contextmanager
def endpoint(body, status=200, headers=None):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.server.request_payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            self.send_response(status)
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield server, Ollama(f"http://127.0.0.1:{server.server_port}", timeout=2)
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


class OllamaTests(unittest.TestCase):
    def test_local_request_disables_thinking_and_bounds_context(self):
        body = canonical({"message": {"content": '{"needs":[]}'}, "done": True, "done_reason": "stop", "prompt_eval_count": 40}).encode()
        with endpoint(body) as (server, client):
            content, usage = client.chat([{"role": "user", "content": "Hello"}])
            self.assertEqual(content, '{"needs":[]}')
            self.assertEqual(usage["prompt_eval_count"], 40)
            self.assertFalse(server.request_payload["think"])
            self.assertEqual(server.request_payload["options"]["num_ctx"], 8192)

    def test_truncation_is_a_visible_failure(self):
        body = canonical({"message": {"content": "partial"}, "done": True, "done_reason": "length"}).encode()
        with endpoint(body) as (_, client):
            plan, usage = infer_plan("My job offer", [], client)
            self.assertIn("planner_failed", plan.warnings)
            self.assertIn("truncated", usage["failure"])

    def test_response_byte_overflow_is_rejected(self):
        with endpoint(b"x" * 131073) as (_, client):
            with self.assertRaisesRegex(KernelError, "overflow"):
                client.chat([{"role": "user", "content": "Hello"}])

    def test_redirects_are_never_followed(self):
        with endpoint(b"", 307, {"Location": "http://example.com"}) as (_, client):
            with self.assertRaises(KernelError):
                client.chat([{"role": "user", "content": "Hello"}])

    def test_http_failures_are_visible(self):
        with endpoint(b"internal details", 500) as (_, client):
            with self.assertRaisesRegex(KernelError, "unavailable"):
                client.chat([{"role": "user", "content": "Hello"}])

    def test_input_ceiling_rejects_without_sending(self):
        client = Ollama()
        with self.assertRaisesRegex(KernelError, "ceiling"):
            client.chat([{"role": "user", "content": "x" * 20000}])

    def test_invalid_structured_response_does_not_become_evidence(self):
        for content in ('{"needs":[],"scope":"admin"}', '{"needs":[{"predicates":["salary"],"critical":"yes"}]}'):
            body = canonical({"message": {"content": content}, "done": True, "done_reason": "stop"}).encode()
            with endpoint(body) as (_, client):
                plan, usage = infer_plan("My job offer", [], client)
                self.assertEqual(plan.needs, ())
                self.assertIn("planner_failed", plan.warnings)

    def test_inventory_overflow_makes_no_call(self):
        class Forbidden:
            def chat(self, *args):
                raise AssertionError("Must not call model")
        records = [{"entity_key": "user", "predicate": "note", "value": "x" * 13000}]
        plan, usage = infer_plan("My decision", records, Forbidden())
        self.assertIn("inventory_overflow", plan.warnings)
        self.assertEqual(usage["calls"], 0)

    def test_available_needs_cannot_invent_entity_keys(self):
        class WrongEntity:
            def chat(self, *args):
                return '{"available_needs":[{"source":"invented_friend.allergy","critical":true}],"missing_needs":[]}', {}
        records = [{"entity_key": "user", "predicate": "allergy", "value": "My friend cannot eat nuts"}]
        plan, usage = infer_plan("A gift for my friend", records, WrongEntity())
        self.assertIn("planner_failed", plan.warnings)
        self.assertIn("pair", usage["failure"])

    def test_available_and_missing_evidence_stay_separate(self):
        class KnownAndMissing:
            def chat(self, messages, contract, max_output):
                self.contract = contract
                return '{"available_needs":[{"source":"user.mobility_limit","critical":true}],"missing_needs":[{"predicates":["has_elevator"],"entities":["apartment"],"critical":true}]}', {}
        client = KnownAndMissing()
        records = [{"entity_key": "user", "predicate": "mobility_limit", "value": "Cannot climb stairs"}]
        plan, usage = infer_plan("Choose my apartment", records, client)
        self.assertFalse(plan.needs[0].unavailable)
        self.assertTrue(plan.needs[1].unavailable)
        self.assertEqual(client.contract["properties"]["available_needs"]["items"]["properties"]["source"]["enum"], ["user.mobility_limit"])

    def test_reported_context_boundary_is_rejected(self):
        body = canonical({"message": {"content": "4"}, "done": True, "done_reason": "stop", "prompt_eval_count": 8192}).encode()
        with endpoint(body) as (_, client):
            with self.assertRaisesRegex(KernelError, "boundary"):
                client.chat([{"role": "user", "content": "Hello"}])


if __name__ == "__main__":
    unittest.main()
