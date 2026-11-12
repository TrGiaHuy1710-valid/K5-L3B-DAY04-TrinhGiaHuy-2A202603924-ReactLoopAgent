"""Bốn provider cùng một interface. Không chứa agent loop."""
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from pathlib import Path


def _load_dotenv(path=Path(__file__).resolve().with_name('.env')):
    """Đọc .env cạnh file này (KEY=VALUE hoặc export KEY=VALUE).

    Biến đã đặt trong shell được ưu tiên; dòng có giá trị rỗng bị bỏ qua.
    """
    if not path.is_file():
        return
    for line in path.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if line.startswith('export '):
            line = line[len('export '):].lstrip()
        key, sep, value = line.partition('=')
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in '"\'':
            value = value[1:-1]
        if sep and key and value:
            os.environ.setdefault(key, value)


_load_dotenv()

CONFIG = {
    'openai': ('OPENAI_API_KEY', 'https://api.openai.com/v1/chat/completions'),
    'gemini': ('GEMINI_API_KEY', 'https://generativelanguage.googleapis.com/v1beta/openai/chat/completions'),
    'openrouter': ('OPENROUTER_API_KEY', 'https://openrouter.ai/api/v1/chat/completions'),
    'anthropic': ('ANTHROPIC_API_KEY', 'https://api.anthropic.com/v1/messages'),
}


def _env_number(name, default):
    try:
        return float(os.getenv(name, '') or default)
    except ValueError:
        return float(default)


def _log(message):
    print(f'[provider] {message}', file=sys.stderr, flush=True)


class _RateLimiter:
    """Tối đa LLM_RPM request trong mỗi cửa sổ 60 giây, tính riêng từng model.

    Dùng chung cho mọi Provider trong process (agent + judge, nhiều --workers) nên khóa bằng lock.
    LLM_RPM <= 0 hoặc không đặt: không giới hạn.
    """
    def __init__(self):
        self._lock = threading.Lock()
        self._sent = {}

    def wait(self, key):
        rpm = int(_env_number('LLM_RPM', 0))
        if rpm <= 0:
            return
        while True:
            with self._lock:
                now = time.monotonic()
                sent = self._sent.setdefault(key, deque())
                while sent and now - sent[0] >= 60:
                    sent.popleft()
                if len(sent) < rpm:
                    sent.append(now)
                    return
                delay = 60 - (now - sent[0]) + 0.5
            _log(f'Giới hạn {rpm} request/phút cho {key}: chờ {delay:.0f}s')
            time.sleep(delay)


_LIMITER = _RateLimiter()
RETRY_STATUS = {429, 500, 502, 503, 504}


def _read_error_body(exc):
    # Chỉ dùng để nhận diện loại lỗi; không in ra vì có thể chứa thông tin tài khoản/request.
    try:
        return exc.read(8192).decode('utf-8', 'replace')
    except Exception:
        return ''


def _retry_delay(exc, body, attempt):
    """Ưu tiên Retry-After (header), rồi retryDelay của Gemini, cuối cùng là backoff lũy thừa."""
    delay = None
    header = exc.headers.get('Retry-After') if exc.headers else None
    if header:
        try:
            delay = float(header)
        except ValueError:
            delay = None
    if delay is None:
        match = re.search(r'"retryDelay"\s*:\s*"(\d+(?:\.\d+)?)s"', body)
        if match:
            delay = float(match.group(1))
    if delay is None:
        delay = 10 * 2 ** attempt
    return min(delay + 1, _env_number('LLM_MAX_WAIT', 90))


