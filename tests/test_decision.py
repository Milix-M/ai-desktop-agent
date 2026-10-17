"""決定モデル（完了確認・回復方針）のテスト。"""

import pytest

from ai_desktop_agent.actions.primitives import Action, ActionType
from ai_desktop_agent.agent.decision import eval as clef_eval
from ai_desktop_agent.agent.decision.provider import (
    MockDecisionProvider,
    OpenRouterClefProvider,
    WorkersAiClefProvider,
    create_decision_provider_from_env,
)
from ai_desktop_agent.agent.decision.routing import route_recovery, route_verify
from ai_desktop_agent.agent.decision.types import RecoverCase, VerifyCase
from ai_desktop_agent.agent.llm.mock import MockLLMProvider
from ai_desktop_agent.agent.llm.types import (
    ActionDecision,
    RecoveryPlan,
    RecoveryStrategy,
    VerificationResult,
)
from ai_desktop_agent.agent.state import AgentState, Goal, Subtask
from ai_desktop_agent.server.session import TaskSession
from ai_desktop_agent.vm.fake import FakeDisplayBackend


class TestRouting:
    def test_verify_tri_state(self):
        assert route_verify(0.95, "full", high=0.9, low=0.6) == "achieved"
        assert route_verify(0.2, "full", high=0.9, low=0.6) == "not_achieved"
        assert route_verify(0.95, "none", high=0.9, low=0.6) == "not_achieved"
        assert route_verify(0.72, "partial", high=0.9, low=0.6) == "escalate"
        assert route_verify(0.95, "partial", high=0.9, low=0.6) == "escalate"

    def test_recovery_threshold(self):
        assert route_recovery(0.8, minimum=0.6) is True
        assert route_recovery(0.55, minimum=0.6) is False


class TestMockProvider:
    @pytest.mark.asyncio
    async def test_verify_achieved_on_overlap(self):
        p = MockDecisionProvider()
        r = await p.verify_completion("ツールバーが見える", "ツールバーが見えることを確認", 1.0)
        assert r.success is True
        assert r.prob >= 0.9

    @pytest.mark.asyncio
    async def test_verify_not_achieved_without_overlap(self):
        p = MockDecisionProvider()
        r = await p.verify_completion("ツールバーが見える", "真っ黒の画面だった", 1.0)
        assert r.success is False

    @pytest.mark.asyncio
    async def test_recover_maps_unchanged_screen(self):
        p = MockDecisionProvider()
        r = await p.choose_recovery("前回の操作後も画面に変化がありません", "Firefox起動")
        assert r.strategy == "alternative_approach"
        assert r.escalate is False

    @pytest.mark.asyncio
    async def test_recover_escalates_on_vague_error(self):
        p = MockDecisionProvider()
        r = await p.choose_recovery("何か変だった", "何かする")
        assert r.escalate is True


class TestWorkersAiPayload:
    def test_verify_payload_has_two_questions(self):
        p = WorkersAiClefProvider.__new__(WorkersAiClefProvider)
        payload = WorkersAiClefProvider.build_verify_payload(p, "期待結果", "根拠テキスト")
        kinds = {q["type"] for q in payload["questions"]}
        assert kinds == {"noul", "score"}
        assert "期待結果" in payload["state"]

    def test_recover_payload_has_choice(self):
        p = WorkersAiClefProvider.__new__(WorkersAiClefProvider)
        payload = WorkersAiClefProvider.build_recover_payload(p, "失敗", "説明")
        assert payload["questions"][0]["type"] == "choice"
        assert "give_up" in payload["questions"][0]["options"]


