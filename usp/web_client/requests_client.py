"""Implementation of :mod:`usp.web_client.abstract_client` with Requests."""

import logging
from http import HTTPStatus

import requests

from usp import __version__
from usp.disk_cache import DiskConditionalCache

from .abstract_client import (
    RETRYABLE_HTTP_STATUS_CODES,
    AbstractWebClient,
    AbstractWebClientResponse,
    AbstractWebClientSuccessResponse,
    RequestWaiter,
    WebClientErrorResponse,
)

log = logging.getLogger(__name__)

REDIRECT_STATUS_CODES = {301, 302, 303, 307, 308}
"""HTTP status codes that Requests follows as redirects."""


class RequestsWebClientSuccessResponse(AbstractWebClientSuccessResponse):
    """
    requests-based successful response.
    """

    __slots__ = [
        "__requests_response",
        "__max_response_data_length",
    ]

    def __init__(
        self,
        requests_response: requests.Response,
        max_response_data_length: int | None = None,
    ):
        """
        :param requests_response: Response data
        :param max_response_data_length: Maximum data length, or ``None`` to not restrict.
        """
        self.__requests_response = requests_response
        self.__max_response_data_length = max_response_data_length

    def status_code(self) -> int:
        return int(self.__requests_response.status_code)

    def status_message(self) -> str:
        message = self.__requests_response.reason
        if not message:
            message = HTTPStatus(self.status_code()).phrase
        return message

    def header(self, case_insensitive_name: str) -> str | None:
        return self.__requests_response.headers.get(case_insensitive_name.lower(), None)

    def raw_data(self) -> bytes:
        if self.__max_response_data_length:
            data = self.__requests_response.content[: self.__max_response_data_length]
        else:
            data = self.__requests_response.content

        return data

    def url(self) -> str:
        return self.__requests_response.url


class RequestsWebClientErrorResponse(WebClientErrorResponse):
    """
    Error response from the Requests parser.
    """

    pass


class RequestsWebClientRedirectResponse(AbstractWebClientResponse):
    """A redirect response returned when redirects are not followed automatically."""

    __slots__ = ["__requests_response"]

    def __init__(self, requests_response: requests.Response):
        self.__requests_response = requests_response

    def status_code(self) -> int:
        return int(self.__requests_response.status_code)

    def status_message(self) -> str:
        message = self.__requests_response.reason
        if not message:
            message = HTTPStatus(self.status_code()).phrase
        return message

    def header(self, case_insensitive_name: str) -> str | None:
        return self.__requests_response.headers.get(case_insensitive_name.lower(), None)

    def url(self) -> str:
        return self.__requests_response.url


class RequestsWebClientNotModifiedResponse(AbstractWebClientResponse):
    """A ``304 Not Modified`` response.

    It intentionally does not expose a body: the caller is expected to supply the
    body from its own cache.
    """

    __slots__ = ["__requests_response"]

    def __init__(self, requests_response: requests.Response):
        self.__requests_response = requests_response

    def status_code(self) -> int:
        return int(self.__requests_response.status_code)

    def status_message(self) -> str:
        message = self.__requests_response.reason
        if not message:
            message = HTTPStatus.NOT_MODIFIED.phrase
        return message

    def header(self, case_insensitive_name: str) -> str | None:
        return self.__requests_response.headers.get(case_insensitive_name.lower(), None)

    def url(self) -> str:
        return self.__requests_response.url