def post_json(url, headers, payload):
    data = json.dumps(payload).encode()
    retries = int(_env_number('LLM_MAX_RETRIES', 4))
    limit_key = payload.get('model', url)
    for attempt in range(retries + 1):
        _LIMITER.wait(limit_key)
        req = urllib.request.Request(url, data=data, headers={'Content-Type': 'application/json', **headers})
        try:
            with urllib.request.urlopen(req, timeout=_env_number('LLM_TIMEOUT', 60)) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            body = _read_error_body(exc)
            if exc.code == 429 and 'PerDay' in body:
                raise RuntimeError('Provider trả HTTP 429: đã hết quota theo NGÀY của model này; '
                                   'thử lại sẽ không giúp được. Đổi model/key hoặc chờ quota reset.') from exc
            if exc.code not in RETRY_STATUS or attempt == retries:
                raise RuntimeError(f'Provider trả HTTP {exc.code}; kiểm tra model, key và quota.') from exc
            delay = _retry_delay(exc, body, attempt)
            _log(f'HTTP {exc.code} từ provider, thử lại lần {attempt + 1}/{retries} sau {delay:.0f}s')
            time.sleep(delay)
        except urllib.error.URLError as exc:
            raise RuntimeError(f'Không kết nối được provider: {exc.reason}') from exc


class Provider:
    """complete(messages, schemas) -> {message, calls, finish, usage}.

    messages: system/user/assistant/tool theo format Chat Completions. message trả về
    cần được append nguyên vẹn; tool result có role, tool_call_id, content (JSON string).
    """
    def __init__(self, name, model):
        if name not in CONFIG:
            raise ValueError(f'Provider không hợp lệ: {name}')
        if not model:
            raise ValueError('Cần đặt MODEL là model có hỗ trợ tool calling.')
        self.name, self.model = name, model
        key_name, self.url = CONFIG[name]
        self.key = os.getenv(key_name)
        if not self.key:
            raise ValueError(f'Thiếu {key_name}; xem README.md.')

    def complete(self, messages, schemas):
        if self.name == 'anthropic':
            return self._anthropic(messages, schemas)
        payload = {'model': self.model, 'messages': messages}
        if schemas:
            payload['tools'] = schemas
        data = post_json(self.url, {'Authorization': f'Bearer {self.key}'}, payload)
        choice = data['choices'][0]
        message = choice['message']
        calls = [{'id': c['id'], 'name': c['function']['name'], 'arguments': c['function']['arguments']} for c in message.get('tool_calls', [])]
        return {'message': message, 'calls': calls, 'finish': choice.get('finish_reason'), 'usage': data.get('usage', {})}

    def _anthropic(self, messages, schemas):
        system = '\n'.join(m['content'] for m in messages if m['role'] == 'system')
        native = []
        for m in messages:
            if m['role'] == 'system':
                continue
            if m['role'] == 'assistant':
                blocks = []
                if m.get('content'):
                    blocks.append({'type': 'text', 'text': m['content']})
                for call in m.get('tool_calls', []):
                    blocks.append({'type': 'tool_use', 'id': call['id'], 'name': call['function']['name'], 'input': json.loads(call['function']['arguments'])})
                native.append({'role': 'assistant', 'content': blocks})
            elif m['role'] == 'tool':
                block = {'type': 'tool_result', 'tool_use_id': m['tool_call_id'], 'content': m['content']}
                if native and native[-1]['role'] == 'user' and isinstance(native[-1]['content'], list):
                    native[-1]['content'].append(block)
                else:
                    native.append({'role': 'user', 'content': [block]})
            else:
                native.append({'role': 'user', 'content': m['content']})
        tools = [{'name': x['function']['name'], 'description': x['function']['description'], 'input_schema': x['function']['parameters']} for x in schemas]
        payload = {'model': self.model, 'max_tokens': 1024, 'system': system, 'messages': native}
        if tools:
            payload['tools'] = tools
        data = post_json(self.url, {'x-api-key': self.key, 'anthropic-version': '2023-06-01'}, payload)
        blocks = data['content']
        calls = [{'id': b['id'], 'name': b['name'], 'arguments': json.dumps(b['input'], ensure_ascii=False)} for b in blocks if b['type'] == 'tool_use']
        text = '\n'.join(b['text'] for b in blocks if b['type'] == 'text')
        message = {'role': 'assistant', 'content': text or None}
        if calls:
            message['tool_calls'] = [{'id': c['id'], 'type': 'function', 'function': {'name': c['name'], 'arguments': c['arguments']}} for c in calls]
        reason = {'end_turn': 'stop', 'tool_use': 'tool_calls', 'max_tokens': 'length'}.get(data.get('stop_reason'), data.get('stop_reason'))
        return {'message': message, 'calls': calls, 'finish': reason, 'usage': data.get('usage', {})}
