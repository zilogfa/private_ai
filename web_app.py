from app import create_app

from app.config import (
    WEB_DEBUG,
    WEB_HOST,
    WEB_PORT,
)


app = create_app()


if __name__ == "__main__":
    print(
        "\nPrivate AI web started."
    )

    print(
        f"Open: "
        f"http://{WEB_HOST}:{WEB_PORT}"
    )

    print(
        "Press Ctrl+C to stop.\n"
    )

    app.run(
        host=WEB_HOST,
        port=WEB_PORT,
        debug=WEB_DEBUG,
        threaded=True,
    )
