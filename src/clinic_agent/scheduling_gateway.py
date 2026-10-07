"""Conversation-to-scheduler HTTP adapter. Every request carries scoped identity."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import httpx

from clinic_agent.models import BookingProposal, Constraints, Principal, Slot


class HTTPSchedulingGateway:
    def __init__(self, base_url: str = "http://127.0.0.1:8002", signing_secret: str | None = None):
        self.base_url = base_url.rstrip("/")
        self.signing_secret = signing_secret
        self.client = httpx.Client(base_url=self.base_url, timeout=10)

    @staticmethod
    def _state(state: dict) -> dict:
        return {
            key: state.get(key)
            for key in (
                "conversation_id",
                "tenant_id",
                "patient_id",
                "request_revision",
                "constraints",
                "status",
            )
        }

    def _call(self, method: str, path: str, principal: Principal, **kwargs: Any) -> Any:
        from clinic_agent.auth import sign_context

        token = sign_context(principal, self.signing_secret, audience="scheduling")
        try:
            response = self.client.request(
                method, path, headers={"Authorization": f"Bearer {token}"}, **kwargs
            )
        except httpx.RequestError as exc:
            raise ConnectionError(
                "The scheduling service did not return a reliable response."
            ) from exc
        if response.status_code == 403:
            raise PermissionError("The scheduling service denied this request.")
        if response.status_code == 404:
            raise KeyError("The scheduling record was not found.")
        if response.status_code in {400, 409, 422}:
            raise ValueError("The scheduling service rejected the request.")
        if response.status_code >= 500:
            raise ConnectionError("The scheduling service is unavailable.")
        response.raise_for_status()
        return response.json()

    def search(
        self, principal: Principal, constraints: Constraints, state: dict, now: datetime
    ) -> list[Slot]:
        data = self._call(
            "GET",
            "/internal/slots",
            principal,
            params={
                "constraints": json.dumps(constraints.model_dump()),
                "conversation_id": state["conversation_id"],
                "request_revision": state["request_revision"],
            },
        )
        return [Slot.model_validate(slot) for slot in data["slots"]]

    def book(
        self,
        principal: Principal,
        proposal: BookingProposal,
        operation_id: str,
        digest: str,
        state: dict,
        now: datetime,
    ) -> dict:
        return self._call(
            "POST",
            "/internal/book",
            principal,
            json={
                "proposal": proposal.model_dump(),
                "operation_id": operation_id,
                "digest": digest,
                "state": self._state(state),
            },
        )

    def lookup(self, principal: Principal, operation_id: str, state: dict, now: datetime) -> dict:
        return self._call(
            "POST",
            f"/internal/operations/{operation_id}/lookup",
            principal,
            json={"state": self._state(state)},
        )

    def inspect_world(self) -> dict:
        return self._call(
            "GET", "/internal/world", Principal(role="staff", patient_id="demo-evaluation-service")
        )

    def close(self) -> None:
        self.client.close()