class RequestsWebClient(AbstractWebClient):
    """requests-based web client to be used by the sitemap fetcher."""

    __USER_AGENT = f"ultimate_sitemap_parser/{__version__}"

    __HTTP_REQUEST_TIMEOUT = (9.05, 60)
    """
    HTTP request timeout.

    Some webservers might be generating huge sitemaps on the fly, so this is why it's rather big.
    """

    __slots__ = [
        "__max_response_data_length",
        "__timeout",
        "__proxies",
        "__verify",
        "__waiter",
        "__cache",
    ]

    def __init__(
        self,
        verify=True,
        wait: float | None = None,
        random_wait: bool = False,
        session: requests.Session | None = None,
        cache: DiskConditionalCache | None = None,
        cache_dir: str | None = None,
        cache_namespace: str = "default",
    ):
        """
        :param verify: whether certificates should be verified for HTTPS requests.
        :param wait: time to wait between requests, in seconds.
        :param random_wait: if true, wait time is multiplied by a random number between 0.5 and 1.5.
        :param session: a custom session object to use, or None to create a new one.
        :param cache: an on-disk conditional cache to use for XML sitemaps, or None (the default) to disable caching.
        :param cache_dir: convenience alternative to ``cache``: create a
            :class:`~usp.disk_cache.DiskConditionalCache` in this directory.
        :param cache_namespace: cache namespace to use when ``cache_dir`` is given.
        """
        if cache is not None and cache_dir is not None:
            raise ValueError("Pass either cache or cache_dir, not both.")

        if cache_dir is not None:
            cache = DiskConditionalCache(directory=cache_dir, namespace=cache_namespace)

        if session is not None and cache is not None:
            log.info(
                "A custom requests session is in use while a USP disk cache is "
                "enabled; both caches will be active independently."
            )

        self.__max_response_data_length = None
        self.__timeout = self.__HTTP_REQUEST_TIMEOUT
        self.__proxies = {}
        self.__verify = verify
        self.__waiter = RequestWaiter(wait, random_wait)
        self.__session = session or requests.Session()
        self.__cache = cache

    @property
    def conditional_cache(self) -> DiskConditionalCache | None:
        """Return the conditional cache configured for this client, if any."""
        return self.__cache

    def set_timeout(self, timeout: float | tuple[float, float] | None) -> None:
        """Set HTTP request timeout.

        See also: `Requests timeout docs <https://requests.readthedocs.io/en/latest/user/advanced/#timeouts>`__

        :param timeout: An integer to use as both the connect and read timeouts,
            or a tuple to specify them individually, or None for no timeout
        """
        # Used mostly for testing
        self.__timeout = timeout

    def set_proxies(self, proxies: dict[str, str]) -> None:
        """
        Set a proxy for the request.

        :param proxies: Proxy definition where the keys are schemes ("http" or "https") and values are the proxy address.
            Example: ``{'http': 'http://user:pass@10.10.1.10:3128/'}, or an empty dict to disable proxy.``
        """
        # Used mostly for testing
        self.__proxies = proxies

    def set_max_response_data_length(self, max_response_data_length: int) -> None:
        self.__max_response_data_length = max_response_data_length

    def get(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        allow_redirects: bool = True,
        accept_not_modified: bool = False,
    ) -> AbstractWebClientResponse:
        """Fetch a URL.

        :param url: URL to fetch.
        :param headers: additional request headers, e.g. conditional request
            headers. They are sent only for this request.
        :param allow_redirects: whether Requests should follow redirects
            automatically. When false, redirects are returned as
            :class:`RequestsWebClientRedirectResponse` so that the caller can
            decide which validators to send for each hop.
        :param accept_not_modified: whether a ``304 Not Modified`` response should
            be returned as :class:`RequestsWebClientNotModifiedResponse` instead of
            being treated as an HTTP error.
        """
        self.__waiter.wait()

        request_headers = {"User-Agent": self.__USER_AGENT}
        if headers:
            request_headers.update(headers)

        try:
            response = self.__session.get(
                url,
                timeout=self.__timeout,
                stream=True,
                headers=request_headers,
                proxies=self.__proxies,
                verify=self.__verify,
                allow_redirects=allow_redirects,
            )
        except requests.exceptions.Timeout as ex:
            # Retryable timeouts
            return RequestsWebClientErrorResponse(message=str(ex), retryable=True)

        except requests.exceptions.RequestException as ex:
            # Other errors, e.g. redirect loops
            return RequestsWebClientErrorResponse(message=str(ex), retryable=False)

        else:
            if response.status_code in REDIRECT_STATUS_CODES and not allow_redirects:
                return RequestsWebClientRedirectResponse(requests_response=response)

            if response.status_code == 304 and accept_not_modified:
                return RequestsWebClientNotModifiedResponse(requests_response=response)

            if 200 <= response.status_code < 300:
                return RequestsWebClientSuccessResponse(
                    requests_response=response,
                    max_response_data_length=self.__max_response_data_length,
                )
            else:
                message = f"{response.status_code} {response.reason}"
                log.debug(f"Response content: {response.text}")

                if response.status_code in RETRYABLE_HTTP_STATUS_CODES:
                    return RequestsWebClientErrorResponse(
                        message=message, retryable=True
                    )
                else:
                    return RequestsWebClientErrorResponse(
                        message=message, retryable=False
                    )
