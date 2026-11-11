import io
import qrcode
import requests
import time
import json
from uuid import uuid4
from datetime import datetime, timedelta
from django.utils import timezone
from django.conf import settings
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth import login, logout
from django.contrib.auth.models import User
from django.contrib.auth.decorators import login_required
from django.contrib.admin.views.decorators import staff_member_required
from django.views.generic import CreateView
from django.urls import reverse_lazy
from django.http import HttpResponse, JsonResponse
from django.views.decorators.http import require_POST, require_GET
from django.contrib import messages

from .models import ServerProfile, AdminSettings, ServerMetric
from .forms import SignUpForm
from .api import get_client_for_server, get_server_domain


# ---------- Публичные страницы ----------
def home(request):
    return render(request, 'home.html')


class SignUpView(CreateView):
    form_class = SignUpForm
    template_name = 'signup.html'
    success_url = reverse_lazy('dashboard')

    def form_valid(self, form):
        user = form.save()
        login(self.request, user)
        return redirect('dashboard')


@require_GET
def server_status(request):
    statuses = {}
    for key, url in [('server1', settings.SERVER_1_API_URL), ('server2', settings.SERVER_2_API_URL)]:
        try:
            requests.get(url, timeout=4, verify=False)
            statuses[key] = True
        except Exception:
            statuses[key] = False
    return JsonResponse(statuses)


# ---------- Личный кабинет ----------
@login_required
def dashboard(request):
    profiles = ServerProfile.objects.filter(user=request.user).order_by('server', 'protocol', 'created_at')
    servers_data = {
        'server1': {'name': 'Сервер 1', 'profiles': [p for p in profiles if p.server == 'server1']},
        'server2': {'name': 'Сервер 2', 'profiles': [p for p in profiles if p.server == 'server2']},
    }
    return render(request, 'dashboard.html', {'servers': servers_data})


@login_required
@require_POST
def create_profile(request):
    server_key = request.POST.get('server')
    protocol = request.POST.get('protocol')

    if server_key not in ('server1', 'server2') or protocol not in ('vless', 'amneziawg'):
        messages.error(request, 'Неверные параметры.')
        return redirect('dashboard')

    # Лимит: не более 2 подписок на каждый протокол на каждом сервере
    existing_count = ServerProfile.objects.filter(user=request.user, server=server_key, protocol=protocol).count()
    if existing_count >= 2:
        messages.error(request, f'Достигнут лимит подписок для {protocol} на этом сервере (максимум 2).')
        return redirect('dashboard')

    # Выбор ID инбаунда
    if protocol == 'vless':
        inbound_id = settings.SERVER_1_INBOUND_ID_VLESS if server_key == 'server1' else settings.SERVER_2_INBOUND_ID_VLESS
    else:
        inbound_id = settings.SERVER_1_INBOUND_ID_AWG if server_key == 'server1' else settings.SERVER_2_INBOUND_ID_AWG

    email = f'{request.user.username}_{server_key}_{protocol}{existing_count + 1}@vpn.local'
    vpn_uuid = str(uuid4())

    try:
        client = get_client_for_server(server_key)
        result = client.add_client(email, vpn_uuid)
    except Exception as e:
        messages.error(request, f'Ошибка при создании на сервере: {e}')
        return redirect('dashboard')

    admin_cfg = AdminSettings.load()
    ServerProfile.objects.create(
        user=request.user,
        server=server_key,
        protocol=protocol,
        vpn_email=email,
        vpn_uuid=result['uuid'],
        vpn_inbound_id=inbound_id,
        vpn_sub_id=result['sub_id'],
        subscription_expiry=timezone.now() + timedelta(days=admin_cfg.default_days),
        total_gb=admin_cfg.default_traffic_gb,
    )
    messages.success(request, 'Профиль успешно создан.')
    return redirect('dashboard')


