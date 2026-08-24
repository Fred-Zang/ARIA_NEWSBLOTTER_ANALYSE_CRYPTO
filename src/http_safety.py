"""Primitives HTTP avec contrôles SSRF, redirections bornées et taille limitée."""
from __future__ import annotations

import ipaddress
import socket
from typing import Any
from urllib.parse import urljoin, urlparse

import requests

from src.config import (
    ALLOW_PRIVATE_API_HOSTS,
    ARIA_API_BASE_URL,
    HTTP_TIMEOUT_SECONDS,
    MAX_HTTP_RESPONSE_BYTES,
    MAX_REDIRECTS,
)


class UnsafeUrlError(ValueError):
    """URL refusée par la politique réseau du worker."""


def _is_public_ip(value: str) -> bool:
    ip = ipaddress.ip_address(value)
    return ip.is_global


def validate_https_url(url: str, *, allow_private: bool = False) -> str:
    """Valide le schéma et, par défaut, refuse les destinations non publiques."""
    parsed = urlparse(url)
    if parsed.scheme.lower() != "https":
        raise UnsafeUrlError("Seules les URL HTTPS sont autorisées")
    if not parsed.hostname or parsed.username or parsed.password:
        raise UnsafeUrlError("URL invalide ou credentials intégrés interdits")
    if parsed.port not in (None, 443):
        raise UnsafeUrlError("Les ports HTTPS non standards sont refusés par défaut")

    if allow_private:
        return url

    try:
        infos = socket.getaddrinfo(parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise UnsafeUrlError(f"Impossible de résoudre l'hôte : {parsed.hostname}") from exc

    addresses = {info[4][0] for info in infos}
    if not addresses or any(not _is_public_ip(address) for address in addresses):
        raise UnsafeUrlError("Destination privée, locale ou réservée refusée")
    return url


def safe_request(
    method: str,
    url: str,
    *,
    allow_private: bool | None = None,
    timeout: float | None = None,
    max_bytes: int | None = None,
    max_redirects: int | None = None,
    **kwargs: Any,
) -> requests.Response:
    """Exécute une requête bornée et revalide chaque redirection.

    Une destination privée n'est autorisée que pour l'hôte exact de
    ``ARIA_API_BASE_URL`` lorsque cette option est explicitement activée.
    Les credentials HTTP sont supprimés en cas de redirection cross-origin.
    """
    api_host = urlparse(ARIA_API_BASE_URL).hostname
    request_host = urlparse(url).hostname
    if allow_private is None:
        allow_private = bool(ALLOW_PRIVATE_API_HOSTS and request_host and request_host == api_host)
    private_allowed_host = api_host if allow_private else None

    def validate_target(candidate: str) -> str:
        candidate_host = urlparse(candidate).hostname
        target_is_allowed_private = bool(private_allowed_host and candidate_host == private_allowed_host)
        return validate_https_url(candidate, allow_private=target_is_allowed_private)

    timeout = HTTP_TIMEOUT_SECONDS if timeout is None else timeout
    max_bytes = MAX_HTTP_RESPONSE_BYTES if max_bytes is None else max_bytes
    max_redirects = MAX_REDIRECTS if max_redirects is None else max_redirects

    current_url = validate_target(url)
    request_kwargs = dict(kwargs)
    request_kwargs.pop("allow_redirects", None)
    request_kwargs.pop("stream", None)

    with requests.Session() as session:
        for redirect_count in range(max_redirects + 1):
            response = session.request(
                method,
                current_url,
                timeout=timeout,
                allow_redirects=False,
                stream=True,
                **request_kwargs,
            )

            if response.is_redirect or response.is_permanent_redirect:
                status_code = response.status_code
                location = response.headers.get("Location")
                response.close()
                if not location:
                    raise requests.TooManyRedirects("Redirection sans en-tête Location")
                if redirect_count >= max_redirects:
                    raise requests.TooManyRedirects("Nombre maximal de redirections dépassé")

                next_url = validate_target(urljoin(current_url, location))
                previous_host = urlparse(current_url).hostname
                next_host = urlparse(next_url).hostname
                if previous_host != next_host:
                    headers = dict(request_kwargs.get("headers") or {})
                    sensitive_names = {
                        name
                        for name in headers
                        if name.lower() in {"authorization", "proxy-authorization", "cookie"}
                        or "api-key" in name.lower()
                    }
                    for name in sensitive_names:
                        headers.pop(name, None)
                    if headers:
                        request_kwargs["headers"] = headers
                    else:
                        request_kwargs.pop("headers", None)

                current_url = next_url
                # Les paramètres de requête appartiennent uniquement à la requête
                # initiale. Les rejouer après une redirection serait incorrect et
                # pourrait exposer un token transmis en query string.
                request_kwargs.pop("params", None)
                if status_code in {301, 302, 303} and method.upper() not in {"GET", "HEAD"}:
                    method = "GET"
                    request_kwargs.pop("data", None)
                    request_kwargs.pop("json", None)
                continue

            content = bytearray()
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                content.extend(chunk)
                if len(content) > max_bytes:
                    response.close()
                    raise requests.RequestException("Réponse HTTP supérieure à la limite autorisée")
            response._content = bytes(content)
            response._content_consumed = True
            return response

    raise requests.TooManyRedirects("Nombre maximal de redirections dépassé")


def safe_get(url: str, **kwargs: Any) -> requests.Response:
    return safe_request("GET", url, **kwargs)


def safe_post(url: str, **kwargs: Any) -> requests.Response:
    return safe_request("POST", url, **kwargs)


def safe_put(url: str, **kwargs: Any) -> requests.Response:
    return safe_request("PUT", url, **kwargs)


def safe_delete(url: str, **kwargs: Any) -> requests.Response:
    return safe_request("DELETE", url, **kwargs)
