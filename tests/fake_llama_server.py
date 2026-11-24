"""Stands in for llama-server in tests: answers /health and /metrics on a port."""
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

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    HTTPServer(("127.0.0.1", int(sys.argv[sys.argv.index("--port") + 1])), Handler).serve_forever()
