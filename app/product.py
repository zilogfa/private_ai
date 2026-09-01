import os


COMPANY_NAME = (
    os.environ.get(
        "BEGLOO_COMPANY_NAME",
        "BEGLOO",
    )
    .strip()
    or "BEGLOO"
)

PRODUCT_NAME = (
    os.environ.get(
        "BEGLOO_PRODUCT_NAME",
        "ATLAS",
    )
    .strip()
    or "ATLAS"
)

PRODUCT_MARK = (
    os.environ.get(
        "BEGLOO_PRODUCT_MARK",
        PRODUCT_NAME[:1] or "A",
    )
    .strip()
    or "A"
)[:2]

PRODUCT_SIGNATURE = (
    f"{PRODUCT_NAME} by {COMPANY_NAME}"
)

PRODUCT = {
    "company": COMPANY_NAME,
    "name": PRODUCT_NAME,
    "mark": PRODUCT_MARK,
    "signature": PRODUCT_SIGNATURE,
}
