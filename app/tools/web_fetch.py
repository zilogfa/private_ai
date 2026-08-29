import ipaddress
import socket

from urllib.parse import (
    urljoin,
    urlparse,
)

import requests
from bs4 import BeautifulSoup

import app.config as config


class WebFetchError(Exception):
    pass


BLOCKED_TAGS = {
    "script",
    "style",
    "noscript",
    "svg",
    "canvas",
    "iframe",
    "form",
    "button",
    "nav",
    "footer",
    "header",
}

TEXT_BLOCK_TAGS = (
    "h1",
    "h2",
    "h3",
    "h4",
    "p",
    "li",
    "blockquote",
    "pre",
    "td",
    "th",
)


def _validate_public_url(url):
    url = str(
        url or ""
    ).strip()

    parsed = urlparse(url)

    if (
        parsed.scheme
        not in {"http", "https"}
        or not parsed.hostname
    ):
        raise WebFetchError(
            "Only public http/https URLs are supported."
        )

    hostname = parsed.hostname.lower()

    if hostname in {
        "localhost",
        "localhost.localdomain",
    }:
        raise WebFetchError(
            "Local/private URLs are blocked."
        )

    try:
        addresses = socket.getaddrinfo(
            hostname,
            parsed.port
            or (
                443
                if parsed.scheme == "https"
                else 80
            ),
            type=socket.SOCK_STREAM,
        )

    except socket.gaierror as error:
        raise WebFetchError(
            f"Could not resolve {hostname}."
        ) from error

    if not addresses:
        raise WebFetchError(
            f"Could not resolve {hostname}."
        )

    for address in addresses:
        ip_text = address[4][0]

        try:
            ip = ipaddress.ip_address(
                ip_text
            )

        except ValueError:
            continue

        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            raise WebFetchError(
                "Local/private network URLs are blocked."
            )

    return url


def _decode_response_bytes(
    response,
    data,
):
    encoding = (
        response.encoding
        or "utf-8"
    )

    try:
        return data.decode(
            encoding,
            errors="replace",
        )

    except LookupError:
        return data.decode(
            "utf-8",
            errors="replace",
        )


def _read_limited_bytes(response):
    max_bytes = int(
        config.WEB_FETCH_MAX_BYTES
    )

    chunks = []
    total = 0

    for chunk in response.iter_content(
        chunk_size=65536
    ):
        if not chunk:
            continue

        total += len(chunk)

        if total > max_bytes:
            remaining = (
                max_bytes
                - (
                    total
                    - len(chunk)
                )
            )

            if remaining > 0:
                chunks.append(
                    chunk[:remaining]
                )

            break

        chunks.append(chunk)

    return b"".join(chunks)


def _clean_text(value):
    return " ".join(
        str(value or "")
        .replace("\xa0", " ")
        .split()
    ).strip()


def _extract_html(
    html,
    url,
):
    soup = BeautifulSoup(
        html,
        "html.parser",
    )

    for tag_name in BLOCKED_TAGS:
        for tag in soup.find_all(
            tag_name
        ):
            tag.decompose()

    title = ""

    if soup.title:
        title = _clean_text(
            soup.title.get_text(
                " ",
                strip=True,
            )
        )

    container = (
        soup.find("article")
        or soup.find("main")
        or soup.body
        or soup
    )

    lines = []
    seen = set()

    for block in container.find_all(
        TEXT_BLOCK_TAGS
    ):
        text = _clean_text(
            block.get_text(
                " ",
                strip=True,
            )
        )

        if (
            len(text) < 25
            or text in seen
        ):
            continue

        seen.add(text)
        lines.append(text)

    content = "\n\n".join(
        lines
    )

    if not content:
        content = _clean_text(
            container.get_text(
                " ",
                strip=True,
            )
        )

    max_chars = int(
        config
        .WEB_FETCH_MAX_CHARS_PER_SOURCE
    )

    if len(content) > max_chars:
        content = (
            content[:max_chars]
            .rsplit(" ", 1)[0]
            .strip()
            + "\n[page text truncated]"
        )

    if not title:
        title = (
            urlparse(url).netloc
            or "Web source"
        )

    return title, content


def fetch_page(
    url,
    max_redirects=4,
):
    current_url = _validate_public_url(
        url
    )

    session = requests.Session()

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0 Safari/537.36 PrivateAI/1.3"
        ),
        "Accept": (
            "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.5"
        ),
    }

    response = None

    try:
        for _ in range(
            max_redirects + 1
        ):
            response = session.get(
                current_url,
                headers=headers,
                stream=True,
                allow_redirects=False,
                timeout=(
                    config
                    .WEB_FETCH_TIMEOUT_SECONDS
                ),
            )

            if response.status_code in {
                301,
                302,
                303,
                307,
                308,
            }:
                location = response.headers.get(
                    "Location"
                )

                response.close()

                if not location:
                    raise WebFetchError(
                        "Web page redirected without a destination."
                    )

                current_url = (
                    _validate_public_url(
                        urljoin(
                            current_url,
                            location,
                        )
                    )
                )
                continue

            break

        else:
            raise WebFetchError(
                "Too many redirects."
            )

        if response is None:
            raise WebFetchError(
                "No response received."
            )

        if not response.ok:
            raise WebFetchError(
                "Web page returned HTTP "
                f"{response.status_code}."
            )

        content_type = (
            response.headers.get(
                "Content-Type",
                "",
            )
            .split(";", 1)[0]
            .strip()
            .lower()
        )

        data = _read_limited_bytes(
            response
        )

        text = _decode_response_bytes(
            response,
            data,
        )

        if (
            content_type.startswith(
                "text/html"
            )
            or content_type
            in {
                "application/xhtml+xml",
                "",
            }
        ):
            title, content = (
                _extract_html(
                    text,
                    current_url,
                )
            )

        elif content_type.startswith(
            "text/"
        ):
            title = (
                urlparse(
                    current_url
                ).netloc
                or "Web source"
            )

            content = text[:(
                config
                .WEB_FETCH_MAX_CHARS_PER_SOURCE
            )].strip()

        else:
            raise WebFetchError(
                "This URL is not an HTML/text page yet."
            )

        return {
            "url": current_url,
            "title": title,
            "content": content,
            "content_type": content_type,
        }

    except requests.exceptions.ConnectionError as error:
        raise WebFetchError(
            "Could not connect to that web page."
        ) from error

    except requests.exceptions.Timeout as error:
        raise WebFetchError(
            "Web page request timed out."
        ) from error

    except requests.exceptions.RequestException as error:
        raise WebFetchError(
            f"Web page request failed: {error}"
        ) from error

    finally:
        if response is not None:
            response.close()

        session.close()
