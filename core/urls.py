from django.urls import path
from django.contrib.auth import views as auth_views
from . import views

urlpatterns = [
    # Auth
    path('signup/', views.SignUpView.as_view(), name='signup'),
    path('login/', auth_views.LoginView.as_view(template_name='login.html'), name='login'),
    path('logout/', views.logout_view, name='logout'),

    # Личный кабинет
    path('dashboard/', views.dashboard, name='dashboard'),
    path('create-profile/', views.create_profile, name='create_profile'),
    path('qr/<int:profile_id>/', views.qr_code, name='qr_code'),
    path('link/<int:profile_id>/', views.subscription_link, name='subscription_link'),
    path('server-status/', views.server_status, name='server_status'),
    path('api/ping/', views.api_ping_servers, name='api_ping_servers'),

    # Админка
    path('admin-panel/', views.admin_dashboard, name='admin_dashboard'),
    path('admin-panel/settings/', views.admin_settings, name='admin_settings'),

    # API для админки (единая панель)
    path('api/admin/traffic/', views.api_admin_traffic, name='api_admin_traffic'),
    path('api/admin/users/', views.api_admin_users, name='api_admin_users'),
    path('api/admin/action/', views.api_admin_action, name='api_admin_action'),
]
