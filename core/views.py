import io
import json
import qrcode
import requests
import time
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
    """Проверяет доступность обоих серверов."""
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


# ---------- Статистика ----------
@staff_member_required
def admin_statistics(request):
    return render(request, 'admin_statistics.html')


def get_online_count(client):
    """Запрашивает список активных пользователей и считает их количество."""
    try:
        url = f'{client.base}/login/panel/api/inbounds/onlines'
        resp = client.session.post(url, headers=client._headers(), timeout=5)
        data = resp.json()
        if data.get('success') and isinstance(data.get('obj'), list):
            return len(data['obj'])
    except Exception:
        pass
    return 0


@staff_member_required
@require_GET
def api_get_stats(request):
    time_range = request.GET.get('range', 'live')
    now = timezone.now()

    # Получаем данные с серверов
    servers_data = {}
    for key in ['server1', 'server2']:
        try:
            client = get_client_for_server(key)
            stats = client.get_server_status()
            if stats:
                servers_data[key] = {
                    'online': True,
                    'cpu': float(stats.get('cpu', 0)),
                    'mem_percent': round((float(stats.get('mem', {}).get('current', 0)) / float(
                        stats.get('mem', {}).get('total', 1)) * 100), 1),
                    'onlines': get_online_count(client),
                    'uptime': f"{stats.get('uptime', 0) // 86400}д {(stats.get('uptime', 0) % 86400) // 3600}ч",
                    'net_up': int(stats.get('netIO', {}).get('up', 0)),
                    'net_down': int(stats.get('netIO', {}).get('down', 0)),
                }
            else:
                servers_data[key] = {'online': False, 'error': 'Нет данных'}
        except Exception as e:
            servers_data[key] = {'online': False, 'error': str(e)}

    # --- Сохранение истории (раз в минуту) ---
    try:
        last_metric = ServerMetric.objects.order_by('-timestamp').first()
        if not last_metric or (now - last_metric.timestamp).total_seconds() > 60:
            for key, data in servers_data.items():
                if data['online']:
                    ServerMetric.objects.create(
                        server=key, cpu=data['cpu'], mem_percent=data['mem_percent'],
                        net_up=data['net_up'], net_down=data['net_down'], onlines=data['onlines']
                    )
            ServerMetric.objects.filter(timestamp__lt=now - timedelta(days=7)).delete()
    except Exception:
        pass

    # --- Ответ для Live режима ---
    if time_range == 'live':
        return JsonResponse(servers_data)

    # --- Ответ для истории ---
    delta_map = {'1m': 1, '10m': 10, '1h': 60, '24h': 1440, '7d': 10080}
    minutes = delta_map.get(time_range, 1)
    start_time = now - timedelta(minutes=minutes)

    history_data = {'server1': [], 'server2': []}
    try:
        metrics = ServerMetric.objects.filter(timestamp__gte=start_time).order_by('timestamp')
        for m in metrics:
            label = m.timestamp.strftime('%H:%M:%S') if time_range in ['1m', '10m', '1h'] else m.timestamp.strftime(
                '%d.%m %H:%M')
            history_data[m.server].append({
                'time': label, 'up': m.net_up, 'down': m.net_down, 'onlines': m.onlines
            })
    except Exception:
        pass

    return JsonResponse(history_data)


@require_GET
def api_ping_servers(request):
    results = {}
    for key, url in [('server1', settings.SERVER_1_API_URL), ('server2', settings.SERVER_2_API_URL)]:
        try:
            start = time.time()
            requests.get(url, timeout=3, verify=False)
            ping = int((time.time() - start) * 1000)
            results[key] = {'online': True, 'ping': ping}
        except Exception:
            results[key] = {'online': False, 'ping': 0}
    return JsonResponse(results)


# ... (ваши импорты в начале файла) ...

# ---------- АДМИН-ПАНЕЛЬ И API ----------

@staff_member_required
def admin_dashboard(request):
    """Рендерим единую админ-панель (HTML)."""
    return render(request, 'admin_dashboard.html')


