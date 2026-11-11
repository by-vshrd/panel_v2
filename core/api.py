import requests
import json
import re
import time
from datetime import datetime, timedelta
from urllib.parse import quote
import urllib3
from django.conf import settings

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class XUIClient:
    def __init__(self, api_url, username, password):
        self.base = api_url.rstrip('/')
        self.username = username
        self.password = password
        self.session = requests.Session()
        self.session.verify = False
        self.csrf_token = None
        self._login()

    def _login(self):
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

    def add_client(self, email, uuid, inbound_id):
        expiry_ms = int((datetime.now() + timedelta(days=30)).timestamp() * 1000)
        client_data = {
            'email': email, 'id': uuid, 'enable': True,
            'expiryTime': expiry_ms, 'totalGB': 0, 'limitIp': 0,
            'flow': '', 'security': 'auto', 'password': uuid,
        }
        payload = {'inboundIds': [inbound_id], 'client': client_data}

        url = f'{self.base}/login/panel/api/clients/add'
        resp = self.session.post(url, json=payload, headers=self._headers(), timeout=15)
        data = self._safe_json(resp)
        if not data.get('success'):
            raise Exception(f'Ошибка создания: {data.get("msg")}')

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
        encoded_email = quote(email, safe='')
        url = f'{self.base}/login/panel/api/clients/get/{encoded_email}'
        resp = self.session.get(url, headers=self._headers(), timeout=15)
        data = self._safe_json(resp)
        if data.get('success'):
            return (data.get('obj') or {}).get('client')
        return None

    def update_client(self, email, uuid, sub_id, expiry_time, enable=True, total_gb=0):
        encoded_email = quote(email, safe='')
        url = f'{self.base}/login/panel/api/clients/update/{encoded_email}'
        expiry_ms = int(expiry_time.timestamp() * 1000) if expiry_time else 0

        payload = {
            'id': uuid, 'subId': sub_id, 'email': email, 'enable': enable,
            'expiryTime': expiry_ms, 'totalGB': total_gb, 'limitIp': 0,
            'flow': '', 'security': 'auto', 'password': uuid,
            'auth': '', 'comment': '', 'tgId': 0, 'reset': 0, 'group': '',
        }

        resp = self.session.post(url, json=payload, headers=self._headers(), timeout=15)
        data = self._safe_json(resp)
        if not data.get('success'):
            raise Exception(f'Ошибка обновления: {data.get("msg")}')

        time.sleep(0.6)
        fresh = self.get_client(email)
        if fresh and fresh.get('expiryTime', 0) != expiry_ms:
            raise Exception('Панель отчиталась об успехе, но данные не обновились')
        return True

    def delete_client(self, email):
        encoded_email = quote(email, safe='')
        url = f'{self.base}/login/panel/api/clients/del/{encoded_email}'
        resp = self.session.post(url, headers=self._headers(), timeout=15)
        data = self._safe_json(resp)
        if not data.get('success'):
            raise Exception(f'Ошибка удаления: {data.get("msg")}')

    def get_server_status(self):
        url = f'{self.base}/login/panel/api/server/status'
        resp = self.session.get(url, headers=self._headers(), timeout=10)
        data = self._safe_json(resp)
        return data.get('obj') if data.get('success') else None

    def get_online_users(self):
        endpoints = ['/login/panel/api/clients/onlines', '/login/panel/api/inbounds/onlines']
        for endpoint in endpoints:
            url = f'{self.base}{endpoint}'
            try:
                resp = self.session.post(url, headers=self._headers(), timeout=5)
                data = self._safe_json(resp)
                if not data.get('success'):
                    continue
                obj = data.get('obj')
                emails = []
                if isinstance(obj, list):
                    for item in obj:
                        if isinstance(item, str): emails.append(item)
                        elif isinstance(item, dict) and item.get('email'): emails.append(item['email'])
                elif isinstance(obj, dict):
                    emails = list(obj.keys())
                if emails: return emails
            except Exception:
                pass
        return []

    def get_client_links(self, email):
        encoded_email = quote(email, safe='')
        url = f'{self.base}/login/panel/api/clients/links/{encoded_email}'
        try:
            resp = self.session.get(url, headers=self._headers(), timeout=15)
            data = self._safe_json(resp)
            if data.get('success'):
                obj = data.get('obj')
                if isinstance(obj, list): return obj
                if isinstance(obj, str): return [obj]
        except Exception:
            pass
        return []


def get_client_for_server(server_key):
    if server_key == 'server1':
        return XUIClient(settings.SERVER_1_API_URL, settings.SERVER_1_USERNAME, settings.SERVER_1_PASSWORD)
    elif server_key == 'server2':
        return XUIClient(settings.SERVER_2_API_URL, settings.SERVER_2_USERNAME, settings.SERVER_2_PASSWORD)
    raise ValueError(f'Неизвестный сервер: {server_key}')


def get_server_domain(server_key):
    return settings.SERVER_1_DOMAIN if server_key == 'server1' else settings.SERVER_2_DOMAIN