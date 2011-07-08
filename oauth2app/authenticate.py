#-*- coding: utf-8 -*-


"""OAuth 2.0 Authentication"""


from urlparse import parse_qsl
from simplejson import dumps
from django.conf import settings
from django.http import HttpResponse
from .exceptions import OAuth2Exception
from .models import AccessToken, AccessRange, TimestampGenerator
from .consts import REALM, AUTHENTICATION_METHOD, MAC, BEARER


class AuthenticationException(OAuth2Exception):
    """Authentication exception base class."""
    pass


class InvalidRequest(AuthenticationException):
    """The request is missing a required parameter, includes an
    unsupported parameter or parameter value, repeats the same
    parameter, uses more than one method for including an access
    token, or is otherwise malformed."""
    error = 'invalid_request'


class InvalidToken(AuthenticationException):
    """The access token provided is expired, revoked, malformed, or
    invalid for other reasons."""
    error = 'invalid_token'


class InsufficientScope(AuthenticationException):
    """The request requires higher privileges than provided by the
    access token."""
    error = 'insufficient_scope'


class Authenticator(object):
    """Django HttpRequest authenticator. Checks a request for valid
    credentials and scope.

    **Args:**

    * *request:* Django HttpRequest object.

    **Kwargs:**

    * *scope:* A iterable of oauth2app.models.AccessRange objects.

    * *authentication_method:* Accepted authentication methods. Possible
      values are: oauth2app.consts.MAC, oauth2app.consts.BEARER, 
      oauth2app.consts.MAC | oauth2app.consts.BEARER
    """

    valid = False
    access_token = None
    auth_type = None
    auth_value = None
    error = None
    attempted_validation = False

    def __init__(
            self, 
            request, 
            scope=None, 
            authentication_method=AUTHENTICATION_METHOD):
        if authentication_method not in [BEARER, MAC, BEARER | MAC]:
            raise OAuth2Exception("Possible values for authentication_method" 
                " are oauth2app.consts.MAC, oauth2app.consts.BEARER, "
                "oauth2app.consts.MAC | oauth2app.consts.BEARER")
        self.authentication_method = authentication_method
        if scope is None:
            self.authorized_scope = None
        elif isinstance(scope, AccessRange):
            self.authorized_scope = set([scope.key])
        else:
            self.authorized_scope = set([x.key for x in scope])
        self.request = request
        self.bearer_token = request.REQUEST.get('bearer_token')
        if "HTTP_AUTHORIZATION" in self.request.META:
            auth = self.request.META["HTTP_AUTHORIZATION"].split()
            self.auth_type = auth[0].lower()
            self.auth_value = " ".join(auth[1:]).strip()

    def validate(self):
        """Validate the request. Raises an AuthenticationException if the
        request fails authentication.

        *Returns None*"""
        try:
            self._validate()
        except AuthenticationException, e:
            self.error = e
            raise e
        self.valid = True

    def _validate(self):
        """Validate the request."""
        # Check for Bearer or Mac authorization
        if self.auth_type in ["bearer", "mac"]:
            self.attempted_validation = True
            if self.auth_type == "bearer":
                self._validate_bearer(self.auth_value)
            elif self.auth_type == "mac":
                self._validate_mac(self.auth_value)
            self.valid = True
        # Check for posted/paramaterized bearer token.
        elif self.bearer_token is not None:
            self.attempted_validation = True
            self._validate_bearer(self.bearer_token)
            self.valid = True
            return
        else:
            raise InvalidRequest("Request authentication failed, no "
                "authentication credentials provided.")
        if self.authorized_scope is not None:
            token_scope = set([x.key for x in self.access_token.scope.all()])
            new_scope = self.authorized_scope - token_scope
            if len(new_scope) > 0:
                raise InsufficientScope(("Access token has insufficient"
                    "scope: %s") % list(self.authorized_scope))
        now = TimestampGenerator()()
        if self.access_token.expire < now:
            raise InvalidToken("Token is expired")

    def _validate_bearer(self, token):
        """Validate Bearer token."""
        if self.authentication_method & BEARER == 0:
            raise InvalidToken("Bearer authentication is not supported.")
        try:
            self.access_token = AccessToken.objects.get(token=token)
        except AccessToken.DoesNotExist:
            raise InvalidToken("Token doesn't exist")

    def _validate_mac(self, auth):
        """Validate MAC authentication. Not implemented."""
        if self.authentication_method & MAC == 0:
            raise InvalidToken("MAC authentication is not supported.")
        auth = parse_qsl(auth.replace(",","&").replace('"', ''))
        auth = dict([(x[0].strip(), x[1].strip()) for x in auth])
        raise NotImplementedError()

    def _get_user(self):
        """The user associated with the valid access token.

        *django.auth.User object*"""
        if not self.valid:
            self.validate()
        return self.access_token.user

    user = property(_get_user)

    def _get_scope(self):
        """The client scope associated with the valid access token.

        *QuerySet of AccessRange objects.*"""
        if not self.valid:
            self.validate()
        return self.access_token.scope.all()

    scope = property(_get_scope)

    def _get_client(self):
        """The client associated with the valid access token.

        *oauth2app.models.Client object*"""
        if not self.valid:
            self.validate()
        return self.access_token.client

    client = property(_get_client)

    def error_response(self,
            content='',
            mimetype=None,
            content_type=settings.DEFAULT_CONTENT_TYPE):
        """Error response generator. Returns a Django HttpResponse with status
        401 and the approproate headers set. See Django documentation for details.
        https://docs.djangoproject.com/en/dev/ref/request-response/#django.http.HttpResponse.__init__

        **Kwargs:**

        * *content:* See Django docs. *Default ''*
        * *mimetype:* See Django docs. *Default None*
        * *content_type:* See Django docs. *Default DEFAULT_CONTENT_TYPE*

        """
        response = HttpResponse(
            content=content,
            mimetype=mimetype,
            content_type=content_type)
        if not self.attempted_validation:
            response['WWW-Authenticate'] = 'Bearer realm="%s"' % REALM
            response.status_code = 401
            return response
        else:
            if self.error is not None:
                error = getattr(self.error, "error", "invalid_request")
                error_description = self.error.message
            else:
                error = "invalid_request"
                error_description = "Invalid Request."
            header = [
                'Bearer realm="%s"' % REALM,
                'error="%s"' % error,
                'error_description="%s"' % error_description]
            if isinstance(self.error, InsufficientScope):
                header.append('scope=%s' % ' '.join(self.authorized_scope))
                response.status_code = 403
            elif isinstance(self.error, InvalidToken):
                response.status_code = 401
            elif isinstance(self.error, InvalidRequest):
                response.status_code = 400
            else:
                response.status_code = 401
            response['WWW-Authenticate'] = ', '.join(header)
            return response


