from __future__ import annotations

import mimetypes
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import boto3
from boto3.s3.transfer import TransferConfig


ROOT_DIR = Path(__file__).resolve().parent.parent
MODIFIED_DIR = ROOT_DIR / "modified"


def required_env(name: str) -> str:
    value = os.environ.get(name)

    if not value:
        raise RuntimeError(
            f"缺少环境变量: {name}"
        )

    return value


def upload_file(
    client,
    bucket: str,
    local_path: Path,
    key: str,
    config: TransferConfig,
) -> None:

    content_type = (
        mimetypes.guess_type(
            str(local_path)
        )[0]
        or "application/octet-stream"
    )

    if (
        local_path.name.endswith(".hash")
        or local_path.name == "TableManifestHash"
    ):
        content_type = "text/plain"

    if local_path.suffix.lower() == ".json":
        content_type = "application/json"

    client.upload_file(
        str(local_path),
        bucket,
        key,
        ExtraArgs={
            "ContentType": content_type,
        },
        Config=config,
    )

    print(
        f"[UPLOADED] {key}"
    )


def main():

    if not MODIFIED_DIR.is_dir():
        raise RuntimeError(
            "modified/ 不存在"
        )

    account_id = required_env(
        "R2_ACCOUNT_ID"
    )

    access_key_id = required_env(
        "R2_ACCESS_KEY_ID"
    )

    secret_access_key = required_env(
        "R2_SECRET_ACCESS_KEY"
    )

    bucket = required_env(
        "R2_BUCKET"
    )

    endpoint = (
        f"https://{account_id}"
        ".r2.cloudflarestorage.com"
    )

    client = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access_key_id,
        aws_secret_access_key=secret_access_key,
        region_name="auto",
    )

    config = TransferConfig(
        multipart_threshold=64 * 1024 * 1024,
        multipart_chunksize=64 * 1024 * 1024,
        max_concurrency=8,
        use_threads=True,
    )

    files = sorted(
        path
        for path in MODIFIED_DIR.rglob("*")
        if path.is_file()
    )

    print(
        f"准备上传 {len(files)} 个文件"
    )

    def upload_one(
        local_path: Path,
    ):
        relative = local_path.relative_to(
            MODIFIED_DIR
        )

        key = relative.as_posix()

        upload_file(
            client,
            bucket,
            local_path,
            key,
            config,
        )

    with ThreadPoolExecutor(
        max_workers=4
    ) as executor:

        list(
            executor.map(
                upload_one,
                files,
            )
        )

    print()
    print(
        f"R2 上传完成，共 {len(files)} 个文件"
    )


if __name__ == "__main__":
    main()