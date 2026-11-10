from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest

from flightrecorder import paypal


@pytest.mark.parametrize('mode', ['redirect', 'oversized', 'truncated'])
def test_transport_rejects_redirects_and_invalid_responses(monkeypatch, mode):
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            received.append(self.path)
            redirect = mode == 'redirect' and self.path == '/start'
            self.send_response(302 if redirect else 200)
            if redirect:
                self.send_header('Location', '/redirect-target')
            elif mode == 'truncated':
                self.send_header('Content-Length', '4')
            self.end_headers()
            if mode != 'redirect':
                self.wfile.write(b'{}' if mode == 'truncated' else b'{   }')

        def log_message(self, *args):
            pass

    with ThreadingHTTPServer(('127.0.0.1', 0), Handler) as server:
        origin = f'http://127.0.0.1:{server.server_port}'
        monkeypatch.setattr(paypal, 'SANDBOX_URL', origin)
        monkeypatch.setattr(paypal, 'MAX_RESPONSE_BYTES', 4)
        worker = Thread(target=server.serve_forever)
        worker.start()
        try:
            if mode == 'redirect':
                code, _ = paypal.urllib_transport('GET', origin + '/start', {'Authorization': 'Bearer fixture'}, None)
                assert code == 302
            else:
                with pytest.raises(paypal.PayPalError, match='size limit|declared length'):
                    paypal.urllib_transport('GET', origin + '/start', {}, None)
        finally:
            server.shutdown()
            worker.join()
    assert received == ['/start']


@pytest.mark.parametrize('resource_id', ['../orders', 'a/b', '..', 'a%2fb', ''])
def test_malformed_resource_id_is_rejected_before_network(resource_id):
    def unexpected(*args):
        pytest.fail('invalid identifier reached PayPal transport')

    client = paypal.PayPalClient(paypal.PayPalConfig('id', 'fixture'), unexpected)
    with pytest.raises(paypal.PayPalError, match='Invalid PayPal resource ID'):
        client.get_order(resource_id)
    with pytest.raises(paypal.PayPalError, match='Invalid PayPal resource ID'):
        client.get_dispute(resource_id)
