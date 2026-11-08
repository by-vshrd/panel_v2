import requests
import json
import re
import time
from datetime import datetime, timedelta
from urllib.parse import quote
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class XUIClient:
    """Клиент для работы с одним сервером 3x-ui."""

    def __init__(self, api_url, username, password, inbound_id):
        self.base = api_url.rstrip('/')
        self.username = username
        self.password = password
        self.inbound_id = inbound_id
        self.session = requests.Session()
        self.session.verify = False
        self.csrf_token = None
        self._login()

    def _login(self):
        """Вход в панель: GET для получения CSRF + POST для логина."""
        login_page = f'{self.base}/login/panel/'
        resp = self.session.get(login_page, timeout=15)
        if resp.status_code != 200:
            raise Exception(f'Не удалось открыть страницу входа (статус {resp.status_code})')

        match = re.search(r'<meta\s+name="csrf-token"\s+content="([^"]+)"', resp.text)
        self.csrf_token = match.group(1) if match else None

        data = {'username': self.username, 'password': self.password}
        headers = {'Referer': login_page}
        if self.csrf_token:
            data['_csrf'] = self.csrf_token
            headers['X-CSRF-TOKEN'] = self.csrf_token
            headers['X-XSRF-TOKEN'] = self.csrf_token

        login_url = f'{self.base}/login/login'
        resp = self.session.post(login_url, data=data, headers=headers, timeout=15)
        if resp.status_code != 200:
            raise Exception(f'Ошибка входа в панель (статус {resp.status_code})')
        try:
            json_resp = resp.json()
            if not json_resp.get('success'):
                raise Exception(f'Ошибка входа: {json_resp.get("msg")}')
        except ValueError:
            raise Exception('Не удалось разобрать ответ панели при входе')

    def _headers(self, extra=None):
        h = {'Content-Type': 'application/json'}
        if self.csrf_token:
            h['X-CSRF-TOKEN'] = self.csrf_token
        if extra:
            h.update(extra)
        return h

    def _safe_json(self, resp):
        try:
            data = resp.json()
        except Exception:
            raise Exception(f'Не JSON-ответ: {resp.text[:200]}')
        if isinstance(data, str):
            data = json.loads(data)
        if not isinstance(data, dict):
            raise Exception(f'Неожиданный ответ: {data}')
        return data

    def add_client(self, email, uuid):
        """Создаёт клиента в указанном inbound'е и возвращает (uuid, sub_id)."""
        expiry_ms = int((datetime.now() + timedelta(days=30)).timestamp() * 1000)

        client_data = {
            'email': email,
            'id': uuid,
            'enable': True,
            'expiryTime': expiry_ms,
            'totalGB': 0,
            'limitIp': 0,
            'flow': '',
            'security': 'auto',
            'password': uuid,
        }

        payload = {
            'inboundIds': [self.inbound_id],
            'client': client_data,
        }

        url = f'{self.base}/login/panel/api/clients/add'
        resp = self.session.post(url, json=payload, headers=self._headers(), timeout=15)
        data = self._safe_json(resp)
        if not data.get('success'):
            raise Exception(f'Ошибка создания: {data.get("msg")}')

        # Извлекаем реальный UUID и subId (панель генерирует свои)
        real_uuid = None
        sub_id = None
        encoded_email = quote(email, safe='')
        for _ in range(5):
            time.sleep(1.5)
            info_url = f'{self.base}/login/panel/api/clients/get/{encoded_email}'
            info_resp = self.session.get(info_url, headers=self._headers(), timeout=15)
            info = self._safe_json(info_resp)
            if info.get('success'):
                obj = info.get('obj') or {}
                client = obj.get('client') or {}
                real_uuid = client.get('uuid')
                sub_id = client.get('subId')
                if real_uuid:
                    break

        if not real_uuid:
            raise Exception('Не удалось получить UUID клиента от панели')

        return {'uuid': real_uuid, 'sub_id': sub_id}

    def get_client(self, email):
        """Возвращает словарь с данными клиента или None."""
        encoded_email = quote(email, safe='')
        url = f'{self.base}/login/panel/api/clients/get/{encoded_email}'
        resp = self.session.get(url, headers=self._headers(), timeout=15)
        data = self._safe_json(resp)
        if data.get('success'):
            obj = data.get('obj') or {}
            return obj.get('client')
        return None

    def update_client(self, email, uuid, sub_id, expiry_time, enable=True, total_gb=0):
        """Обновляет данные клиента (продление, вкл/выкл)."""
        encoded_email = quote(email, safe='')
        url = f'{self.base}/login/panel/api/clients/update/{encoded_email}'

        expiry_ms = 0
        if expiry_time is not None:
            expiry_ms = int(expiry_time.timestamp() * 1000)

        payload = {
            'id': uuid,
            'subId': sub_id,
            'email': email,
            'enable': enable,
            'expiryTime': expiry_ms,
            'totalGB': total_gb,
            'limitIp': 0,
            'flow': '',
            'security': 'auto',
            'password': '',
            'auth': '',
            'comment': '',
            'tgId': 0,
            'reset': 0,
            'group': '',
        }

        resp = self.session.post(url, json=payload, headers=self._headers(), timeout=15)
        data = self._safe_json(resp)
        if not data.get('success'):
            raise Exception(f'Ошибка обновления: {data.get("msg")}')

    def delete_client(self, email):
        """Удаляет клиента по email."""
        encoded_email = quote(email, safe='')
        url = f'{self.base}/login/panel/api/clients/del/{encoded_email}'
        resp = self.session.post(url, headers=self._headers(), timeout=15)
        data = self._safe_json(resp)
        if not data.get('success'):
            raise Exception(f'Ошибка удаления: {data.get("msg")}')

    def get_server_status(self):
        """Возвращает общую статистику сервера (CPU, RAM, Disk, Uptime и т.д.)."""
        url = f'{self.base}/login/panel/api/server/status'
        resp = self.session.get(url, headers=self._headers(), timeout=10)
        data = self._safe_json(resp)
        if data.get('success'):
            return data.get('obj')
        return None

def get_client_for_server(server_key):
    """Возвращает XUIClient для 'server1' или 'server2'."""
    from django.conf import settings
    if server_key == 'server1':
        return XUIClient(
            settings.SERVER_1_API_URL,
            settings.SERVER_1_USERNAME,
            settings.SERVER_1_PASSWORD,
            settings.SERVER_1_INBOUND_ID,
        )
    elif server_key == 'server2':
        return XUIClient(
            settings.SERVER_2_API_URL,
            settings.SERVER_2_USERNAME,
            settings.SERVER_2_PASSWORD,
            settings.SERVER_2_INBOUND_ID,
        )
    raise ValueError(f'Неизвестный сервер: {server_key}')


def get_server_domain(server_key):
    """Возвращает публичный адрес сервера для формирования ссылок подписки."""
    from django.conf import settings
    return settings.SERVER_1_DOMAIN if server_key == 'server1' else settings.SERVER_2_DOMAIN