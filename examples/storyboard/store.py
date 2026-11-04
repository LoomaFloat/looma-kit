"""Хранилище проектов: бакет сервиса на платформе, у себя папка.

Платформа даёт процессу `LOOMA_BUCKET` и `AWS_*` (раздел `data.bucket`
в looma.yaml). Без них (`looma dev --no-data` или просто `python app.py`)
всё ложится в ./data: так сервис запускается без Docker.
"""

import os
from pathlib import Path


class Store:
    def __init__(self):
        self.name = os.environ.get("LOOMA_BUCKET", "")
        if self.name:
            import boto3
            from botocore.config import Config

            # Бакет платформы адресуется по пути (bucket в пути, а не в имени хоста).
            self.s3 = boto3.client("s3", endpoint_url=os.environ.get("AWS_ENDPOINT_URL_S3"),
                                   region_name=os.environ.get("AWS_REGION", "us-east-1"),
                                   config=Config(s3={"addressing_style": "path"}))
        else:
            self.root = Path(os.environ.get("STORYBOARD_DATA", "data"))
            self.root.mkdir(parents=True, exist_ok=True)

    def where(self):
        return f"бакет {self.name}" if self.name else f"папка {self.root.resolve()}"

    def put(self, key, data, kind):
        if self.name:
            self.s3.put_object(Bucket=self.name, Key=key, Body=data, ContentType=kind)
            return
        path = self.root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(path)               # читатель не увидит файл наполовину

    def get(self, key):
        """Байты объекта. Если объекта нет, KeyError."""
        if self.name:
            try:
                return self.s3.get_object(Bucket=self.name, Key=key)["Body"].read()
            except self.s3.exceptions.NoSuchKey:
                raise KeyError(key) from None
        try:
            return (self.root / key).read_bytes()
        except FileNotFoundError:
            raise KeyError(key) from None

    def folders(self, prefix):
        """Имена «папок» прямо под prefix (prefix кончается на /)."""
        if self.name:
            pages = self.s3.get_paginator("list_objects_v2").paginate(
                Bucket=self.name, Prefix=prefix, Delimiter="/")
            return [p["Prefix"][len(prefix):].rstrip("/")
                    for page in pages for p in page.get("CommonPrefixes", [])]
        base = self.root / prefix
        return [p.name for p in base.iterdir() if p.is_dir()] if base.is_dir() else []

    def delete_folder(self, prefix):
        if self.name:
            pages = self.s3.get_paginator("list_objects_v2").paginate(Bucket=self.name,
                                                                       Prefix=prefix)
            keys = [{"Key": o["Key"]} for page in pages for o in page.get("Contents", [])]
            for start in range(0, len(keys), 1000):
                self.s3.delete_objects(Bucket=self.name,
                                       Delete={"Objects": keys[start:start + 1000]})
            return
        base = self.root / prefix
        if base.is_dir():
            for path in sorted(base.rglob("*"), reverse=True):
                path.unlink() if path.is_file() else path.rmdir()
            base.rmdir()