class TestOpenRouterClef:
    def _provider(self, **kwargs):
        kwargs.setdefault("api_key", "test-key")
        return OpenRouterClefProvider(**kwargs)

    def test_model_normalization_strips_cf_prefix(self):
        p = self._provider(model="@cf/cloudflare/clef-flash")
        assert p.model_name == "cloudflare/clef-flash"
        assert p.provider_name == "clef-openrouter"

    def test_workers_ai_model_normalization_adds_cf_prefix(self):
        from ai_desktop_agent.agent.decision.provider import _normalize_clef_model

        assert (
            _normalize_clef_model("cloudflare/clef", for_openrouter=False) == "@cf/cloudflare/clef"
        )
        assert (
            _normalize_clef_model("@cf/cloudflare/clef-flash", for_openrouter=True)
            == "cloudflare/clef-flash"
        )

    def test_requires_api_key(self):
        with pytest.raises(ValueError):
            OpenRouterClefProvider(api_key="")

    def test_verify_payload_is_decisions_dict_form(self):
        p = self._provider(model="cloudflare/clef-flash")
        payload = p.build_verify_payload("期待結果", "根拠テキスト")
        assert payload["model"] == "cloudflare/clef-flash"
        assert isinstance(payload["state"], str)
        assert "期待結果" in payload["state"]
        questions = payload["questions"]
        assert set(questions) == {"verify_achieved", "verify_grade"}
        assert questions["verify_achieved"]["type"] == "noul"
        assert questions["verify_grade"]["type"] == "score"

    def test_verify_payload_with_image_uses_state_array(self):
        import io

        from PIL import Image

        buf = io.BytesIO()
        Image.new("RGB", (10, 10), color="red").save(buf, format="PNG")
        p = self._provider()
        payload = p.build_verify_payload("期待結果", "根拠", buf.getvalue())
        state = payload["state"]
        assert isinstance(state, list)
        assert isinstance(state[0], str)
        img_part = state[1]
        assert img_part["type"] == "image_url"
        assert img_part["image_url"]["url"].startswith("data:image/jpeg;base64,")

    def test_recover_payload_has_choice_criteria(self):
        p = self._provider()
        payload = p.build_recover_payload("失敗", "説明")
        q = payload["questions"]["recover_strategy"]
        assert q["type"] == "choice"
        assert set(q["criteria"]) == {
            "wait_and_retry",
            "alternative_approach",
            "replan_subtask",
            "give_up",
        }

    @pytest.mark.asyncio
    async def test_verify_parses_openrouter_answers(self):
        p = self._provider()

        async def _fake_post(payload):
            assert payload["model"] == "cloudflare/clef-flash"
            return {
                "verify_achieved": {"type": "noul", "noul": 0.95},
                "verify_grade": {
                    "type": "score",
                    "probabilities": {"完全達成": 0.9, "部分達成": 0.08, "未達": 0.02},
                },
            }

        p._post = _fake_post  # type: ignore[method-assign]
        r = await p.verify_completion("期待結果", "根拠")
        assert r.success is True
        assert r.prob == 0.95
        assert r.grade == "full"

    @pytest.mark.asyncio
    async def test_recover_parses_openrouter_choice(self):
        p = self._provider()

        async def _fake_post(payload):
            return {
                "recover_strategy": {
                    "type": "choice",
                    "choice": "alternative_approach",
                    "probabilities": {"alternative_approach": 0.82, "wait_and_retry": 0.1},
                }
            }

        p._post = _fake_post  # type: ignore[method-assign]
        r = await p.choose_recovery("画面に変化がありません", "Firefox起動")
        assert r.strategy == "alternative_approach"
        assert r.prob == 0.82
        assert r.escalate is False

    @pytest.mark.asyncio
    async def test_unparsable_response_raises_for_llm_fallback(self):
        p = self._provider()

        async def _fake_post(payload):
            return {"garbage": 1}

        p._post = _fake_post  # type: ignore[method-assign]
        with pytest.raises(ValueError):
            await p.verify_completion("a", "b")

    def test_grade_with_legend_indexed_probabilities(self):
        from ai_desktop_agent.agent.decision.provider import _extract_grade

        grade = _extract_grade(
            {
                "verify_grade": {
                    "type": "score",
                    "score": 0.187,
                    "legend": {"0": "完全達成", "1": "部分達成", "2": "未達"},
                    "probabilities": {"0": 0.8505, "1": 0.112, "2": 0.0375},
                }
            }
        )
        assert grade == "full"

    def test_mixed_string_values_do_not_crash(self):
        from ai_desktop_agent.agent.decision.provider import _extract_choice, _extract_grade

        assert _extract_grade({"verify_grade": {"type": "score", "score": 0.5}}) == "partial"
        strategy, prob = _extract_choice(
            {"recover_strategy": {"choice": "wait_and_retry", "prob": "high"}}
        )
        assert (strategy, prob) == ("wait_and_retry", 0.7)


