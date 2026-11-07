"""Content hashes of the starter definitions shipped in each release.

GENERATED FILE — do not edit by hand. Regenerate with:

    uv run python scripts/gen_starters.py

RELEASED_STARTER_HASHES maps a starter filename to the sha256 of every
version of it shipped in a release tag (see turnstile_cli.upgrade.content_hash
for the normalisation). `turnstile update` treats a local copy whose hash is
listed here as unedited, so installs that predate the starter manifest are
still upgraded safely.
"""


RELEASED_STARTER_HASHES: dict[str, list[str]] = {
    "bug-fix.yaml": [
        "1ce5bf594ea2e6151ea6130d7445a0d31bfb766dde77af93f66f2268e9f4b598",
        "5ad7ae2b2368c8443213c5026a3b1e55d709b106b07b70f4f288a458c020a4ac",
        "708636e6766689355d6381934e6a018a03bf90e15c493baeff86a97b54611f3f",
    ],
    "code-review.yaml": [
        "80673739040129fcea1990ec328d6504fb9293a020eaed8e11f08a5c5d8d3e25",
    ],
    "create-process.yaml": [
        "7ae7075c635a17f5c57248ef41759d67b0f9540819bd8973ed4489dd336ab8a3",
        "b26d3a88d36a8698d557ebcd50223f858f7cd315073f001b4c3719a5a2987321",
    ],
    "decision-record.yaml": [
        "2d634fe7c26e72c4da3897cdc1526a2bfa4d94a7711b634bbb83c822f750981f",
        "e26396b8889d0a14e9144cb7fdf25d73e7ae7e75851a695a0eaa6a7578fae4ff",
    ],
    "feature-development.yaml": [
        "b36db207cf586ba55be20ce7bda7b9424ada4dbc983a77b019a5e41ba94a1f82",
        "ba175adc8254fdcd2076568d67b91b49c377d6a9d2169200a8b72099012e9c77",
        "f9ae32725f48f27d0b50d32d21dd7bfb79269176949e64af9197825bfdcb852c",
    ],
    "peer-review.yaml": [
        "55551040a78d993165d1c8340c43c5db7f552e08ee530599a74225222ff71075",
        "999e6b312de57c09b827cd77fe0a4759bef04a70a8fc80ebecaecba159419a7f",
    ],
    "readme-update.yaml": [
        "28170df2f55e889f013d177188bb7b8e638437d8a9f557fba0dfafadf4f39a0d",
        "3c7c910f00e3166e12198b7fa68b901506d76c023c45738b95af1d6cfe6fcec8",
        "48f38d510664e12a0849fd158b67fe4bead780b0b9988313315c7b75f1127f89",
    ],
    "release.yaml": [
        "6b9da9a91502b1b01b87f5b20a09fa01f9d9b82e1edc705f206aef9a7f45e0e1",
        "fdf2e1303b93618c9c8cb75e81059bfe1e099eb63c3c152d0ccf9f0c27f81ad4",
    ],
    "scientific-method.yaml": [
        "095715ed636cca05db87cca04f7e8a0ca0c58881ddf8f2c3a218eccf6348ddc2",
        "b0814d5964d0ccd2acd84d58322c98cc17ace98fbced53d266ea54f023492c66",
    ],
    "security-review.yaml": [
        "6e9572456437f317549e5e0f58d2e85126a130ce14b0d2a11410173a626e5cd0",
    ],
    "spike.yaml": [
        "1b62e95d08fbde39c42a48917f2f53328a70497a9fbb00160c96f289cfd0c91c",
        "db03470c63149422ab0c5790fcc040eef4da505a228ae8247d8d459abd548f06",
    ],
    "turnstile-feedback.yaml": [
        "1c31d6d648b2578d4fd79ec5c79199462fce503415d74be795247f529e1e87b9",
    ],
}
