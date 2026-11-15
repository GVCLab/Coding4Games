import http.client
import json

from coding4game_gui.benchmark.code.model_gateway import ModelGateway


class Response:
    def __init__(self, body, content_type="application/json"):
        self.status = 200
        self.headers = {"content-type": content_type}
        self.body = body
    def iter_bytes(self): yield self.body
    def close(self): pass


class Transport:
    def __init__(self, responses): self.responses=iter(responses); self.calls=[]
    def __call__(self, method, url, headers, body, timeout):
        self.calls.append((method,url,headers,json.loads(body),timeout))
        return next(self.responses)


def post(gateway, path, body):
    connection=http.client.HTTPConnection("127.0.0.1",gateway.port,timeout=3)
    headers = {"Content-Type": "application/json", "Authorization": "Bearer " + gateway.token}
    connection.request("POST",path,json.dumps(body),headers)
    response=connection.getresponse(); payload=response.read()
    return response.status,payload


def test_anthropic_route_rewrites_model_headers_and_accounts_cache_tokens(tmp_path):
    transport=Transport([Response(json.dumps({"content":[],"usage":{
        "input_tokens":4,"output_tokens":3,"cache_read_input_tokens":2}}).encode())])
    with ModelGateway("https://provider.invalid/v1","host-secret","claude-fixed",50,60,
                      tmp_path/"a.jsonl",transport=transport,protocol="anthropic",
                      strict_total_enforcement=False) as gateway:
        status,_=post(gateway,"/v1/messages",{"model":"ignored","messages":[],"max_tokens":9})
        assert status==200 and gateway.status()["tokens_reported"]==9
        assert gateway.base_url==f"http://127.0.0.1:{gateway.port}"
    _,url,headers,body,_=transport.calls[0]
    assert url=="https://provider.invalid/v1/messages"
    assert body["model"]=="claude-fixed"
    assert headers["x-api-key"]=="host-secret"


def test_anthropic_strict_count_uses_messages_count_tokens(tmp_path):
    transport=Transport([Response(b'{"input_tokens":4}'),
                         Response(b'{"usage":{"input_tokens":4,"output_tokens":3}}')])
    with ModelGateway("https://provider.invalid/v1","secret","m",20,60,
                      tmp_path/"strict.jsonl",transport=transport,protocol="anthropic",
                      count_input=True) as gateway:
        assert post(gateway,"/v1/messages",{"messages":[],"max_tokens":10})[0]==200
        assert gateway.status()["tokens_used"]==7
    assert [call[1] for call in transport.calls]==[
        "https://provider.invalid/v1/messages/count_tokens",
        "https://provider.invalid/v1/messages"]


def test_anthropic_stream_combines_start_and_delta_usage(tmp_path):
    stream=(b'data: {"type":"message_start","message":{"usage":{"input_tokens":5,"output_tokens":0}}}\n\n'
            b'data: {"type":"message_delta","usage":{"output_tokens":4}}\n\n'
            b'data: {"type":"message_stop"}\n\n')
    transport=Transport([Response(stream,"text/event-stream")])
    with ModelGateway("https://provider.invalid/v1","secret","m",50,60,
                      tmp_path/"s.jsonl",transport=transport,protocol="anthropic",
                      strict_total_enforcement=False) as gateway:
        assert post(gateway,"/v1/messages",{"messages":[],"max_tokens":10})[0]==200
        assert gateway.status()["tokens_reported"]==9