@staff_member_required
@require_GET
def api_admin_traffic(request):
    """Возвращает сырые данные о трафике (байты) для графиков."""
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
    """Возвращает всех пользователей с их статусом и подписками."""
    # 1. Собираем онлайн-email'ы с обоих серверов
    online_emails = set()
    for key in ['server1', 'server2']:
        try:
            client = get_client_for_server(key)
            emails = client.get_online_users()
            online_emails.update(emails)
        except Exception:
            pass

    # 2. Формируем список пользователей
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
    """Единый эндпоинт для обработки действий (продление, удаление, смена пароля)."""
    try:
        data = json.loads(request.body)
    except Exception:
        return JsonResponse({'success': False, 'msg': 'Неверный формат данных'}, status=400)

    action = data.get('action')
    user_id = data.get('user_id')
    sub_id = data.get('sub_id')

    user_obj = User.objects.filter(id=user_id).first() if user_id else None

    # --- 1. Смена пароля ---
    if action == 'change_password':
        password = data.get('password', '')
        if len(password) < 6:
            return JsonResponse({'success': False, 'msg': 'Пароль слишком короткий'})
        if user_obj:
            user_obj.set_password(password)
            user_obj.save()
            return JsonResponse({'success': True, 'msg': 'Пароль успешно изменён'})
        return JsonResponse({'success': False, 'msg': 'Пользователь не найден'})

    # --- 2. Создание подписки ---
    if action == 'create_sub':
        if not user_obj:
            return JsonResponse({'success': False, 'msg': 'Пользователь не найден'})

        server_key = data.get('server')
        protocol = data.get('protocol')

        if ServerProfile.objects.filter(user=user_obj, server=server_key, protocol=protocol).exists():
            return JsonResponse({'success': False, 'msg': 'Такая подписка уже есть'})

        email = f'{user_obj.username}_{server_key}_{protocol}@vpn.local'
        vpn_uuid = str(uuid4())

        try:
            client = get_client_for_server(server_key)
            result = client.add_client(email, vpn_uuid)
        except Exception as e:
            return JsonResponse({'success': False, 'msg': f'Ошибка API: {e}'})

        admin_cfg = AdminSettings.load()
        ServerProfile.objects.create(
            user=user_obj, server=server_key, protocol=protocol,
            vpn_email=email, vpn_uuid=result['uuid'],
            vpn_inbound_id=(settings.SERVER_1_INBOUND_ID if server_key == 'server1' else settings.SERVER_2_INBOUND_ID),
            vpn_sub_id=result['sub_id'],
            subscription_expiry=timezone.now() + timedelta(days=admin_cfg.default_days),
            total_gb=admin_cfg.default_traffic_gb,
        )
        return JsonResponse({'success': True, 'msg': 'Подписка создана'})

    # Для действий ниже нужна подписка
    profile = ServerProfile.objects.filter(id=sub_id).first()
    if not profile:
        return JsonResponse({'success': False, 'msg': 'Подписка не найдена'})

    # --- 3. Продление / Сокращение ---
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
                email=profile.vpn_email,
                uuid=str(profile.vpn_uuid),
                sub_id=profile.vpn_sub_id,
                expiry_time=new_expiry,
                enable=(new_expiry > timezone.now()),
                total_gb=profile.total_gb,
            )

            profile.subscription_expiry = new_expiry
            profile.save()
            return JsonResponse({'success': True, 'msg': f'Срок изменён на {days} дн.'})
        except Exception as e:
            # Возвращаем реальный текст ошибки, чтобы увидеть её в тосте
            return JsonResponse({'success': False, 'msg': f'Ошибка API: {e}'})
    # --- 4. Удаление ---
    if action == 'delete_sub':
        try:
            client = get_client_for_server(profile.server)
            client.delete_client(profile.vpn_email)
            profile.delete()
            return JsonResponse({'success': True, 'msg': 'Подписка удалена'})
        except Exception as e:
            return JsonResponse({'success': False, 'msg': f'Ошибка API: {e}'})

    return JsonResponse({'success': False, 'msg': 'Неизвестное действие'})