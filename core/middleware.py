from django.shortcuts import redirect


class ForcePasswordChange:
    """Users created with the default password must choose their own on first login."""
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        u = request.user
        if u.is_authenticated and u.must_change_password and request.path not in ("/password/", "/logout/"):
            return redirect("password")
        return self.get_response(request)
