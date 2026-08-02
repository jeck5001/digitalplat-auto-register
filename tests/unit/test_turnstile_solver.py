import asyncio

from digitalplat_auto_register.services.turnstile_solver import TurnstileSolver
from digitalplat_auto_register.types import TurnstileConfig, TurnstileSolverType


class FakeResponse:
    def __init__(self, payload):
        self.status_code = 200
        self._payload = payload

    def json(self):
        return self._payload


class RecordingSession:
    def __init__(self):
        self.timeouts = []
        self.responses = [
            FakeResponse({"errorId": 0, "taskId": "task-1"}),
            FakeResponse({
                "errorId": 0,
                "status": "ready",
                "solution": {"token": "turnstile-token"},
            }),
        ]

    def post(self, *args, **kwargs):
        self.timeouts.append(kwargs.get("timeout"))
        return self.responses.pop(0)

    def close(self):
        return None


def test_remote_solver_applies_timeout_to_create_and_poll_requests():
    config = TurnstileConfig(
        solver_type=TurnstileSolverType.REMOTE,
        remote_endpoint="https://solver.example.test",
        timeout=17,
    )
    solver = TurnstileSolver(config)
    session = RecordingSession()
    solver.session = session

    token = asyncio.run(
        solver._get_remote_token(
            website_url="https://site.example.test/register",
            website_key="site-key",
        )
    )

    assert token == "turnstile-token"
    assert session.timeouts == [17, 17]