@login_required
def qr_code(request, profile_id):
    profile = get_object_or_404(ServerProfile, pk=profile_id, user=request.user)
    if not profile.is_active():
        return HttpResponse('Подписка истекла', status=403)

    if profile.protocol == 'amneziawg':
        return HttpResponse('QR-код недоступен для AmneziaWG. Используйте ссылку vpn://', status=400)

    domain = get_server_domain(profile.server)
    sub_link = f'https://{domain}:2096/sub/{profile.vpn_sub_id}'
    qr = qrcode.make(sub_link)
    buf = io.BytesIO()
    qr.save(buf, format='PNG')
    return HttpResponse(buf.getvalue(), content_type='image/png')


@login_required
def subscription_link(request, profile_id):
    profile = get_object_or_404(ServerProfile, pk=profile_id, user=request.user)
    if not profile.is_active():
        return HttpResponse('Подписка истекла', status=403)

    if profile.protocol == 'amneziawg':
        try:
            client = get_client_for_server(profile.server)
            links = client.get_client_links(profile.vpn_email)
            vpn_link = next((l for l in links if l.startswith('vpn://')), None)
            if vpn_link:
                return HttpResponse(vpn_link)
            return HttpResponse('Ссылка vpn:// не найдена на сервере', status=404)
        except Exception as e:
            return HttpResponse(f'Ошибка: {e}', status=500)

    domain = get_server_domain(profile.server)
    return HttpResponse(f'https://{domain}:2096/sub/{profile.vpn_sub_id}')


def logout_view(request):
    logout(request)
    return redirect('home')


# ---------- Админ ----------
@staff_member_required
def admin_dashboard(request):
    return render(request, 'admin_dashboard.html')


@staff_member_required
@require_GET
def api_admin_traffic(request):
    res = {}
    for key in ['server1', 'server2']:
        try:
            client = get_client_for_server(key)
            stats = client.get_server_status()
            if stats:
                res[key] = {
                    'online': True,
                    'up': int(stats.get('netIO', {}).get('up', 0)),
                    'down': int(stats.get('netIO', {}).get('down', 0)),
                }
            else:
                res[key] = {'online': False}
        except Exception:
            res[key] = {'online': False}
    return JsonResponse(res)


@staff_member_required
@require_GET
def api_admin_users(request):
    online_emails = set()
    for key in ['server1', 'server2']:
        try:
            client = get_client_for_server(key)
            emails = client.get_online_users()
            online_emails.update(emails)
        except Exception:
            pass

    users_data = []
    for user in User.objects.prefetch_related('profiles').all().order_by('-date_joined'):
        is_online = False
        subs = []
        for p in user.profiles.all():
            if p.vpn_email in online_emails:
                is_online = True
            subs.append({
                'id': p.id,
                'server': p.get_server_display(),
                'server_key': p.server,
                'protocol': p.get_protocol_display(),
                'protocol_key': p.protocol,
                'days_left': p.days_left(),
                'active': p.is_active(),
            })
        users_data.append({
            'id': user.id,
            'username': user.username,
            'email': user.email,
            'online': is_online,
            'subs': subs
        })
    return JsonResponse({'users': users_data})


