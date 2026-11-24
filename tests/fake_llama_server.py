"""Stands in for llama-server in tests: answers /health, /metrics and a
streamed /v1/chat/completions on a port."""
import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

METRICS = """# HELP llamacpp:prompt_tokens_total Number of prompt tokens processed
llamacpp:prompt_tokens_total 120
llamacpp:prompt_seconds_total 0.5
llamacpp:tokens_predicted_total 400
llamacpp:tokens_predicted_seconds_total 10
llamacpp:requests_processing 1
"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = {"/health": '{"status":"ok"}', "/metrics": METRICS}.get(self.path)
        self.send_response(200 if body else 404)
        self.end_headers()
        self.wfile.write((body or "").encode())

    def do_POST(self):
        if self.path != "/v1/chat/completions":
            self.send_response(404)
            self.end_headers()
            return
        received = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        # The answer says what arrived, so tests can check what Baton forwards.
        chunks = [{"choices": [{"delta": {"reasoning_content": "thinking"}}]},
                  {"choices": [{"delta": {"content": ",".join(sorted(received))}}]},
                  {"choices": [{"delta": {"content": "|" + received["messages"][-1]["content"]}}],
                   "timings": {"predicted_n": 3, "predicted_per_second": 42.0}}]
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for c in chunks:
            self.wfile.write(f"data: {json.dumps(c)}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    HTTPServer(("127.0.0.1", int(sys.argv[sys.argv.index("--port") + 1])), Handler).serve_forever()
