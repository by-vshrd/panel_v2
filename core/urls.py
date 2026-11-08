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

    # Админ
    path('admin-panel/', views.admin_dashboard, name='admin_dashboard'),
    path('admin-panel/user/<int:user_id>/', views.manage_user, name='manage_user'),
    path('admin-panel/user/<int:user_id>/extend/', views.manual_extend, name='manual_extend'),
    path('admin-panel/settings/', views.admin_settings, name='admin_settings'),
    path('admin-panel/statistics/', views.admin_statistics, name='admin_statistics'),
    path('admin-panel/statistics/', views.admin_statistics, name='admin_statistics'),
    path('api/stats/', views.api_get_stats, name='api_get_stats'),
    path('api/ping/', views.api_ping_servers, name='api_ping_servers'),
]