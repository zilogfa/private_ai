import html

import bleach
import mistune

from pygments import (
    highlight as pygments_highlight,
)

from pygments.formatters import (
    HtmlFormatter,
)

from pygments.lexers import (
    TextLexer,
    get_lexer_by_name,
)

from pygments.util import (
    ClassNotFound,
)


# =========================================================
# MARKDOWN RENDERER
# =========================================================

ALLOWED_TAGS = [
    "p",
    "br",
    "strong",
    "em",
    "del",
    "ul",
    "ol",
    "li",
    "blockquote",
    "hr",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "a",
    "img",
    "table",
    "thead",
    "tbody",
    "tr",
    "th",
    "td",
    "div",
    "span",
    "pre",
    "code",
    "button",
    "input",
]

ALLOWED_ATTRIBUTES = {
    "a": [
        "href",
        "title",
    ],

    "img": [
        "src",
        "alt",
        "title",
        "loading",
    ],

    "div": [
        "class",
        "data-language",
    ],

    "span": [
        "class",
    ],

    "code": [
        "class",
    ],

    "button": [
        "type",
        "class",
    ],

    "input": [
        "type",
        "checked",
        "disabled",
        "class",
    ],

    "th": [
        "align",
    ],

    "td": [
        "align",
    ],
}

ALLOWED_PROTOCOLS = [
    "http",
    "https",
    "mailto",
]


class PrivateAIMarkdownRenderer(
    mistune.HTMLRenderer
):
    """
    Safe HTML renderer with local Pygments code highlighting.

    Raw HTML from model output is escaped by Mistune.
    The final result is sanitized again with Bleach.

    Image Markdown is deliberately restricted to Private AI's authenticated
    generated-image route. This prevents model output from silently embedding
    arbitrary remote tracking images in the browser.
    """

    def __init__(self):
        super().__init__(
            escape=True,
            allow_harmful_protocols=False,
        )

        self.code_formatter = (
            HtmlFormatter(
                nowrap=True
            )
        )

    def block_code(
        self,
        code,
        info=None,
    ):
        language = ""

        if info:
            language = (
                str(info)
                .strip()
                .split(
                    None,
                    1,
                )[0]
                .lower()
            )

        try:
            lexer = (
                get_lexer_by_name(
                    language
                )
                if language
                else TextLexer()
            )

        except ClassNotFound:
            lexer = TextLexer()

        highlighted = (
            pygments_highlight(
                code,
                lexer,
                self.code_formatter,
            )
        )

        display_language = (
            language
            or "code"
        )

        safe_language = (
            html.escape(
                language,
                quote=True,
            )
        )

        safe_display_language = (
            html.escape(
                display_language
            )
        )

        return (
            '<div '
            'class="code-block" '
            f'data-language="{safe_language}">'
            '<div class="code-toolbar">'
            '<span class="code-language">'
            f'{safe_display_language}'
            '</span>'
            '<button '
            'type="button" '
            'class="code-copy-button">'
            'Copy'
            '</button>'
            '</div>'
            '<pre>'
            '<code class="code-content">'
            f'{highlighted}'
            '</code>'
            '</pre>'
            '</div>\n'
        )

    def image(
        self,
        text,
        url,
        title=None,
    ):
        url = str(
            url or ""
        ).strip()

        # Only authenticated local generated images are renderable. External
        # image URLs remain plain alt text rather than causing a browser fetch.
        if not url.startswith(
            "/api/images/"
        ):
            return html.escape(
                str(text or "Image")
            )

        safe_url = html.escape(
            url,
            quote=True,
        )
        safe_alt = html.escape(
            str(text or "Generated image"),
            quote=True,
        )

        title_attribute = ""

        if title:
            safe_title = html.escape(
                str(title),
                quote=True,
            )
            title_attribute = (
                f' title="{safe_title}"'
            )

        return (
            f'<img src="{safe_url}" '
            f'alt="{safe_alt}" '
            f'loading="lazy"'
            f'{title_attribute}>'
        )


_renderer = (
    PrivateAIMarkdownRenderer()
)

_markdown = mistune.create_markdown(
    renderer=_renderer,
    plugins=[
        "strikethrough",
        "table",
        "task_lists",
        "url",
    ],
)


# =========================================================
# PUBLIC RENDER FUNCTION
# =========================================================


def render_markdown(
    content,
):
    """
    Convert raw assistant Markdown to sanitized HTML.

    Raw Markdown remains the database source of truth.
    HTML is generated only for presentation.
    """

    raw_text = str(
        content or ""
    )

    if not raw_text:
        return ""

    try:
        rendered = _markdown(
            raw_text
        )

        return bleach.clean(
            rendered,
            tags=ALLOWED_TAGS,
            attributes=
                ALLOWED_ATTRIBUTES,
            protocols=
                ALLOWED_PROTOCOLS,
            strip=True,
        )

    except Exception:
        escaped = html.escape(
            raw_text
        )

        escaped = (
            escaped
            .replace(
                "\r\n",
                "\n",
            )
            .replace(
                "\r",
                "\n",
            )
            .replace(
                "\n",
                "<br>",
            )
        )

        return (
            f"<p>{escaped}</p>"
        )
