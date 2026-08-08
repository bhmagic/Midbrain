from __future__ import annotations

import asyncio
import copy
import hashlib
import json
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any


PrepareAction = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]
SelectContinuation = Callable[
    [dict[str, Any]],
    dict[str, Any] | None,
]
ResolveAuthorization = Callable[
    [dict[str, Any]],
    Awaitable[dict[str, Any] | None],
]
ExecuteContinuation = Callable[
    [dict[str, Any]],
    Awaitable[dict[str, Any]],
]


@dataclass(frozen=True)
class PreparedAction:
    call_id: str
    input_digest: str
    preparation_result: dict[str, Any]
    continuation_arguments: dict[str, Any] | None
    authorization_arguments: dict[str, Any] | None

    @property
    def executable(self) -> bool:
        return (
            self.continuation_arguments is not None
            and self.authorization_arguments is not None
        )


class CallScopedPreparedActionCoordinator:
    """Bind one prepared continuation to one exact agent tool call."""

    def __init__(
        self,
        *,
        prepare_action: PrepareAction,
        select_continuation: SelectContinuation,
        resolve_authorization: ResolveAuthorization,
        execute_continuation: ExecuteContinuation,
        maximum_pending_calls: int = 64,
    ) -> None:
        maximum = int(maximum_pending_calls)
        if maximum < 1:
            raise ValueError("maximum_pending_calls must be positive")
        self._prepare_action = prepare_action
        self._select_continuation = select_continuation
        self._resolve_authorization = resolve_authorization
        self._execute_continuation = execute_continuation
        self._maximum_pending_calls = maximum
        self._pending: OrderedDict[str, PreparedAction] = OrderedDict()
        self._lock = asyncio.Lock()

    async def prepare_for_call(
        self,
        call_id: str,
        arguments: dict[str, Any],
    ) -> PreparedAction:
        normalized_call_id = str(call_id or "").strip()
        if not normalized_call_id:
            raise ValueError("prepared action requires an SDK tool call ID")
        input_digest = self._input_digest(arguments)
        async with self._lock:
            existing = self._pending.get(normalized_call_id)
            if existing is not None:
                if existing.input_digest != input_digest:
                    raise ValueError(
                        "prepared action call ID was reused with different input"
                    )
                self._pending.move_to_end(normalized_call_id)
                return self._copy_prepared(existing)

            preparation_result = await self._prepare_action(
                copy.deepcopy(arguments)
            )
            if not isinstance(preparation_result, dict):
                raise TypeError("prepared action result must be an object")
            continuation_arguments = self._select_continuation(
                preparation_result
            )
            authorization_arguments = None
            if continuation_arguments is not None:
                continuation_arguments = copy.deepcopy(
                    continuation_arguments
                )
                authorization_arguments = await self._resolve_authorization(
                    copy.deepcopy(continuation_arguments)
                )
                if authorization_arguments is None:
                    continuation_arguments = None
                elif not isinstance(authorization_arguments, dict):
                    raise TypeError(
                        "prepared action authorization must be an object"
                    )

            prepared = PreparedAction(
                call_id=normalized_call_id,
                input_digest=input_digest,
                preparation_result=copy.deepcopy(preparation_result),
                continuation_arguments=continuation_arguments,
                authorization_arguments=copy.deepcopy(
                    authorization_arguments
                ),
            )
            self._pending[normalized_call_id] = prepared
            while len(self._pending) > self._maximum_pending_calls:
                self._pending.popitem(last=False)
            return self._copy_prepared(prepared)

    async def execute_for_call(
        self,
        call_id: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        normalized_call_id = str(call_id or "").strip()
        input_digest = self._input_digest(arguments)
        async with self._lock:
            prepared = self._pending.pop(normalized_call_id, None)
        if prepared is None:
            return self._state_failure(
                "PREPARED_ACTION_STATE_UNAVAILABLE",
                "No call-scoped prepared action remains, so the host did not "
                "issue a new continuation. This does not prove whether an "
                "earlier interrupted attempt submitted physical motion; "
                "inspect authoritative controller evidence before retrying.",
                prior_submission_unknown=True,
            )
        if prepared.input_digest != input_digest:
            return self._state_failure(
                "PREPARED_ACTION_INPUT_MISMATCH",
                "The prepared action input changed before execution. "
                "No continuation ran.",
            )
        if not prepared.executable:
            return copy.deepcopy(prepared.preparation_result)
        assert prepared.continuation_arguments is not None
        return await self._execute_continuation(
            copy.deepcopy(prepared.continuation_arguments)
        )

    async def authorization_arguments_for_call(
        self,
        call_id: str,
    ) -> dict[str, Any] | None:
        normalized_call_id = str(call_id or "").strip()
        async with self._lock:
            prepared = self._pending.get(normalized_call_id)
            if prepared is None or prepared.authorization_arguments is None:
                return None
            return copy.deepcopy(prepared.authorization_arguments)

    async def discard_call(self, call_id: str) -> None:
        normalized_call_id = str(call_id or "").strip()
        if not normalized_call_id:
            return
        async with self._lock:
            self._pending.pop(normalized_call_id, None)

    @staticmethod
    def _input_digest(arguments: dict[str, Any]) -> str:
        encoded = json.dumps(
            arguments,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _copy_prepared(prepared: PreparedAction) -> PreparedAction:
        return PreparedAction(
            call_id=prepared.call_id,
            input_digest=prepared.input_digest,
            preparation_result=copy.deepcopy(
                prepared.preparation_result
            ),
            continuation_arguments=copy.deepcopy(
                prepared.continuation_arguments
            ),
            authorization_arguments=copy.deepcopy(
                prepared.authorization_arguments
            ),
        )

    @staticmethod
    def _state_failure(
        status: str,
        message: str,
        *,
        prior_submission_unknown: bool = False,
    ) -> dict[str, Any]:
        return {
            "status": status,
            "workflow_complete": False,
            "physical_motion_authorized": (
                None if prior_submission_unknown else False
            ),
            "physical_motion_submitted": (
                None if prior_submission_unknown else False
            ),
            "physical_motion_completed": False,
            "new_continuation_submitted": False,
            "message": message,
        }