class JSONAuthenticator(Authenticator):
    """Wraps Authenticator, adds support for a callback parameter and
    JSON related. convenience methods.

    **Args:**

    * *request:* Django HttpRequest object.

    **Kwargs:**

    * *scope:* A iterable of oauth2app.models.AccessRange objects.
    """
    def __init__(self, request, scope=None):
        Authenticator.__init__(self, request, scope=scope)
        self.callback = request.REQUEST.get('callback')

    def response(self, data):
        """Returns a HttpResponse object of JSON serialized data.

        **Args:**

        * *data:* Object to be JSON serialized and returned.
        """
        json_data = dumps(data)
        if self.callback is not None:
            json_data = "%s(%s);" % (self.callback, json_data)
        response = HttpResponse(
            content=json_data,
            content_type='application/json')
        return response

    def error_response(self):
        """Returns a HttpResponse object of JSON error data."""
        if self.error is not None:
            content = dumps({
                "error":getattr(self.error, "error", "invalid_request"),
                "error_description":self.error.message})
        else:
            content = ({
                "error":"invalid_request",
                "error_description":"Invalid Request."})
        if self.callback is not None:
            content = "%s(%s);" % (self.callback, content)
        response = Authenticator.error_response(
            self,
            content=content,
            content_type='application/json')
        if self.callback is not None:
            response.status_code = 200
        return response