@staff_member_required
@require_POST
def api_admin_action(request):
    try:
        data = json.loads(request.body)
    except Exception:
        return JsonResponse({'success': False, 'msg': 'Неверный формат данных'}, status=400)

    action = data.get('action')
    user_id = data.get('user_id')
    sub_id = data.get('sub_id')
    user_obj = User.objects.filter(id=user_id).first() if user_id else None

    if action == 'change_password':
        password = data.get('password', '')
        if len(password) < 6:
            return JsonResponse({'success': False, 'msg': 'Пароль слишком короткий'})
        if user_obj:
            user_obj.set_password(password)
            user_obj.save()
            return JsonResponse({'success': True, 'msg': 'Пароль успешно изменён'})
        return JsonResponse({'success': False, 'msg': 'Пользователь не найден'})

    if action == 'create_sub':
        if not user_obj:
            return JsonResponse({'success': False, 'msg': 'Пользователь не найден'})

        server_key = data.get('server')
        protocol = data.get('protocol')

        existing_count = ServerProfile.objects.filter(user=user_obj, server=server_key, protocol=protocol).count()
        if existing_count >= 2:
            return JsonResponse({'success': False, 'msg': 'Лимит подписок (2) для этого протокола'})

        if protocol == 'vless':
            inbound_id = settings.SERVER_1_INBOUND_ID_VLESS if server_key == 'server1' else settings.SERVER_2_INBOUND_ID_VLESS
        else:
            inbound_id = settings.SERVER_1_INBOUND_ID_AWG if server_key == 'server1' else settings.SERVER_2_INBOUND_ID_AWG

        email = f'{user_obj.username}_{server_key}_{protocol}{existing_count + 1}@vpn.local'
        vpn_uuid = str(uuid4())

        try:
            client = get_client_for_server(server_key)
            result = client.add_client(email, vpn_uuid)
        except Exception as e:
            return JsonResponse({'success': False, 'msg': f'Ошибка API: {e}'})

        admin_cfg = AdminSettings.load()
        ServerProfile.objects.create(
            user=user_obj, server=server_key, protocol=protocol,
            vpn_email=email, vpn_uuid=result['uuid'], vpn_inbound_id=inbound_id,
            vpn_sub_id=result['sub_id'],
            subscription_expiry=timezone.now() + timedelta(days=admin_cfg.default_days),
            total_gb=admin_cfg.default_traffic_gb,
        )
        return JsonResponse({'success': True, 'msg': 'Подписка создана'})

    profile = ServerProfile.objects.filter(id=sub_id).first()
    if not profile:
        return JsonResponse({'success': False, 'msg': 'Подписка не найдена'})

    if action == 'extend':
        days = int(data.get('days', 0))
        try:
            if profile.is_active() and profile.subscription_expiry:
                new_expiry = profile.subscription_expiry + timedelta(days=days)
            else:
                new_expiry = timezone.now() + timedelta(days=days)
            if new_expiry < timezone.now():
                new_expiry = timezone.now() - timedelta(minutes=1)

            client = get_client_for_server(profile.server)
            client.update_client(
                email=profile.vpn_email, uuid=str(profile.vpn_uuid),
                sub_id=profile.vpn_sub_id, expiry_time=new_expiry,
                enable=(new_expiry > timezone.now()), total_gb=profile.total_gb
            )
            profile.subscription_expiry = new_expiry
            profile.save()
            return JsonResponse({'success': True, 'msg': f'Срок изменён на {days} дн.'})
        except Exception as e:
            return JsonResponse({'success': False, 'msg': f'Ошибка API: {e}'})

    if action == 'delete_sub':
        try:
            client = get_client_for_server(profile.server)
            client.delete_client(profile.vpn_email)
            profile.delete()
            return JsonResponse({'success': True, 'msg': 'Подписка удалена'})
        except Exception as e:
            return JsonResponse({'success': False, 'msg': f'Ошибка API: {e}'})

    return JsonResponse({'success': False, 'msg': 'Неизвестное действие'})


@staff_member_required
def admin_settings(request):
    settings_obj = AdminSettings.load()
    if request.method == 'POST':
        settings_obj.default_days = int(request.POST.get('default_days', 30))
        settings_obj.default_traffic_gb = int(request.POST.get('default_traffic_gb', 0))
        settings_obj.footer_text = request.POST.get('footer_text', 'VPN Service').strip()
        settings_obj.save()
        messages.success(request, 'Настройки сохранены.')
        return redirect('admin_settings')
    return render(request, 'admin_settings.html', {'settings': settings_obj})


@require_GET
def api_ping_servers(request):
    results = {}
    for key, url in [('server1', settings.SERVER_1_API_URL), ('server2', settings.SERVER_2_API_URL)]:
        try:
            start = time.time()
            requests.get(url, timeout=3, verify=False)
            results[key] = {'online': True, 'ping': int((time.time() - start) * 1000)}
        except Exception:
            results[key] = {'online': False, 'ping': 0}
    return JsonResponse(results)