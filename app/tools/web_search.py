from urllib.parse import urlparse

import requests

import app.config as config


class WebSearchError(Exception):
    pass


class SearchProvider:
    name = "base"

    def search(
        self,
        query,
        limit=None,
    ):
        raise NotImplementedError


class SearXNGSearchProvider(
    SearchProvider
):
    name = "searxng"

    def __init__(
        self,
        base_url=None,
    ):
        self.base_url = (
            base_url
            or config.SEARXNG_BASE_URL
        ).rstrip("/")

    def search(
        self,
        query,
        limit=None,
    ):
        query = str(
            query or ""
        ).strip()

        if not query:
            raise WebSearchError(
                "Search query is empty."
            )

        limit = int(
            limit
            or config.WEB_SEARCH_RESULT_LIMIT
        )

        try:
            response = requests.get(
                f"{self.base_url}/search",
                params={
                    "q": query,
                    "format": "json",
                    "categories": "general",
                    "safesearch":
                        config.WEB_SAFE_SEARCH,
                },
                headers={
                    "Accept": "application/json",
                    "User-Agent": (
                        "PrivateAI/1.3 local-search-client"
                    ),
                },
                timeout=(
                    config
                    .WEB_SEARCH_TIMEOUT_SECONDS
                ),
            )

        except requests.exceptions.ConnectionError as error:
            raise WebSearchError(
                "Could not connect to local SearXNG at "
                f"{self.base_url}."
            ) from error

        except requests.exceptions.RequestException as error:
            raise WebSearchError(
                f"SearXNG request failed: {error}"
            ) from error

        if response.status_code == 403:
            raise WebSearchError(
                "SearXNG rejected JSON output. Make sure "
                "'json' is enabled under search.formats "
                "in settings.yml."
            )

        if not response.ok:
            detail = (
                response.text
                or response.reason
                or "request failed"
            ).strip()

            raise WebSearchError(
                "SearXNG returned HTTP "
                f"{response.status_code}: "
                f"{detail[:300]}"
            )

        try:
            data = response.json()

        except ValueError as error:
            raise WebSearchError(
                "SearXNG returned invalid JSON."
            ) from error

        results = []
        seen_urls = set()

        for raw in (
            data.get("results")
            or []
        ):
            url = str(
                raw.get("url")
                or ""
            ).strip()

            parsed = urlparse(url)

            if (
                parsed.scheme
                not in {"http", "https"}
                or not parsed.netloc
                or url in seen_urls
            ):
                continue

            seen_urls.add(url)

            title = str(
                raw.get("title")
                or parsed.netloc
            ).strip()

            snippet = str(
                raw.get("content")
                or ""
            ).strip()

            published_at = (
                raw.get("publishedDate")
                or raw.get("published_date")
            )

            engines = raw.get(
                "engines"
            )

            if not engines:
                engine = raw.get(
                    "engine"
                )
                engines = (
                    [engine]
                    if engine
                    else []
                )

            results.append({
                "title": title,
                "url": url,
                "snippet": snippet,
                "published_at":
                    published_at,
                "engines": engines,
            })

            if len(results) >= limit:
                break

        return results


def get_search_provider(
    provider_name=None,
):
    name = str(
        provider_name
        or config.WEB_SEARCH_PROVIDER
        or "searxng"
    ).strip().lower()

    if name == "searxng":
        return SearXNGSearchProvider()

    raise WebSearchError(
        "Unsupported web search provider: "
        f"{name}"
    )


def search_web(
    query,
    limit=None,
):
    provider = get_search_provider()

    return provider.search(
        query,
        limit=limit,
    )