class TestDecisionFactory:
    def test_clef_openrouter_from_env(self, monkeypatch):
        monkeypatch.setenv("DECISION_PROVIDER", "clef-openrouter")
        monkeypatch.setenv("CLEF_API_KEY", "sk-or-test")
        monkeypatch.delenv("CLOUDFLARE_ACCOUNT_ID", raising=False)
        p = create_decision_provider_from_env()
        assert isinstance(p, OpenRouterClefProvider)

    def test_clef_prefers_dedicated_key(self, monkeypatch):
        monkeypatch.setenv("DECISION_PROVIDER", "clef-openrouter")
        monkeypatch.setenv("CLEF_API_KEY", "dedicated")
        monkeypatch.setenv("OPENROUTER_API_KEY", "shared")
        p = create_decision_provider_from_env()
        assert isinstance(p, OpenRouterClefProvider)
        assert p._api_key == "dedicated"

    def test_clef_falls_back_to_shared_openrouter_key(self, monkeypatch):
        monkeypatch.setenv("DECISION_PROVIDER", "clef-openrouter")
        monkeypatch.delenv("CLEF_API_KEY", raising=False)
        monkeypatch.setenv("OPENROUTER_API_KEY", "shared")
        p = create_decision_provider_from_env()
        assert isinstance(p, OpenRouterClefProvider)
        assert p._api_key == "shared"

    def test_clef_openrouter_without_key_raises(self, monkeypatch):
        monkeypatch.setenv("DECISION_PROVIDER", "clef-openrouter")
        monkeypatch.delenv("CLEF_API_KEY", raising=False)
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        with pytest.raises(ValueError):
            create_decision_provider_from_env()


def _complete_session(llm, decision=None) -> TaskSession:
    s = TaskSession(llm=llm, display=FakeDisplayBackend(), decision=decision)
    s.loop.start(Goal(description="g"))
    s.loop.understanding_done()
    s.loop.plan_ready([Subtask(id="s1", description="d", expected_outcome="ツールバーが見える")])
    s.loop.record_action(Action(action_type=ActionType.MOUSE_MOVE, params={"x": 1, "y": 1}), True)
    s.loop.record_action(Action(action_type=ActionType.SUBTASK_COMPLETE), True)
    s.loop.action_executed()
    s.loop.wait_complete()
    assert s.loop.state == AgentState.VERIFYING
    return s


def _recording_llm(**kwargs) -> MockLLMProvider:
    llm = MockLLMProvider(**kwargs)
    llm.verify_calls = 0  # type: ignore[attr-defined]
    llm.recover_calls = 0  # type: ignore[attr-defined]
    orig_verify = llm.verify_result
    orig_recover = llm.recover_from_error

    async def _verify(*a, **k):
        llm.verify_calls += 1  # type: ignore[attr-defined]
        return await orig_verify(*a, **k)

    async def _recover(*a, **k):
        llm.recover_calls += 1  # type: ignore[attr-defined]
        return await orig_recover(*a, **k)

    llm.verify_result = _verify  # type: ignore[method-assign]
    llm.recover_from_error = _recover  # type: ignore[method-assign]
    return llm


class TestSessionVerifyIntegration:
    @pytest.mark.asyncio
    async def test_decision_high_confidence_skips_llm(self):
        llm = _recording_llm()
        s = _complete_session(llm, decision=MockDecisionProvider())
        s.loop.context.action_history[-1].reasoning = "ツールバーが見える"
        await s._verify_phase()
        assert s.loop.state == AgentState.COMPLETED
        assert llm.verify_calls == 0
        assert s.decision_calls == 1

    @pytest.mark.asyncio
    async def test_decision_low_confidence_goes_recovering_without_llm(self):
        llm = _recording_llm()
        s = _complete_session(llm, decision=MockDecisionProvider())
        s.loop.context.action_history[-1].reasoning = "真っ黒の画面だった"
        await s._verify_phase()
        assert s.loop.state == AgentState.RECOVERING
        assert llm.verify_calls == 0

    @pytest.mark.asyncio
    async def test_decision_failure_falls_back_to_llm(self):
        class _Boom(MockDecisionProvider):
            async def verify_completion(self, *a, **k):
                raise RuntimeError("boom")

        llm = _recording_llm(
            verify_result=VerificationResult(success=True, reasoning="ok", evidence="e")
        )
        s = _complete_session(llm, decision=_Boom())
        await s._verify_phase()
        assert s.loop.state == AgentState.COMPLETED
        assert llm.verify_calls == 1
        assert s.decision_escalations == 1


