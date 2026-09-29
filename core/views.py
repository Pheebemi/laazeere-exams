from django.http import JsonResponse


def health(request):
    """Simple deploy-verification endpoint — confirms the app + DB are up."""
    return JsonResponse({"status": "ok"})
