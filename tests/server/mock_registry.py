import json
import re
import http.server as HTTPSERVER

from threading import Thread, Event

from schema_registry.client import load, errors
from schema_registry.client import status as status_codes

from tests.client.mock_schema_registry_client import MockSchemaRegistryClient


class ReqHandler(HTTPSERVER.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def do_GET(self):
        self.server._run_routes(self)

    def do_POST(self):
        self.server._run_routes(self)

    def log_message(self, format, *args):
        pass


class MockServer(HTTPSERVER.HTTPServer, object):
    def __init__(self, *args, **kwargs):
        super(MockServer, self).__init__(*args, **kwargs)
        self.counts = {}
        self.registry = MockSchemaRegistryClient()
        self.schema_cache = {}
        self.all_routes = {
            "GET": [
                (r"/schemas/ids/(\d+)", "get_schema_by_id"),
                (r"/subjects/([-\w]+)/versions/latest", "get_latest"),
            ],
            "POST": [
                (r"/subjects/([-\w]+)/versions", "register"),
                (r"/subjects/([-\w]+)", "get_version"),
            ],
        }

    def _send_response(self, resp, status, body):
        resp.send_response(status)
        resp.send_header("Content-Type", "application/json")
        resp.end_headers()
        resp.wfile.write(json.dumps(body).encode())
        resp.finish()

    def _create_error(self, msg, status=status_codes.HTTP_400_BAD_REQUEST, err_code=1):
        return (status, {"error_code": err_code, "message": msg})

    def _run_routes(self, req):
        self.add_count((req.command, req.path))
        routes = self.all_routes.get(req.command, [])
        for r in routes:
            m = re.match(r[0], req.path)
            if m:
                func = getattr(self, r[1])
                status, body = func(req, m.groups())
                return self._send_response(req, status, body)

        # here means we got a bad req
        status, body = self._create_error("bad path specified")
        self._send_response(req, status, body)

    def get_schema_by_id(self, req, groups):
        schema_id = int(groups[0])
        schema = self.registry.get_by_id(schema_id)
        if not schema:
            return self._create_error(
                "schema not found", status_codes.HTTP_404_NOT_FOUND
            )
        result = {"schema": json.dumps(schema.to_json())}
        return (200, result)

    def _get_identity_schema(self, avro_schema):
        # normalized
        schema_str = json.dumps(avro_schema.to_json())
        if schema_str in self.schema_cache:
            return self.schema_cache[schema_str]
        self.schema_cache[schema_str] = avro_schema
        return avro_schema

    def _get_schema_from_body(self, req):
        length = int(req.headers["content-length"])
        data = req.rfile.read(length)
        data = json.loads(data.decode("utf-8"))
        schema = data.get("schema", None)
        if not schema:
            return
        try:
            avro_schema = load.loads(schema)
            return self._get_identity_schema(avro_schema)
        except errors.ClientError:
            return

    def register(self, req, groups):
        avro_schema = self._get_schema_from_body(req)
        if not avro_schema:
            return self._create_error(
                "Invalid avro schema", status_codes.HTTP_422_UNPROCESSABLE_ENTITY, 42201
            )
        subject = groups[0]
        schema_id = self.registry.register(subject, avro_schema)
        return (200, {"id": schema_id})

    def get_version(self, req, groups):
        avro_schema = self._get_schema_from_body(req)
        if not avro_schema:
            return self._create_error(
                "Invalid avro schema", status_codes.HTTP_422_UNPROCESSABLE_ENTITY, 42201
            )
        subject = groups[0]
        version = self.registry.get_version(subject, avro_schema)
        if version == -1:
            return self._create_error("Not found", status_codes.HTTP_404_NOT_FOUND)
        schema_id = self.registry.get_id_for_schema(subject, avro_schema)

        result = {
            "schema": json.dumps(avro_schema.to_json()),
            "subject": subject,
            "id": schema_id,
            "version": version,
        }
        return (200, result)

    def get_latest(self, req, groups):
        subject = groups[0]
        schema_id, avro_schema, version = self.registry.get_latest_schema(subject)
        if schema_id is None:
            return self._create_error("Not found", status_codes.HTTP_404_NOT_FOUND)
        result = {
            "schema": json.dumps(avro_schema.to_json()),
            "subject": subject,
            "id": schema_id,
            "version": version,
        }
        return (200, result)

    def add_count(self, path):
        if path not in self.counts:
            self.counts[path] = 0
        self.counts[path] += 1


class ServerThread(Thread):
    def __init__(self, port):
        Thread.__init__(self)
        self.server = None
        self.port = port
        self.daemon = True
        self.started = Event()

    def run(self):
        self.server = MockServer(("127.0.0.1", self.port), ReqHandler)
        self.started.set()
        self.server.serve_forever()

    def start(self):
        """Start, and wait for server to be fully started, before returning."""
        super(ServerThread, self).start()
        self.started.wait()

    def shutdown(self):
        if self.server:
            self.server.shutdown()
            self.server.socket.close()


if __name__ == "__main__":
    s = ServerThread(0)
    s.start()
