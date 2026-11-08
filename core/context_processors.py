from .models import AdminSettings

def global_settings(request):
    try:
        settings = AdminSettings.load()
        return {'footer_text': settings.footer_text}
    except Exception:
        return {'footer_text': 'VPN Service'}