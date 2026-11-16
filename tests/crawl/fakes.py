"""Test doubles for the LLM chain and S3."""

import io
import json
from datetime import UTC, datetime
from typing import Any


class FakeChat:
    """A provider that replays canned answers (strings, or exceptions to raise)."""

    def __init__(self, name: str, answers: list[Any], *, up: bool = True) -> None:
        self.name = name
        self.model = f"{name}-model"
        self.up = up
        self.answers = list(answers)
        self.calls: list[list[dict[str, str]]] = []
        self.formats: list[bool] = []

    async def available(self) -> bool:
        return self.up

    async def complete(self, messages: list[dict[str, str]], schema: dict[str, Any], *, json_schema: bool) -> str:
        self.calls.append(messages)
        self.formats.append(json_schema)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer if isinstance(answer, str) else json.dumps(answer)


class FakeS3:
    """Just enough of boto3's client for SnapshotStore."""

    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, Any]] = {}

    def put_object(self, Bucket: str, Key: str, Body: bytes) -> None:  # noqa: N803
        self.objects[Key] = (Body, datetime.now(UTC))

    def get_object(self, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803
        return {"Body": io.BytesIO(self.objects[Key][0])}

    def get_paginator(self, _name: str) -> Any:
        objects = self.objects

        class P:
            def paginate(self, Bucket: str, Prefix: str):  # noqa: N803
                yield {
                    "Contents": [{"Key": k, "LastModified": v[1]} for k, v in objects.items() if k.startswith(Prefix)]
                }

        return P()

    def delete_objects(self, Bucket: str, Delete: dict[str, Any]) -> None:  # noqa: N803
        for o in Delete["Objects"]:
            self.objects.pop(o["Key"], None)