class TestSessionRecoverIntegration:
    def _recover_session(self, llm, decision):
        s = TaskSession(llm=llm, display=FakeDisplayBackend(), decision=decision)
        s.loop.start(Goal(description="g"))
        s.loop.understanding_done()
        s.loop.plan_ready([Subtask(id="s1", description="d")])
        s.loop.record_action(
            Action(action_type=ActionType.LEFT_CLICK, params={"x": 1, "y": 1}),
            False,
            "画面に変化がありません",
        )
        s.loop.action_executed()
        s.loop.wait_complete()
        s.loop.verify_failed()
        assert s.loop.state == AgentState.RECOVERING
        return s

    @pytest.mark.asyncio
    async def test_decision_recovery_skips_llm(self):
        llm = _recording_llm()
        s = self._recover_session(llm, MockDecisionProvider())
        await s._recover_phase()
        assert s.loop.state == AgentState.EXECUTING
        assert llm.recover_calls == 0

    @pytest.mark.asyncio
    async def test_decision_give_up_fails_task(self):
        class _GiveUp(MockDecisionProvider):
            async def choose_recovery(self, *a, **k):
                from ai_desktop_agent.agent.decision.types import DecisionRecovery

                return DecisionRecovery(
                    strategy="give_up", recoverable=False, prob=0.9, reasoning="mock"
                )

        llm = _recording_llm()
        s = self._recover_session(llm, _GiveUp())
        await s._recover_phase()
        assert s.loop.state == AgentState.FAILED
        assert llm.recover_calls == 0

    @pytest.mark.asyncio
    async def test_recover_failure_falls_back_to_llm(self):
        class _Boom(MockDecisionProvider):
            async def choose_recovery(self, *a, **k):
                raise RuntimeError("boom")

        llm = _recording_llm(
            recover_result=RecoveryPlan(
                strategy=RecoveryStrategy.WAIT_AND_RETRY,
                actions=[],
                reasoning="ok",
                recoverable=True,
            )
        )
        s = self._recover_session(llm, _Boom())
        await s._recover_phase()
        assert s.loop.state == AgentState.EXECUTING
        assert llm.recover_calls == 1


class TestEvalHarness:
    def _record(self, **kw):
        base = {
            "id": "t1",
            "instruction": "i",
            "state": "completed",
            "success": True,
            "actions": [],
            "subtasks": [],
            "current_subtask_index": 0,
        }
        base.update(kw)
        return base

    def test_build_verify_cases_maps_completes(self):
        rec = self._record(
            subtasks=[{"id": "s1", "description": "d", "expected_outcome": "ツールバーが見える"}],
            actions=[
                {
                    "action_type": "subtask_complete",
                    "params": {},
                    "success": True,
                    "reasoning": "ツールバーが見える",
                    "confidence": 0.9,
                }
            ],
        )
        cases = clef_eval.build_verify_cases([rec])
        assert len(cases) == 1
        assert cases[0].expected_outcome == "ツールバーが見える"

    @pytest.mark.asyncio
    async def test_evaluate_counts_high_conf_errors(self):
        from ai_desktop_agent.agent.decision.types import DecisionVerify

        class _WrongConfident(MockDecisionProvider):
            async def verify_completion(self, *a, **k):
                return DecisionVerify(success=True, prob=0.95, grade="full", reasoning="x")

        m = await clef_eval.evaluate(
            _WrongConfident(),
            [VerifyCase(id="v1", expected_outcome="A", evidence_text="B", label=False)],
            [],
        )
        assert m.verify_high_conf_errors == 1
        assert "not-usable" in clef_eval.threshold_recommendation(m)

    @pytest.mark.asyncio
    async def test_evaluate_cost_estimate(self):
        m = await clef_eval.evaluate(
            MockDecisionProvider(),
            [VerifyCase(id="v1", expected_outcome="A", evidence_text="A", label=True)],
            [RecoverCase(id="r1", error_message="画面に変化がありません", subtask_description="d")],
        )
        assert m.est_llm_cost_without_clef > m.est_total_with_clef

    def test_decision_counts_in_snapshot(self):
        async def _run():
            llm = _recording_llm()
            s = _complete_session(llm, decision=MockDecisionProvider())
            s.loop.context.action_history[-1].reasoning = "ツールバーが見える"
            await s._verify_phase()
            rec = s.snapshot(success=True)
            assert rec.decision_calls == 1
            d = rec.to_dict()
            assert d["decision_calls"] == 1

        import asyncio

        asyncio.run(_run())

    def test_action_decision_import_used(self):
        d = ActionDecision(
            action=Action(action_type=ActionType.WAIT, params={"seconds": 1.0}),
            expected_effect="e",
            confidence=0.5,
            reasoning="r",
        )
        assert d.confidence == 0.5
