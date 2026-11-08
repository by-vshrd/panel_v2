import io
import qrcode
import requests
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
from .models import ServerProfile, AdminSettings
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
    """Проверяет доступность обоих серверов (упрощённо: по доступности панели)."""
    statuses = {}
    for key, url in [('server1', settings.SERVER_1_API_URL),
                     ('server2', settings.SERVER_2_API_URL)]:
        try:
            requests.get(url, timeout=4, verify=False)
            statuses[key] = True
        except Exception:
            statuses[key] = False
    return JsonResponse(statuses)


# ---------- Личный кабинет ----------
@login_required
def dashboard(request):
    profiles = ServerProfile.objects.filter(user=request.user).order_by('server', 'protocol')

    # Группируем по серверу
    servers_data = {
        'server1': {
            'name': 'Сервер 1',
            'profiles': [p for p in profiles if p.server == 'server1'],
        },
        'server2': {
            'name': 'Сервер 2',
            'profiles': [p for p in profiles if p.server == 'server2'],
        },
    }
    context = {'servers': servers_data}
    return render(request, 'dashboard.html', context)


@login_required
@require_POST
def create_profile(request):
    server_key = request.POST.get('server')
    protocol = request.POST.get('protocol')

    if server_key not in ('server1', 'server2') or protocol not in ('vless', 'hysteria'):
        messages.error(request, 'Неверные параметры.')
        return redirect('dashboard')

    if ServerProfile.objects.filter(user=request.user, server=server_key, protocol=protocol).exists():
        messages.error(request, 'Профиль для этого сервера и протокола уже создан.')
        return redirect('dashboard')

    email = f'{request.user.username}_{server_key}_{protocol}@vpn.local'
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
        vpn_inbound_id=(
            settings.SERVER_1_INBOUND_ID if server_key == 'server1'
            else settings.SERVER_2_INBOUND_ID
        ),
        vpn_sub_id=result['sub_id'],
        subscription_expiry=timezone.now() + timedelta(days=admin_cfg.default_days),
        total_gb=admin_cfg.default_traffic_gb,
    )
    messages.success(request, 'Профиль создан.')
    return redirect('dashboard')


@login_required
def qr_code(request, profile_id):
    profile = get_object_or_404(ServerProfile, pk=profile_id, user=request.user)
    if not profile.is_active():
        return HttpResponse('Подписка истекла', status=403)
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
    domain = get_server_domain(profile.server)
    return HttpResponse(f'https://{domain}:2096/sub/{profile.vpn_sub_id}')


def logout_view(request):
    logout(request)
    return redirect('home')


# ---------- Админ ----------
@staff_member_required
def admin_dashboard(request):
    total_users = User.objects.count()
    total_profiles = ServerProfile.objects.count()
    active_profiles = ServerProfile.objects.filter(subscription_expiry__gt=timezone.now()).count()

    users = User.objects.prefetch_related('profiles').order_by('-date_joined')

    context = {
        'total_users': total_users,
        'total_profiles': total_profiles,
        'active_profiles': active_profiles,
        'users': users,
    }
    return render(request, 'admin_dashboard.html', context)


@staff_member_required
def manage_user(request, user_id):
    user_obj = get_object_or_404(User, pk=user_id)
    profiles = user_obj.profiles.all()

    if request.method == 'POST' and request.POST.get('action') == 'delete_profile':
        profile_id = request.POST.get('profile_id')
        profile = get_object_or_404(ServerProfile, pk=profile_id, user=user_obj)
        try:
            client = get_client_for_server(profile.server)
            client.delete_client(profile.vpn_email)
        except Exception as e:
            messages.error(request, f'Ошибка API при удалении: {e}')
            return redirect('manage_user', user_id=user_id)
        profile.delete()
        messages.success(request, 'Профиль удалён.')
        return redirect('manage_user', user_id=user_id)

    context = {'user_obj': user_obj, 'profiles': profiles}
    return render(request, 'manage_user.html', context)


@staff_member_required
@require_POST
def manual_extend(request, user_id):
    user_obj = get_object_or_404(User, pk=user_id)
    try:
        days = int(request.POST.get('days', 30))
    except ValueError:
        messages.error(request, 'Неверное число дней.')
        return redirect('manage_user', user_id=user_id)

    for profile in user_obj.profiles.all():
        try:
            if profile.is_active():
                new_expiry = profile.subscription_expiry + timedelta(days=days)
            else:
                new_expiry = timezone.now() + timedelta(days=days)
            profile.subscription_expiry = new_expiry
            profile.save()

            client = get_client_for_server(profile.server)
            client.update_client(
                email=profile.vpn_email,
                uuid=str(profile.vpn_uuid),
                sub_id=profile.vpn_sub_id,
                expiry_time=new_expiry,
                enable=True,
                total_gb=profile.total_gb,
            )
        except Exception as e:
            messages.error(request, f'Ошибка при продлении {profile.vpn_email}: {e}')
            return redirect('manage_user', user_id=user_id)

    messages.success(request, f'Все профили продлены на {days} дней.')
    return redirect('manage_user', user_id=user_id)


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

import math  # для форматирования размера


# ... остальные импорты ...

@staff_member_required
def admin_statistics(request):
    """Страница статистики для администраторов."""

    def get_stats_for_server(server_key):
        try:
            client = get_client_for_server(server_key)
            stats = client.get_server_status()
            if not stats:
                return {'online': False, 'error': 'Нет данных от API'}

            # Форматируем данные для удобного вывода
            uptime_sec = stats.get('uptime', 0)
            uptime_days = uptime_sec // 86400
            uptime_hours = (uptime_sec % 86400) // 3600

            # Размер памяти (обычно приходит в MB)
            mem_total = stats.get('mem', {}).get('total', 0)
            mem_current = stats.get('mem', {}).get('current', 0)
            mem_percent = round((mem_current / mem_total * 100), 1) if mem_total > 0 else 0

            return {
                'online': True,
                'name': 'Сервер 1' if server_key == 'server1' else 'Сервер 2',
                'cpu': stats.get('cpu', 0),
                'cpu_cores': stats.get('cpuCores', 0),
                'mem_current': mem_current,
                'mem_total': mem_total,
                'mem_percent': mem_percent,
                'uptime_days': uptime_days,
                'uptime_hours': uptime_hours,
                'onlines': stats.get('onlines', 0),
                'xray_state': stats.get('xray', {}).get('state', 'unknown'),
                'xray_version': stats.get('xray', {}).get('version', 'unknown'),
                'net_up': stats.get('netIO', {}).get('up', 0),
                'net_down': stats.get('netIO', {}).get('down', 0),
            }
        except Exception as e:
            return {'online': False, 'name': 'Сервер 1' if server_key == 'server1' else 'Сервер 2', 'error': str(e)}

    # Собираем данные с обоих серверов
    server1_stats = get_stats_for_server('server1')
    server2_stats = get_stats_for_server('server2')

    context = {
        'server1': server1_stats,
        'server2': server2_stats,
    }
    return render(request, 'admin_statistics.html', context)